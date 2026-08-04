"""把携程 batchSearch 响应解析成 Itinerary + PlatformOffer。

字段映射基于 2026-08-04 抓取的真实响应（PVG→ORD，47 条行程 / 94 个航段）
核对得出，样例见 tests/fixtures/ctrip_batchsearch_sample.json。

几个关键口径，都是从真实数据核对出来的，不是猜的：

price = adultPrice + adultTax
    该口径下全量最低价 = 5212，与页面显示的「¥5212起 含税价」一致。
    单用 adultPrice 最低是 2627，和页面不符。

Segment.carrier = operateAirlineCode or marketAirlineCode
    实际承运方优先。样例里 LH9152 由 UA945 实际执飞——携程显示 LH9152，
    别的平台可能显示 UA945。itinerary_key 用实际承运方才不会把同一趟
    航班误判成两趟。94 个航段里只有 1 个带 operate* 字段（代码共享是
    少数但确实存在），字段缺失时回落到 marketAirlineCode。

Segment.flight_no = flightNo
    保留携程展示的那个航班号（市场航班号），便于和页面/订单对照。
    刻意不参与 itinerary_key 计算。

stop_count = transferCount，不是 stopCount
    transferCount 是中转次数；stopCount 是技术经停（不换飞机），
    两者语义不同，样例里 stopCount 全为 0。

TicketGroup.baggage_through_checked = None
    getVisaLuggageDirectInfo 接口返回 luggageDirectType（观测到值 2），
    但这个枚举的含义没有公开文档，样本里也没有足够变化来反推。
    按项目原则：查不到就标未知，不猜。确定枚举含义后再改这里。
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

from flight_assistant.models import Itinerary, PlatformOffer, Segment, TicketGroup

_DT_FMT = "%Y-%m-%d %H:%M:%S"


def _parse_dt(s: str) -> datetime:
    return datetime.strptime(s, _DT_FMT)


def _blank_to_none(v: Any) -> str | None:
    """携程对缺失航站楼用字段缺失或空串表示，统一成 None。

    94 个航段里 13 个没有 departureTerminal。航站楼是 MCT 判断的关键输入，
    缺失必须表达成 None（未知），不能填空串冒充"已知无航站楼"。
    """
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def parse_segment(flight: dict) -> Segment:
    return Segment(
        carrier=flight.get("operateAirlineCode") or flight["marketAirlineCode"],
        flight_no=flight["flightNo"],
        dep_airport=flight["departureAirportCode"],
        dep_terminal=_blank_to_none(flight.get("departureTerminal")),
        arr_airport=flight["arrivalAirportCode"],
        arr_terminal=_blank_to_none(flight.get("arrivalTerminal")),
        dep_local=_parse_dt(flight["departureDateTime"]),
        arr_local=_parse_dt(flight["arrivalDateTime"]),
        dep_country=flight.get("departureCountryCode"),
        arr_country=flight.get("arrivalCountryCode"),
    )


def parse_itinerary(raw: dict) -> Itinerary:
    """一条 flightItineraryList 元素 → Itinerary。

    单程时 flightSegments 只有 1 个元素；往返会有 2 个（去程/回程）。
    这里把所有 flightSegments 的 flightList 按顺序摊平，时长和中转次数
    累加——单程结果不变，往返也不会丢数据。
    """
    segments: list[Segment] = []
    total_duration = 0
    stops = 0

    for seg in raw["flightSegments"]:
        for flight in seg["flightList"]:
            segments.append(parse_segment(flight))
        total_duration += int(seg["duration"])
        stops += int(seg["transferCount"])

    return Itinerary(
        segments=segments,
        tickets=[
            TicketGroup(
                segment_idx=list(range(len(segments))),
                # 携程 batchSearch 未暴露 PNR 拆分信息；这批数据的 penalty
                # 里有单一 validatingCarrier，按一张票处理。若后续发现拆票
                # 产品（自行中转），需要在这里拆成多个 TicketGroup。
                baggage_through_checked=None,  # 见模块 docstring
                source_platform="ctrip",
            )
        ],
        total_duration_min=total_duration,
        stop_count=stops,
    )


def parse_offers(raw: dict, fetched_at: datetime) -> list[PlatformOffer]:
    """一条行程下的 priceList → 多个 PlatformOffer。

    同一趟航班携程会给 2-5 个不同运价产品（不同渠道/舱位/品牌运价），
    每个都是一个独立报价。上层 group_and_compare 按 itinerary_key 分组后，
    这些会落到同一组里，最低价由代码算出。
    """
    offers: list[PlatformOffer] = []
    for price in raw.get("priceList", []):
        total = Decimal(str(price["adultPrice"])) + Decimal(str(price["adultTax"]))
        penalty = price.get("penalty") or {}
        offers.append(
            PlatformOffer(
                platform="ctrip",
                price=total,
                currency="CNY",
                fetched_at=fetched_at,
                booking_url=None,  # batchSearch 不含直达订票链接
                # 退改政策原文，先不解析（原文是一段嵌套 JSON 字符串）
                fare_conditions_raw=penalty.get("penaltyCriteria"),
                confidence="confirmed",
            )
        )
    return offers


def parse_batch_search(
    payload: dict, fetched_at: datetime | None = None
) -> list[tuple[Itinerary, PlatformOffer]]:
    """完整的 batchSearch 响应 → [(Itinerary, PlatformOffer), ...]。

    返回形状直接对接 matching.group_and_compare。
    """
    if str(payload.get("status")) != "0":
        raise RuntimeError(f"携程 batchSearch 返回非成功状态: {payload.get('msg')}")

    fetched_at = fetched_at or datetime.now()
    out: list[tuple[Itinerary, PlatformOffer]] = []

    for raw in payload.get("data", {}).get("flightItineraryList", []):
        itinerary = parse_itinerary(raw)
        for offer in parse_offers(raw, fetched_at):
            out.append((itinerary, offer))
    return out
