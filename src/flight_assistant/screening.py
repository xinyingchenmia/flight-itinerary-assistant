"""确定性粗筛：判断哪些候选需要送 agent 审查。

动机是成本：agent 审查要花钱，全量候选都送不划算。但很多候选根本没有
可疑信号——单票、行李直挂已确认、衔接充裕、同航站楼、同承运人、白天
落地、没有跨境中转，这种送去审只会得到"无风险"。

## 边界：这里只判断"值不值得看"，不判断"有没有问题"

粗筛只用**从数据里直接算出来的结构性信号**，不含任何地理/政策知识：
不知道哪个国家转机要入境、不知道哪个机场换航站楼要多久、不知道哪个
城市几点没地铁。这些全是 agent 的判断范围。

具体说：代码算出"NRT 是进入 JP 的第一个落点，衔接 40 分钟，换航站楼"
这些事实，然后把候选交给 agent 去判断"这意味着什么"。代码不维护
"哪些国家需要入境提取行李"的表——那种表穷举不完，写死几个国家等于
把工具限制在那几个国家。

## 设计原则：宁可多送，不可漏掉

漏报等价于让用户误机，是文档里权重最高的指标；粗筛省下的钱远不值一次
漏报。所以规则一律是"有任何可疑信号就送审"，只有全部信号都干净才跳过。
每次跳过都记录理由，评测时可回查是否误跳。

## 实际效果的诚实说明

真实携程数据里 baggage_through_checked 全是 None（平台不给这个字段），
所以"行李直挂未知"这一条会让绝大多数候选都进 agent。也就是说粗筛在当
前数据条件下省不了多少钱——主要的降本手段是批量调用和限制候选数量，
不是这一层。等 v1 接入行李直挂数据源后，这一层的过滤率才会真正上来。
"""

from dataclasses import dataclass

from flight_assistant.models import FlightPriceComparison, TripContext

# 衔接时间阈值（分钟）。低于此值一律送审。
#
# 240 分钟是保守取值，不是某个机场的 MCT——代码不知道任何机场的 MCT。
# 取这么宽是因为需要入境清关的中转可能吃掉 90-180 分钟，再加延误余量。
# 超过 4 小时的衔接，无论在哪个国家、要不要入境，都很难说"时间不够"。
TIGHT_CONNECTION_MIN = 240

# 深夜到达时段（当地时间小时）。落地到走出航站楼还有 20-60 分钟，
# 所以 22 点落地也可能赶不上末班车。具体某个城市几点没车由 agent 判断。
LATE_ARRIVAL_HOURS = set(range(22, 24)) | set(range(0, 6))


@dataclass
class ScreenResult:
    candidate: FlightPriceComparison
    needs_review: bool
    reasons: list[str]  # 送审理由，或（needs_review=False 时）判定干净的依据


def screen(
    candidate: FlightPriceComparison, trip_context: TripContext | None = None
) -> ScreenResult:
    it = candidate.itinerary
    segs = it.segments
    reasons: list[str] = []

    # 1. 拆票：前段延误后段不保护
    if len(it.tickets) > 1:
        reasons.append(f"拆成 {len(it.tickets)} 张票，无联程保护")

    # 2. 行李直挂未知
    if any(t.baggage_through_checked is None for t in it.tickets):
        reasons.append("行李是否直挂未知")

    origin_country = segs[0].dep_country

    # 3. 逐个中转点，只记录事实
    for i in range(len(segs) - 1):
        arr, dep = segs[i], segs[i + 1]
        gap = int((dep.dep_local - arr.arr_local).total_seconds() // 60)
        at = arr.arr_airport

        # 3a. 跨境中转：这是进入该国的第一个落点吗？
        #     只陈述事实，不判断该国是否要求入境——那是 agent 的事。
        country = arr.arr_country
        if country is None:
            reasons.append(f"{at} 国别未知，无法判断是否跨境中转")
        elif country != origin_country:
            prior = {s.arr_country for s in segs[:i]}
            first = country not in prior
            reasons.append(
                f"{at} 是{'进入' + country + '的首个落点' if first else country + '境内再次中转'}"
                f"（衔接 {gap} 分钟）"
            )

        # 3b. 衔接偏紧
        if gap < TIGHT_CONNECTION_MIN:
            reasons.append(f"{at} 衔接 {gap} 分钟 < {TIGHT_CONNECTION_MIN} 分钟阈值")

        # 3c. 换航站楼，或航站楼未知
        if arr.arr_terminal is None or dep.dep_terminal is None:
            reasons.append(f"{at} 航站楼信息缺失，无法判断是否需要转场")
        elif arr.arr_terminal != dep.dep_terminal:
            reasons.append(
                f"{at} 换航站楼 {arr.arr_terminal}→{dep.dep_terminal}（衔接 {gap} 分钟）"
            )

        # 3d. 跨承运人
        if arr.carrier != dep.carrier:
            reasons.append(f"{at} 跨承运人 {arr.carrier}→{dep.carrier}")

    # 4. 深夜/凌晨到达终点
    final = segs[-1]
    if final.arr_local.hour in LATE_ARRIVAL_HOURS:
        reasons.append(
            f"{final.arr_airport} 到达当地时间 {final.arr_local:%H:%M}（深夜时段）"
        )

    if not reasons:
        gaps = [
            int((segs[i + 1].dep_local - segs[i].arr_local).total_seconds() // 60)
            for i in range(len(segs) - 1)
        ]
        clean = ["单票联程", "行李直挂已确认"]
        if gaps:
            clean.append(f"各段衔接 {gaps} 分钟均 ≥ {TIGHT_CONNECTION_MIN}")
        clean.append(f"到达 {final.arr_local:%H:%M} 非深夜")
        clean.append("无跨境中转")
        return ScreenResult(candidate, False, clean)

    return ScreenResult(candidate, True, reasons)


def screen_all(
    candidates: list[FlightPriceComparison],
    trip_context: TripContext | None = None,
) -> tuple[list[FlightPriceComparison], list[ScreenResult]]:
    """返回 (需要送审的候选, 全部粗筛结果)。

    第二个返回值保留了跳过的理由——评测时要能回查"跳过的那些里有没有
    真的 blocker"，这是误跳率的数据来源。
    """
    results = [screen(c, trip_context) for c in candidates]
    return [r.candidate for r in results if r.needs_review], results
