"""把飞猪 flight_search_result_poller.do 响应解析成 Itinerary + PlatformOffer。

字段映射基于 2026-08-05 抓取的真实响应（PVG→ORD/SEA 中转，DevTools 手工核对），
样例结构见本文件测试 fixture。

价格口径和携程一致：

price = adultPrice + adultTax，即响应里的 totalAdultPrice
    样例核对：adultPrice=358800, adultTax=205900, totalAdultPrice=564700，
    三者算式成立。单位是分，除以 100 得元。

Segment.carrier = operatingAirlineCode or marketingAirlineCode
    实际承运方优先，和携程口径一致，理由同 ctrip_parse.py。

flightInfo 是数组：单程只有 1 个元素（整趟行程），往返会有 2 个
（去程/回程）。逐个摊平其 flightSegments，行为和 ctrip_parse 摊平
flightSegments 一致。

stop_count 累加各 flightInfo 元素的 transferCount。

TicketGroup.baggage_through_checked = None
    响应里没有联程行李是否直挂的字段，按项目原则标未知，不猜。

delayForNextPoll 是轮询标志（在 fetcher 里处理，不在这里）：0 = 数据已出全，
非 0 = 还要再等这么多毫秒轮询下一批。
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

from flight_assistant.models import Itinerary, PlatformOffer, Segment, TicketGroup

_DT_FMT = "%Y-%m-%d %H:%M:%S"


def _parse_dt(s: str) -> datetime:
    return datetime.strptime(s, _DT_FMT)


def _blank_to_none(v: Any) -> str | None:
    """飞猪对缺失航站楼用空串表示（样例：西雅图段 arrTerm=""），统一成 None。"""
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def parse_segment(flight: dict) -> Segment:
    return Segment(
        carrier=flight.get("operatingAirlineCode") or flight["marketingAirlineCode"],
        flight_no=flight["marketingFlightNo"],
        dep_airport=flight["depAirportCode"],
        dep_terminal=_blank_to_none(flight.get("depTerm")),
        arr_airport=flight["arrAirportCode"],
        arr_terminal=_blank_to_none(flight.get("arrTerm")),
        dep_local=_parse_dt(flight["depTimeStr"]),
        arr_local=_parse_dt(flight["arrTimeStr"]),
        # 响应里没有单独的国家码字段，只有城市码/城市名，不强行推断。
        dep_country=None,
        arr_country=None,
    )


def parse_itinerary(item: dict) -> Itinerary:
    """一条 flightItems 元素 → Itinerary。

    flightInfo 数组摊平逻辑同 ctrip_parse.parse_itinerary：单程 1 个元素，
    往返 2 个，都按顺序把 flightSegments 拼起来。
    """
    segments: list[Segment] = []
    total_duration = 0
    stops = 0

    for leg in item["flightInfo"]:
        for flight in leg["flightSegments"]:
            segments.append(parse_segment(flight))
        stops += int(leg.get("transferCount", 0))
    total_duration = int(item["duration"])

    return Itinerary(
        segments=segments,
        tickets=[
            TicketGroup(
                segment_idx=list(range(len(segments))),
                baggage_through_checked=None,  # 见模块 docstring
                source_platform="feizhu",
            )
        ],
        total_duration_min=total_duration,
        stop_count=stops,
    )


def parse_offer(item: dict, fetched_at: datetime) -> PlatformOffer:
    total = Decimal(str(item["totalAdultPrice"])) / Decimal(100)
    return PlatformOffer(
        platform="feizhu",
        price=total,
        currency="CNY",
        fetched_at=fetched_at,
        booking_url=None,
        fare_conditions_raw=None,  # 响应里没带退改政策原文
        confidence="confirmed",
    )


def parse_poller_response(
    payload: dict, fetched_at: datetime | None = None
) -> tuple[list[tuple[Itinerary, PlatformOffer]], int]:
    """一次 poller.do 响应 → ([(Itinerary, PlatformOffer), ...], delay_ms)。

    delay_ms 即 data.delayForNextPoll：0 表示数据已出全，非 0 表示还要
    再等这么多毫秒重新调用该接口。是否轮询由调用方（fetcher）决定。
    """
    fetched_at = fetched_at or datetime.now()
    data = payload.get("data", {})
    delay_ms = int(data.get("delayForNextPoll", 0))

    out: list[tuple[Itinerary, PlatformOffer]] = []
    for item in data.get("flightItems", []):
        itinerary = parse_itinerary(item)
        offer = parse_offer(item, fetched_at)
        out.append((itinerary, offer))
    return out, delay_ms
