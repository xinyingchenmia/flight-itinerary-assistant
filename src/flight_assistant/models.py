from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel


class Segment(BaseModel):
    carrier: str
    flight_no: str
    dep_airport: str
    dep_terminal: str | None
    arr_airport: str
    arr_terminal: str | None
    dep_local: datetime
    arr_local: datetime


class TicketGroup(BaseModel):
    """一张票 = 一个 PNR。用于判断联程保护。"""

    segment_idx: list[int]
    baggage_through_checked: bool | None  # None = 未知，必须 flag
    source_platform: str


class Itinerary(BaseModel):
    segments: list[Segment]
    tickets: list[TicketGroup]
    total_duration_min: int
    stop_count: int


class CostItem(BaseModel):
    label: str
    amount: Decimal
    certainty: Literal["confirmed", "estimated", "unknown"]
    basis: str


class TotalCost(BaseModel):
    items: list[CostItem]
    total_confirmed: Decimal
    total_with_estimates: Decimal
    unknown_items: list[str]


class PlatformOffer(BaseModel):
    platform: Literal["ctrip", "feizhu", "qunar", "official"]
    price: Decimal
    currency: str
    fetched_at: datetime
    booking_url: str | None
    fare_conditions_raw: str | None  # 退改政策原文，先不解析
    confidence: Literal["confirmed", "stale"]


class FlightPriceComparison(BaseModel):
    """同一趟航班在不同平台的比价。itinerary_key 用 operating_carrier +
    航段序列 + 起降时刻生成，不能只用航班号——代码共享航班在不同平台
    显示的航班号可能不一致，按航班号字符串匹配会导致误判。
    """

    itinerary_key: str
    itinerary: Itinerary
    offers: list[PlatformOffer]
    cheapest_platform: str  # 代码算出，非 agent 判断
    price_spread: Decimal  # max - min


class Risk(BaseModel):
    kind: Literal[
        "mct_tight",
        "no_through_baggage",
        "self_transfer_no_protection",
        "transit_visa_required",
        "last_flight_of_day",
        "terminal_change",
        "arrival_no_ground_transit",
        "passport_validity",
    ]
    severity: Literal["blocker", "major", "minor"]
    evidence: str  # 必须引用具体数据，禁止笼统表述
    affected_segments: list[int]
    needs_user_input: bool  # True → 路由给澄清对话 agent
    prob: float | None
    cost_if_realized: Decimal | None
