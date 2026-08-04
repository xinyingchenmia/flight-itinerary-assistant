"""成本预估和预算硬上限。

来由：第一次跑真实数据花了 $1.04，第二次 $1.81，全量 47 个候选推算要
$14——而这些数字都是**跑完才知道的**。没有事前预估等于没有成本控制。

做法：先跑一个最小批次测出真实单价，再据此推算全量；超预算就中止，
不是跑完再道歉。
"""

from dataclasses import dataclass


@dataclass
class CostEstimate:
    probe_cost: float  # 探测批次的实际花费
    probe_candidates: int  # 探测批次覆盖的候选数
    total_candidates: int  # 计划审查的候选总数
    batch_size: int

    @property
    def per_candidate(self) -> float:
        if not self.probe_candidates:
            return 0.0
        return self.probe_cost / self.probe_candidates

    @property
    def projected_total(self) -> float:
        """推算全量成本。

        按每候选单价线性外推是偏保守的（批量能摊薄 system prompt，批次
        越大单价越低），宁可估高不估低。
        """
        return self.per_candidate * self.total_candidates

    def render(self) -> str:
        return (
            f"探测 {self.probe_candidates} 个候选花费 ${self.probe_cost:.4f} "
            f"(${self.per_candidate:.4f}/候选) → "
            f"{self.total_candidates} 个推算 ${self.projected_total:.2f}"
        )


class BudgetExceeded(RuntimeError):
    """预算超限。中止而不是继续烧钱。"""


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
