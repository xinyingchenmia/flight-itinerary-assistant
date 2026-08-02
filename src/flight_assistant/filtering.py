"""步骤 4：约束过滤 + 排序。确定性代码，不涉及 agent。"""

from typing import Literal

from pydantic import BaseModel

from flight_assistant.matching import cheapest_price
from flight_assistant.models import FlightPriceComparison

SortPref = Literal["duration", "price", "stops"]


class TripRequest(BaseModel):
    """步骤 2 结构化解析的产物。"""

    origin: str
    dest: str
    date: str
    max_stops: int | None = None
    sort_pref: SortPref = "price"


def filter_candidates(
    comparisons: list[FlightPriceComparison], req: TripRequest
) -> list[FlightPriceComparison]:
    if req.max_stops is None:
        return list(comparisons)
    return [c for c in comparisons if c.itinerary.stop_count <= req.max_stops]


def sort_candidates(
    comparisons: list[FlightPriceComparison], req: TripRequest
) -> list[FlightPriceComparison]:
    """按用户偏好排序。同分时用其余两个维度做稳定的次级排序，
    避免结果顺序随取数顺序抖动。
    """
    if req.sort_pref == "duration":
        key = lambda c: (  # noqa: E731
            c.itinerary.total_duration_min,
            cheapest_price(c),
            c.itinerary.stop_count,
        )
    elif req.sort_pref == "stops":
        key = lambda c: (  # noqa: E731
            c.itinerary.stop_count,
            c.itinerary.total_duration_min,
            cheapest_price(c),
        )
    else:  # "price"
        key = lambda c: (  # noqa: E731
            cheapest_price(c),
            c.itinerary.total_duration_min,
            c.itinerary.stop_count,
        )
    return sorted(comparisons, key=key)


def filter_and_sort(
    comparisons: list[FlightPriceComparison], req: TripRequest
) -> list[FlightPriceComparison]:
    return sort_candidates(filter_candidates(comparisons, req), req)
