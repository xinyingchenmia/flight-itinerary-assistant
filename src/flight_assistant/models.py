from datetime import date, datetime
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
    # 国别码来自平台数据（携程 batchSearch 里就有），不是靠机场码推断的。
    # 有了它，"这个中转点是进入哪个国家的第一个落点"就是代码能算的事实，
    # 不用让 agent 凭记忆猜机场属于哪国。至于"该国转机要不要入境提取行李"，
    # 那是 agent 的判断，代码不做这个表。
    dep_country: str | None = None
    arr_country: str | None = None


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


class TripContext(BaseModel):
    """行程之外、但风险判断必需的用户侧信息。

    买票平台不会问这些，可它们决定了一半的风险是否成立：同一个 40 分钟
    中转，持美签直飞过境和需要入境清关重挂行李，结论完全不同；同一个
    23:50 落地，去市区和去机场旁边的酒店，结论也完全不同。

    全部字段可为 None = 未知。澄清对话 agent 只应追问那些「答案会改变
    排序」的字段，不是把这张表当问卷填满。
    """

    nationality: str | None = None  # 护照国籍，如 "CN"
    passport_expiry: date | None = None
    # 已持有的签证：国家码 → 说明，如 {"US": "B1/B2 有效至 2031"}
    visas: dict[str, str] = {}
    # 落地后要去哪。决定 arrival_no_ground_transit 是否成立——
    # 去市区和去机场酒店是两种结论。
    destination_after_arrival: str | None = None
    # 能接受的地面交通方式。只能打车 vs 必须公共交通，会改变排序。
    ground_transport_ok: Literal["taxi_ok", "public_only", "unknown"] = "unknown"
    checked_bags: int | None = None  # 托运件数；0 件时行李直挂风险不成立
    # 中转体验偏好。"转机地别太无聊"这句话有两种相反的解读——想在中转地
    # 逛逛（那就偏好长中转 + 好玩的航站楼），还是嫌等得烦（那就偏好短中转）。
    # 猜错了排序会完全反过来，所以这是澄清 agent 该问的典型问题。
    layover_preference: Literal[
        "shorter",  # 中转越短越好
        "explore",  # 愿意长中转，希望中转地有的逛
        "no_preference",
        "unknown",
    ] = "unknown"


RiskKind = Literal[
    "mct_tight",
    # 需要入境/清关/重新托运的中转（美国首个入境口岸是典型），
    # 和单纯的 mct_tight 分开：成因不同、可缓解手段不同、时长量级也不同
    "entry_connection_tight",
    "no_through_baggage",
    "self_transfer_no_protection",
    "transit_visa_required",
    "last_flight_of_day",
    "terminal_change",
    "arrival_no_ground_transit",
    "passport_validity",
]


class PreferenceNote(BaseModel):
    """针对用户主观偏好的结论。

    和 Risk / Assurance 分开：Risk 是"会导致行程失败"，Assurance 是"查过了
    没问题"，而这里是"就你在意的那件事而言，这个候选怎么样"。主观偏好没有
    blocker/major/minor 的语义——"中转地没什么好逛的"不是故障。

    verdict 用于排序时的加权，不参与过滤：主观偏好不该把候选直接删掉。
    """

    preference: str  # 对应用户原话里的哪个偏好
    verdict: Literal["good", "poor", "unknown"]
    statement: str  # 给用户看的一句话
    evidence: str  # 依据。查过的要写来源
    affected_segments: list[int]


class Assurance(BaseModel):
    """查过了、确认没问题的项。

    只有 Risk 的话，用户看到的永远是坏消息，看不到"哪些事已经替他确认过"。
    实测 agent 联网查到「CTA 蓝线 O'Hare 站 24 小时运营」，这个结论只体现
    为"风险降级"，正面信息被丢掉了——而"你半夜落地也有地铁进市区"恰恰是
    用户最想知道的那类话。

    和 Risk 共用 topic 枚举，这样同一个关注点上"有问题/没问题"能配对。
    """

    topic: RiskKind
    statement: str  # 给用户看的结论，一句话
    evidence: str  # 依据：具体数字或来源
    affected_segments: list[int]


class Risk(BaseModel):
    kind: RiskKind
    severity: Literal["blocker", "major", "minor"]
    evidence: str  # 必须引用具体数据，禁止笼统表述
    affected_segments: list[int]
    needs_user_input: bool  # True → 路由给澄清对话 agent
    prob: float | None
    cost_if_realized: Decimal | None
