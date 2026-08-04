"""步骤 4：约束过滤 + 排序。确定性代码，不涉及 agent。"""

from typing import Literal

from pydantic import BaseModel

from flight_assistant.matching import cheapest_price
from flight_assistant.models import FlightPriceComparison

SortPref = Literal["duration", "price", "stops", "layover"]


def total_layover_min(comparison) -> int:
    """各中转点等待时长之和。中转偏好为 shorter 时用它排序——
    这是纯计算，不需要 agent。
    """
    segs = comparison.itinerary.segments
    return sum(
        int((segs[i + 1].dep_local - segs[i].arr_local).total_seconds() // 60)
        for i in range(len(segs) - 1)
    )


class TripRequest(BaseModel):
    """步骤 2 结构化解析的产物。"""

    origin: str
    dest: str
    date: str
    max_stops: int | None = None
    sort_pref: SortPref = "price"
    # 无法映射到上面这些字段的自由文本偏好，原样保留（如"转机地别太无聊"、
    # "不想坐红眼航班"）。这类偏好要么由澄清 agent 消歧成结构化字段，
    # 要么交给风险审查 agent 去查证——代码不猜它们的含义。
    soft_preferences: list[str] = []


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
    elif req.sort_pref == "layover":
        key = lambda c: (  # noqa: E731
            total_layover_min(c),
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
