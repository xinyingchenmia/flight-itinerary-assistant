"""确定性粗筛测试。

粗筛的作用是省钱（把明显干净的候选挡在 agent 之外），但它的失败模式很
危险：跳过一个真有 blocker 的候选，等价于让用户误机。所以测试重点是
**该送审的一定送审**，而不是"跳过得够多"。

另一条重点：粗筛不许含地理/政策知识。它只陈述"这是进入 XX 国的首个
落点"这类事实，"XX 国转机要不要入境"由 agent 判断——写死国家列表会把
工具限制在那几个国家。
"""

from conftest import itin, offer, seg
from flight_assistant.matching import group_and_compare
from flight_assistant.models import TicketGroup
from flight_assistant.screening import TIGHT_CONNECTION_MIN, screen, screen_all


def _comp(it):
    return group_and_compare([(it, offer("ctrip", "5000"))])[0]


def _clean_itin():
    """完全干净的候选：单票、行李直挂已确认、衔接 5.5 小时、同承运人、
    同航站楼、国内中转、白天到达。"""
    it = itin(
        [
            seg("MU", "MU1", "PVG", "PEK", "2026-09-26T08:00", "2026-09-26T11:00",
                dep_terminal="T1", arr_terminal="T2"),
            seg("MU", "MU2", "PEK", "CAN", "2026-09-26T16:30", "2026-09-26T20:00",
                dep_terminal="T2", arr_terminal="T1"),
        ],
        tickets=[
            TicketGroup(
                segment_idx=[0, 1],
                baggage_through_checked=True,
                source_platform="ctrip",
            )
        ],
    )
    for s in it.segments:
        s.dep_country = "CN"
        s.arr_country = "CN"
    return it


def test_clean_candidate_skipped():
    r = screen(_comp(_clean_itin()))
    assert r.needs_review is False
    assert any("均 ≥" in x for x in r.reasons)


def test_no_hardcoded_country_policy():
    """粗筛模块里不该出现国家政策表——那是 agent 的判断范围。"""
    from flight_assistant import screening

    src = open(screening.__file__).read()
    for token in ("ENTRY_REQUIRED", "AIRPORT_COUNTRY", '"US"', "'US'"):
        assert token not in src, f"粗筛里不该有 {token}"


def test_cross_border_transit_reviewed_without_policy_knowledge():
    """跨境中转一律送审，理由只陈述事实（进入 JP 的首个落点），
    不判断日本要不要入境。
    """
    it = _clean_itin()
    it.segments[0].arr_airport = "NRT"
    it.segments[0].arr_country = "JP"
    it.segments[1].dep_airport = "NRT"
    it.segments[1].dep_country = "JP"
    it.segments[1].arr_airport = "ORD"
    it.segments[1].arr_country = "US"

    r = screen(_comp(it))
    assert r.needs_review is True
    reason = next(x for x in r.reasons if "NRT" in x and "首个落点" in x)
    assert "JP" in reason
    # 不该出现任何"需入境""要清关"之类的判断
    assert not any("需入境" in x or "必须入境" in x for x in r.reasons)


def test_unknown_country_reviewed():
    """国别缺失就送审——不知道是不是跨境，保守处理。"""
    it = _clean_itin()
    it.segments[0].arr_country = None
    r = screen(_comp(it))
    assert r.needs_review is True
    assert any("国别未知" in x for x in r.reasons)


def test_tight_connection_reviewed():
    it = _clean_itin()
    it.segments[1].dep_local = it.segments[0].arr_local.replace(hour=12)
    r = screen(_comp(it))
    assert r.needs_review is True
    assert any(f"< {TIGHT_CONNECTION_MIN}" in x for x in r.reasons)


def test_late_arrival_reviewed():
    """深夜到达要送审——这是用户明确要的两类风险之一。"""
    it = _clean_itin()
    it.segments[-1].arr_local = it.segments[-1].arr_local.replace(hour=23, minute=40)
    r = screen(_comp(it))
    assert r.needs_review is True
    assert any("深夜时段" in x for x in r.reasons)


def test_split_ticket_reviewed():
    it = _clean_itin()
    it.tickets = [
        TicketGroup(segment_idx=[0], baggage_through_checked=True, source_platform="ctrip"),
        TicketGroup(segment_idx=[1], baggage_through_checked=True, source_platform="feizhu"),
    ]
    r = screen(_comp(it))
    assert r.needs_review is True
    assert any("无联程保护" in x for x in r.reasons)


def test_unknown_baggage_reviewed():
    """行李直挂未知就送审。真实携程数据里这一项全是 None，所以实践上
    这条会让绝大多数候选进 agent——这是有意的保守设计，也是为什么主要
    降本手段是批量调用而不是粗筛。
    """
    it = _clean_itin()
    it.tickets[0].baggage_through_checked = None
    assert screen(_comp(it)).needs_review is True


def test_unknown_terminal_reviewed():
    it = _clean_itin()
    it.segments[0].arr_terminal = None
    r = screen(_comp(it))
    assert r.needs_review is True
    assert any("航站楼信息缺失" in x for x in r.reasons)


def test_cross_carrier_reviewed():
    it = _clean_itin()
    it.segments[1].carrier = "CZ"
    assert screen(_comp(it)).needs_review is True


def test_screen_all_partitions_and_keeps_skip_reasons():
    clean = _comp(_clean_itin())
    dirty_it = _clean_itin()
    dirty_it.tickets[0].baggage_through_checked = None
    dirty = _comp(dirty_it)

    to_review, results = screen_all([clean, dirty])
    assert len(results) == 2
    assert len(to_review) == 1
    assert to_review[0].itinerary_key == dirty.itinerary_key
    skipped = [r for r in results if not r.needs_review]
    assert skipped and skipped[0].reasons  # 跳过也要留理由，供回查误跳
