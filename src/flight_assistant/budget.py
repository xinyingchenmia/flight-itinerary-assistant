"""成本账本和预算硬上限。

来由：第一次跑真实数据花了 $1.04，第二次 $1.81，全量 47 个候选推算 $14——
而这些数字都是**跑完才知道的**。没有事前控制等于没有成本控制。

## 保证的边界（重要）

BudgetLedger 在**每次调用 agent 之前**检查累计花费，不够就抛
BudgetExceeded。它管得住"要不要再发一次请求"，管不住"已经发出去的这次
请求花多少"——那个数字要等响应回来才知道。

所以留了 reserve：上限 $0.50 时，花到约 $0.40 就不再发起新调用，剩下的
额度是给已经在途的那次调用兜底的。实际总花费会 ≤ 上限，前提是单次调用
的成本不超过 reserve。单次调用异常昂贵时仍可能略微超出——这种情况会在
账本里留记录，不会静默发生。

## 覆盖范围

必须覆盖一次查询里**所有**会花钱的路径，不是只有风险审查：
  - 步骤 5 风险审查（可能多个批次）
  - 步骤 6 澄清对话（多轮，每轮都花钱）
  - 步骤 8 受影响候选的重新审查
漏掉任何一条，"每次查询不超过 X"就是假的。
"""

from dataclasses import dataclass, field


class BudgetExceeded(RuntimeError):
    """预算用尽。中止而不是继续烧钱。"""


@dataclass
class CallRecord:
    label: str
    cost: float
    candidates: int = 1
    # 阶段。成本量级按阶段差很多：一次批量风险审查 $0.37，一轮澄清对话
    # $0.01-0.06。混在一起做外推会导致用审查的单价去预测澄清的成本，
    # 量级差 10 倍——实测因此把澄清对话整个跳过了。
    kind: str = "default"


@dataclass
class BudgetLedger:
    """一次查询的成本账本。所有花钱的调用都必须经过它。"""

    cap: float
    # 给在途调用留的余量比例。单次调用的成本要等响应才知道，所以不能
    # 花到 cap 才停——那样最后一次调用必然超支。
    #
    # 0.12 而不是 0.25：实测 0.25 时，审查花掉 $0.322/$0.50 后，澄清第一轮
    # 花 $0.15 使累计到 $0.473 > 软上限 $0.375，于是第二轮被掐——答案已经
    # 发出去但 agent 没机会落盘，闭环断在最后一步。余量要够一轮澄清用。
    reserve_ratio: float = 0.12
    calls: list[CallRecord] = field(default_factory=list)

    @property
    def spent(self) -> float:
        return sum(c.cost for c in self.calls)

    @property
    def remaining(self) -> float:
        return self.cap - self.spent

    @property
    def soft_cap(self) -> float:
        """不再发起新调用的阈值。"""
        return self.cap * (1 - self.reserve_ratio)

    def typical_call_cost(self, kind: str | None = None) -> float:
        """该阶段已观测到的最贵一次调用。用最大值而不是平均值——预算判断上
        保守比精确重要。

        kind 为 None 时看全部记录；给定 kind 时只看同阶段的记录。跨阶段
        外推会严重高估（审查 $0.37 vs 澄清 $0.03），把后续阶段误杀。
        """
        pool = [c for c in self.calls if kind is None or c.kind == kind]
        return max((c.cost for c in pool), default=0.0)

    def guard(
        self,
        label: str,
        expected_calls: int = 1,
        reserve: float = 0.0,
        kind: str = "default",
    ) -> None:
        """在发起调用前检查。不够就抛异常。

        reserve 是必须留给后续阶段的额度。实测教训：风险审查把 $0.50 里的
        $0.43 花光后，澄清对话一轮都跑不了——"在预算内"是靠砍掉功能换来的。
        风险审查应当带 reserve 调用，把澄清对话的额度先留出来。
        """
        effective_cap = self.cap - reserve
        effective_soft = min(self.soft_cap, effective_cap)

        if self.spent >= effective_soft:
            detail = f"（总上限 ${self.cap:.2f}"
            if reserve:
                detail += f"，其中 ${reserve:.2f} 预留给后续阶段"
            detail += "，另留余量给在途调用）"
            raise BudgetExceeded(
                f"{label}: 已花费 ${self.spent:.4f}，达到本阶段上限 "
                f"${effective_soft:.4f}{detail}。\n{self.render()}"
            )

        # 只用同阶段的历史做外推。没有同阶段历史时不拦——让它先跑一次，
        # 拿到真实成本后再约束。
        typical = self.typical_call_cost(kind)
        if typical:
            projected = self.spent + typical * expected_calls
            if projected > effective_cap:
                raise BudgetExceeded(
                    f"{label}: 已花费 ${self.spent:.4f}，再做 {expected_calls} 次"
                    f"调用预计到 ${projected:.4f}，超出本阶段上限 "
                    f"${effective_cap:.4f}。\n{self.render()}"
                )

    def record(
        self,
        label: str,
        cost: float,
        candidates: int = 1,
        kind: str = "default",
    ) -> None:
        self.calls.append(CallRecord(label, cost, candidates, kind))

    def record_from_meta(
        self, label: str, meta: dict, kind: str = "default"
    ) -> None:
        """从 agent 返回的元信息里取成本。

        没有 total_cost_usd 时记 0 并不代表免费——只是 SDK 没报。这种
        情况下账本会失去约束力，调用方应当感知到（render 里会标出来）。
        """
        self.record(
            label,
            float(meta.get("total_cost_usd") or 0.0),
            int(meta.get("batch_size", 1)),
            kind,
        )

    @property
    def unreported_calls(self) -> int:
        return sum(1 for c in self.calls if c.cost == 0.0)

    def render(self) -> str:
        lines = [
            f"成本账本: ${self.spent:.4f} / 上限 ${self.cap:.2f} "
            f"(软上限 ${self.soft_cap:.4f})"
        ]
        for c in self.calls:
            per = f" (${c.cost / c.candidates:.4f}/候选)" if c.candidates > 1 else ""
            lines.append(f"  {c.label}: ${c.cost:.4f}{per}")
        if self.unreported_calls:
            lines.append(
                f"  ⚠ {self.unreported_calls} 次调用没有返回成本数据，"
                "账本可能低估"
            )
        return "\n".join(lines)


@dataclass
class CostEstimate:
    """用小批次探真实单价，推算全量成本。"""

    probe_cost: float
    probe_candidates: int
    total_candidates: int
    batch_size: int

    @property
    def per_candidate(self) -> float:
        if not self.probe_candidates:
            return 0.0
        return self.probe_cost / self.probe_candidates

    @property
    def projected_total(self) -> float:
        """按单价线性外推。偏保守——批量能摊薄 system prompt，批次越大
        单价越低，所以实际会比推算的低。
        """
        return self.per_candidate * self.total_candidates

    def render(self) -> str:
        return (
            f"探测 {self.probe_candidates} 个候选花费 ${self.probe_cost:.4f} "
            f"(${self.per_candidate:.4f}/候选) → "
            f"{self.total_candidates} 个推算 ${self.projected_total:.2f}"
        )


def check(estimate: CostEstimate, budget: float) -> None:
    if estimate.projected_total > budget:
        raise BudgetExceeded(
            f"{estimate.render()}，超出预算 ${budget:.2f}。\n"
            f"可选：减少 --limit（当前 {estimate.total_candidates}）、"
            f"加大 --batch-size（当前 {estimate.batch_size}，批量越大单价越低）、"
            f"或换更便宜的模型 --model claude-sonnet-5。"
        )


def cost_of(stats: list[dict]) -> float:
    return sum(s.get("total_cost_usd", 0.0) for s in stats)


def candidates_covered(stats: list[dict]) -> int:
    """stats 里已审查的候选数。批量条目记的是 batch_size，单条记 1。"""
    return sum(s.get("batch_size", 1) for s in stats)
