"""预算账本测试。

「每次查询所有路径不超过 $0.50」这个保证必须能被确定性地验证，不能只靠
跑一次运气好。这里测的是账本的约束行为本身，不花任何 API 钱。
"""

import pytest

from flight_assistant.budget import (
    BudgetExceeded,
    BudgetLedger,
    CostEstimate,
)
from flight_assistant.budget import check as budget_check


def test_empty_ledger_allows_first_call():
    led = BudgetLedger(cap=0.5)
    led.guard("首次")  # 不该抛
    assert led.spent == 0.0
    assert led.remaining == 0.5


def test_soft_cap_leaves_reserve_for_in_flight_call():
    """不能花到 cap 才停——最后一次调用的成本要等响应才知道，
    花到上限才停必然超支。
    """
    led = BudgetLedger(cap=0.5, reserve_ratio=0.25)
    assert led.soft_cap == pytest.approx(0.375)

    led.record("审查", 0.38)
    with pytest.raises(BudgetExceeded, match="软上限"):
        led.guard("下一次")


def test_guard_projects_using_worst_observed_call():
    """用观测到的最贵一次外推，不用平均值——预算判断上保守比精确重要。"""
    led = BudgetLedger(cap=0.5)
    led.record("批次1", 0.05)
    led.record("批次2", 0.20)  # 最贵
    assert led.typical_call_cost() == 0.20

    led.guard("再一次")  # 0.25 + 0.20 = 0.45 < 0.5，放行
    with pytest.raises(BudgetExceeded, match="超出本阶段上限"):
        led.guard("再两次", expected_calls=2)  # 0.25 + 0.40 > 0.5


def _ledger_spent_036() -> BudgetLedger:
    """已花 $0.36，但单次调用只 $0.09——用来隔离 reserve 的作用，
    避免被"按最贵一次外推"那条规则先拦掉。
    """
    led = BudgetLedger(cap=0.5, reserve_ratio=0.1)
    for i in range(4):
        led.record(f"审查批次{i + 1}", 0.09, candidates=2)
    return led


def test_reserve_protects_later_stage():
    """实测教训：风险审查把 $0.50 里的 $0.43 花光，澄清对话一轮都没跑起来。
    "在预算内"是靠砍掉功能换来的，不算达标。风险审查必须带 reserve 调用。
    """
    led = _ledger_spent_036()
    assert led.spent == pytest.approx(0.36)

    # 不带 reserve：软上限 0.45，还能继续花
    led.guard("审查下一批")

    # 带 reserve=0.15：本阶段上限降到 0.35，已花 0.36 就该停手
    with pytest.raises(BudgetExceeded, match="预留给后续阶段"):
        led.guard("审查下一批", reserve=0.15)


def test_reserve_leaves_room_for_clarify_turns():
    """审查阶段被 reserve 拦住之后，澄清阶段仍然有额度可用——
    这才是"两个阶段都跑得起来"。
    """
    led = _ledger_spent_036()

    with pytest.raises(BudgetExceeded):
        led.guard("审查", reserve=0.15)

    # 澄清不带 reserve，用预留出来的那部分
    led.guard("澄清第 1 轮")
    led.record("澄清第 1 轮", 0.06)
    assert led.spent == pytest.approx(0.42)
    assert led.spent <= led.cap


def test_concurrent_batches_checked_upfront():
    """并发批次会在各自 record 之前全部发出，所以必须按批次数预判。"""
    led = BudgetLedger(cap=0.5)
    led.record("探测批次", 0.12)
    # 还剩 0.38，一次 0.12，3 个批次 0.36 → 放行
    led.guard("3 个批次", expected_calls=3)
    # 4 个批次 0.48，加上已花的 0.12 = 0.60 → 拒绝
    with pytest.raises(BudgetExceeded):
        led.guard("4 个批次", expected_calls=4)


def test_all_paths_counted_not_just_review():
    """账本必须覆盖澄清对话——之前那条路径完全没有预算约束，
    所谓"每次查询不超过 X"是假的。
    """
    led = BudgetLedger(cap=0.5)
    led.record("风险审查(8 个)", 0.15, candidates=8)
    for i in range(3):
        led.record(f"澄清第 {i + 1} 轮", 0.06)
    assert led.spent == pytest.approx(0.33)
    assert "澄清第 3 轮" in led.render()
    assert "风险审查" in led.render()


def test_render_flags_unreported_costs():
    """SDK 没报成本时记 0，但那不代表免费——账本会失去约束力，
    必须让调用方看见。
    """
    led = BudgetLedger(cap=0.5)
    led.record_from_meta("批次", {"total_cost_usd": None, "batch_size": 8})
    assert led.unreported_calls == 1
    assert "账本可能低估" in led.render()


def test_record_from_meta_reads_batch_size():
    led = BudgetLedger(cap=0.5)
    led.record_from_meta("批次", {"total_cost_usd": 0.16, "batch_size": 8})
    assert led.spent == pytest.approx(0.16)
    assert "$0.0200/候选" in led.render()


def test_per_candidate_shown_only_for_batches():
    led = BudgetLedger(cap=0.5)
    led.record("单条", 0.05, candidates=1)
    assert "/候选" not in led.render()


# ---------------------------------------------------------------- 事前推算


def test_estimate_projects_from_probe():
    est = CostEstimate(
        probe_cost=0.16, probe_candidates=8, total_candidates=47, batch_size=8
    )
    assert est.per_candidate == pytest.approx(0.02)
    assert est.projected_total == pytest.approx(0.94)


def test_estimate_blocks_when_over_budget():
    est = CostEstimate(
        probe_cost=0.16, probe_candidates=8, total_candidates=47, batch_size=8
    )
    with pytest.raises(BudgetExceeded, match="超出预算"):
        budget_check(est, 0.5)
    budget_check(est, 1.0)  # 1.0 够，不该抛


def test_estimate_handles_zero_probe():
    """探测批次没返回成本时不该除零，也不该假装免费放行。"""
    est = CostEstimate(
        probe_cost=0.0, probe_candidates=0, total_candidates=47, batch_size=8
    )
    assert est.per_candidate == 0.0
    assert est.projected_total == 0.0


def test_stage_costs_do_not_cross_contaminate():
    """实测 bug：审查一次 $0.37，澄清一轮只 $0.03。混在一起外推时，
    守卫用审查的单价预测澄清的成本，$0.37+$0.37 超上限 → 澄清对话被
    整个跳过，一个问题都没问。必须按阶段分开统计。
    """
    led = BudgetLedger(cap=0.5)
    led.record("风险审查(4 个)", 0.37, candidates=4, kind="review")

    # 同阶段外推：再来一次审查会超，拦住是对的
    with pytest.raises(BudgetExceeded):
        led.guard("再审一批", kind="review")

    # 跨阶段不该被审查的单价污染：澄清没有历史，应当放行
    led.guard("澄清第 1 轮", kind="clarify")
    led.record("澄清第 1 轮", 0.03, kind="clarify")

    assert led.typical_call_cost("review") == pytest.approx(0.37)
    assert led.typical_call_cost("clarify") == pytest.approx(0.03)
    # 不带 kind 时看全部，仍是最贵那次
    assert led.typical_call_cost() == pytest.approx(0.37)


def test_clarify_turns_bounded_by_own_history():
    """澄清阶段自己的历史照样要起约束作用。"""
    led = BudgetLedger(cap=0.5)
    led.record("风险审查", 0.30, kind="review")
    for i in range(2):
        led.guard(f"澄清第 {i + 1} 轮", kind="clarify")
        led.record(f"澄清第 {i + 1} 轮", 0.05, kind="clarify")

    assert led.spent == pytest.approx(0.40)
    # 0.40 已超软上限 0.375，即使澄清单轮很便宜也该停
    with pytest.raises(BudgetExceeded):
        led.guard("澄清第 3 轮", kind="clarify")
