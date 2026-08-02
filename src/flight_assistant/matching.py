"""步骤 3.5：跨平台匹配 + 比价。确定性代码，不涉及 agent。"""

from decimal import Decimal

from flight_assistant.models import (
    FlightPriceComparison,
    Itinerary,
    PlatformOffer,
    Segment,
)


def _segment_key(seg: Segment) -> str:
    """单段的身份标识：承运方 + 起降机场 + 起降时刻。

    刻意不包含 flight_no —— 代码共享航班在不同平台显示的航班号可能不
    一致（同一架飞机在携程显示 UA850、在飞猪显示 CA7623），按航班号
    字符串匹配会把同一趟航班误判成两趟。
    """
    return "|".join(
        [
            seg.carrier,
            seg.dep_airport,
            seg.arr_airport,
            seg.dep_local.isoformat(),
            seg.arr_local.isoformat(),
        ]
    )


def build_itinerary_key(itinerary: Itinerary) -> str:
    """整个行程的身份标识：各航段 key 按顺序拼接。"""
    return "::".join(_segment_key(seg) for seg in itinerary.segments)


def group_and_compare(
    fetched: list[tuple[Itinerary, PlatformOffer]],
) -> list[FlightPriceComparison]:
    """把四个平台各自取到的 (行程, 报价) 按 itinerary_key 分组比价。

    cheapest_platform 和 price_spread 都是代码算出来的，不交给 agent 判断。
    """
    grouped: dict[str, tuple[Itinerary, list[PlatformOffer]]] = {}
    for itinerary, offer in fetched:
        key = build_itinerary_key(itinerary)
        if key not in grouped:
            grouped[key] = (itinerary, [])
        grouped[key][1].append(offer)

    comparisons: list[FlightPriceComparison] = []
    for key, (itinerary, offers) in grouped.items():
        cheapest = min(offers, key=lambda o: o.price)
        spread = max(o.price for o in offers) - min(o.price for o in offers)
        comparisons.append(
            FlightPriceComparison(
                itinerary_key=key,
                itinerary=itinerary,
                offers=offers,
                cheapest_platform=cheapest.platform,
                price_spread=Decimal(spread),
            )
        )
    return comparisons


def cheapest_price(comparison: FlightPriceComparison) -> Decimal:
    """该行程在所有平台里的最低价，供过滤排序用。"""
    return min(o.price for o in comparison.offers)
