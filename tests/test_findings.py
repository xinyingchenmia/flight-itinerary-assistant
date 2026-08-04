"""Assurance / PreferenceNote / 软偏好排序的测试。

来由：
- 只输出 Risk 时，用户永远只看到坏消息。实测 agent 联网查到「CTA 蓝线
  24 小时运营」，这个正面结论只体现为"风险降级"，被丢掉了。
- "转机的地方别太无聊"这类主观偏好有两种相反解读（想逛 vs 嫌烦），
  猜错排序会完全反过来，所以代码不许替用户解读。
"""

from datetime import datetime

import pytest
from pydantic import ValidationError

from conftest import itin, offer, seg
from flight_assistant.filtering import TripRequest, filter_and_sort, total_layover_min
from flight_assistant.matching import group_and_compare
from flight_assistant.models import Assurance, PreferenceNote, TripContext
from flight_assistant.risk_review.agent import build_context


def test_assurance_shares_topic_enum_with_risk():
    """同一个关注点上"有问题/没问题"要能配对，所以共用枚举。"""
    a = Assurance(
        topic="arrival_no_ground_transit",
        statement="半夜 00:14 落地 ORD 也能进市区",
        evidence="CTA 官网：蓝线 O'Hare 站 24 小时运营",
        affected_segments=[1],
    )
    assert a.topic == "arrival_no_ground_transit"

    with pytest.raises(ValidationError):
        Assurance(
            topic="terminal_has_good_food",  # 不在枚举里
            statement="x",
            evidence="y",
            affected_segments=[],
        )


def test_preference_note_has_no_severity():
    """主观偏好没有 blocker/major/minor 的语义——"中转地没什么好逛的"
    不是故障，不该混进 Risk 的严重程度体系。
    """
    n = PreferenceNote(
        preference="转机的地方别太无聊",
        verdict="good",
        statement="HND T3 有观景台和江户小路餐饮区，295 分钟够逛",
        evidence="成田/羽田官网航站楼设施页",
        affected_segments=[0, 1],
    )
    assert not hasattr(n, "severity")
    assert n.verdict == "good"


def test_preference_verdict_unknown_is_allowed():
    """查不到就写 unknown，不许凭印象说某个机场好玩。"""
    n = PreferenceNote(
        preference="转机的地方别太无聊",
        verdict="unknown",
        statement="没查到 DFW D 航站楼的设施信息",
        evidence="搜索未找到一手来源",
        affected_segments=[0],
    )
    assert n.verdict == "unknown"


# ------------------------------------------------------- 软偏好不被代码解读


def test_layover_preference_defaults_unknown():
    """代码不许替用户拍解读。默认 unknown，等澄清 agent 去问。"""
    assert TripContext().layover_preference == "unknown"


def test_soft_preferences_passed_through_verbatim():
    """原话原样传给 agent，代码不解释含义。"""
    it = itin(
        [
            seg("NH", "NH1", "PVG", "HND", "2026-09-26T09:00", "2026-09-26T13:00"),
            seg("NH", "NH2", "HND", "ORD", "2026-09-26T18:00", "2026-09-26T16:00"),
        ]
    )
    comp = group_and_compare([(it, offer("ctrip", "9000"))])[0]
    ctx = build_context(comp, TripContext(), ["转机的地方别太无聊"])
    assert ctx["soft_preferences"] == ["转机的地方别太无聊"]


def test_context_without_soft_prefs_is_empty_list():
    it = itin([seg("UA", "UA1", "PVG", "ORD", "2026-09-26T15:00", "2026-09-26T17:00")])
    comp = group_and_compare([(it, offer("ctrip", "7000"))])[0]
    assert build_context(comp)["soft_preferences"] == []


# ------------------------------------------------------- 中转时长排序


def _two_stop_variants():
    """两个候选：一个中转 60 分钟，一个中转 300 分钟。总时长相同。"""
    short = itin(
        [
            seg("MU", "MU1", "PVG", "NRT", "2026-09-26T08:00", "2026-09-26T11:00"),
            seg("MU", "MU2", "NRT", "ORD", "2026-09-26T12:00", "2026-09-26T20:00"),
        ],
        duration=720,
    )
    long_ = itin(
        [
            seg("NH", "NH1", "PVG", "HND", "2026-09-26T08:00", "2026-09-26T11:00"),
            seg("NH", "NH2", "HND", "ORD", "2026-09-26T16:00", "2026-09-27T00:00"),
        ],
        duration=720,
    )
    return group_and_compare(
        [(short, offer("ctrip", "9000")), (long_, offer("ctrip", "6000"))]
    )


def test_total_layover_computed_by_code():
    comps = _two_stop_variants()
    layovers = sorted(total_layover_min(c) for c in comps)
    assert layovers == [60, 300]


def test_layover_sort_puts_short_connection_first():
    """确认成 shorter 之后，排序是纯代码的事，不需要 agent。"""
    req = TripRequest(
        origin="PVG", dest="ORD", date="2026-09-26", sort_pref="layover"
    )
    ranked = filter_and_sort(_two_stop_variants(), req)
    assert total_layover_min(ranked[0]) == 60


def test_price_sort_still_prefers_cheaper_long_layover():
    """同一批候选，按价格排会把长中转那个排前面——这正是"猜错解读就
    排序反过来"的具体体现。
    """
    req = TripRequest(origin="PVG", dest="ORD", date="2026-09-26", sort_pref="price")
    ranked = filter_and_sort(_two_stop_variants(), req)
    assert total_layover_min(ranked[0]) == 300


def test_direct_flight_has_zero_layover():
    it = itin([seg("UA", "UA1", "PVG", "ORD", "2026-09-26T15:00", "2026-09-26T17:00")])
    comp = group_and_compare([(it, offer("ctrip", "7000"))])[0]
    assert total_layover_min(comp) == 0


def test_layover_preference_is_writable_by_clarify_agent():
    """澄清 agent 消歧后要能把结果写回去，否则问了也没用。"""
    from flight_assistant.clarification.tools import TRIP_CONTEXT_FIELDS, UpdateCollector

    assert "layover_preference" in TRIP_CONTEXT_FIELDS
    col = UpdateCollector()
    col.set_trip_field("layover_preference", "explore")
    assert col.trip_context.layover_preference == "explore"


def test_layover_preference_rejects_free_text():
    from flight_assistant.clarification.tools import UpdateCollector

    col = UpdateCollector()
    with pytest.raises(Exception):
        col.set_trip_field("layover_preference", "想逛逛吧大概")
