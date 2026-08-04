"""用真实携程数据跑通「解析 → 匹配比价 → 过滤排序」整条确定性链路。

不含 agent（那部分要 API key，见 scripts/try_risk_review.py），
但覆盖了步骤 3.5 和步骤 4 的全部代码路径。
"""

import json
from datetime import datetime
from pathlib import Path

from flight_assistant.fetchers.ctrip_parse import parse_batch_search
from flight_assistant.filtering import TripRequest, filter_and_sort
from flight_assistant.matching import group_and_compare
from flight_assistant.risk_review.agent import build_context

FIXTURE = Path(__file__).parent / "fixtures" / "ctrip_batchsearch_sample.json"


def _pipeline(sort_pref="price", max_stops=None):
    payload = json.loads(FIXTURE.read_text())
    fetched = parse_batch_search(payload, fetched_at=datetime(2026, 8, 4))
    comparisons = group_and_compare(fetched)
    req = TripRequest(
        origin="PVG",
        dest="ORD",
        date="2026-09-27",
        max_stops=max_stops,
        sort_pref=sort_pref,
    )
    return filter_and_sort(comparisons, req)


def test_pipeline_produces_ranked_candidates():
    ranked = _pipeline()
    assert len(ranked) == 3
    prices = [min(o.price for o in c.offers) for c in ranked]
    assert prices == sorted(prices)  # 按最低价升序


def test_duration_sort_differs_from_price_sort():
    by_price = [c.itinerary_key for c in _pipeline(sort_pref="price")]
    by_duration = [c.itinerary_key for c in _pipeline(sort_pref="duration")]
    durations = [
        c.itinerary.total_duration_min for c in _pipeline(sort_pref="duration")
    ]
    assert durations == sorted(durations)
    # 这批真实数据里价格最低的不是最快的，两种排序结果应该不同
    assert by_price != by_duration


def test_max_stops_zero_filters_everything():
    """这次搜索 PVG→ORD 没有直飞结果，转机≤0 应该过滤到空。

    这是真实数据的性质，不是构造出来的——47 条候选全是 1 次中转。
    """
    assert _pipeline(max_stops=0) == []


def test_risk_context_carries_computed_facts():
    """传给风险审查 agent 的上下文必须含代码算好的联程保护字段，
    而不是让 agent 自己推断。
    """
    ranked = _pipeline()
    ctx = build_context(ranked[0])
    assert ctx["ticket_count"] == 1
    assert ctx["has_through_protection"] is True
    assert ctx["baggage_through_checked"] == [None]  # 未知，必须 flag
    assert len(ctx["segments"]) == 2
    assert ctx["stop_count"] == 1


def test_codeshare_context_uses_operating_carrier():
    """喂给 agent 的航段数据里，carrier 应是实际承运方——
    否则 agent 判断联盟/中转保护时会用错航司。
    """
    ranked = _pipeline()
    cs = next(
        c
        for c in ranked
        if any(s.flight_no == "LH9152" for s in c.itinerary.segments)
    )
    ctx = build_context(cs)
    leg = next(s for s in ctx["segments"] if s["flight_no"] == "LH9152")
    assert leg["carrier"] == "UA"
