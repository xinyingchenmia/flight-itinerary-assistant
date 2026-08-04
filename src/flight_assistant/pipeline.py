"""步骤 1-9 的编排。纯调用，不内联业务逻辑。

取数（步骤 3）不在这里跑——它必须在用户本机、用户自己的浏览器会话里
执行，入口是 scripts/fetch_live.py。这个函数接收已经取好的
(行程, 报价) 列表，从步骤 3.5 开始。
"""

from dataclasses import dataclass, field

from flight_assistant.budget import BudgetLedger
from flight_assistant.clarification.agent import AnswerFn, clarify
from flight_assistant.filtering import TripRequest, filter_and_sort
from flight_assistant.matching import group_and_compare
from flight_assistant.models import (
    Assurance,
    FlightPriceComparison,
    Itinerary,
    PlatformOffer,
    Risk,
    TripContext,
)
from flight_assistant.recompute import apply_updates, risks_needing_recheck
from flight_assistant.risk_review.agent import review_all


@dataclass
class Result:
    """步骤 9：最终结果。排序清单 + 依据 + 未知项标注。"""

    ranked: list[FlightPriceComparison]
    # {itinerary_key: (风险, 已确认没问题的项)}
    risks_by_key: dict[str, tuple[list[Risk], list[Assurance]]]
    trip_context: TripContext = field(default_factory=TripContext)
    missing_platforms: list[str] = field(default_factory=list)
    unresolved_unknowns: list[str] = field(default_factory=list)
    cost_usd: float = 0.0


async def run(
    req: TripRequest,
    fetched: list[tuple[Itinerary, PlatformOffer]],
    answer_fn: AnswerFn,
    missing_platforms: list[str] | None = None,
    trip_context: TripContext | None = None,
    budget: float = 0.5,
) -> Result:
    """budget 覆盖这次查询里所有花钱的调用（风险审查各批次 + 澄清各轮 +
    步骤 8 重审）。用尽时中止后续调用，已得到的结果照常返回。
    """
    ledger = BudgetLedger(cap=budget)
    # 步骤 3.5：跨平台匹配 + 比价（确定性）
    comparisons = group_and_compare(fetched)

    # 步骤 4：约束过滤 + 排序（确定性）
    ranked = filter_and_sort(comparisons, req)

    # 步骤 5：风险审查 agent
    risks_by_key = await review_all(ranked, trip_context=trip_context, ledger=ledger)

    # 步骤 6：澄清对话 agent（只处理 needs_user_input=True 的项）
    updates, trip_context = await clarify(
        risks_by_key, answer_fn, trip_context=trip_context, ledger=ledger
    )

    if updates:
        # 步骤 7：针对性重算——只重算受影响候选
        ranked, touched = apply_updates(ranked, updates)
        ranked = filter_and_sort(ranked, req)

        # 步骤 8：只对受影响候选重新审查
        recheck = risks_needing_recheck(risks_by_key, touched)
        if recheck:
            affected = [c for c in ranked if c.itinerary_key in touched]
            risks_by_key.update(
                await review_all(affected, trip_context=trip_context, ledger=ledger)
            )
    elif trip_context != TripContext():
        # 澄清没改行程字段，但补上了用户侧信息（国籍、落地后去哪等）。
        # 这些信息本身就会改变风险结论，所以全量重审一次。
        risks_by_key = await review_all(ranked, trip_context=trip_context, ledger=ledger)

    unresolved = [
        f"{key}: {r.kind} — {r.evidence}"
        for key, (risks, _) in risks_by_key.items()
        for r in risks
        if r.needs_user_input
    ]

    return Result(
        ranked=ranked,
        risks_by_key=risks_by_key,
        trip_context=trip_context or TripContext(),
        missing_platforms=missing_platforms or [],
        unresolved_unknowns=unresolved,
        cost_usd=ledger.spent,
    )
