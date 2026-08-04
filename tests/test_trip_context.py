"""TripContext、衔接计算、以及字段写回的边界。

这些都对应真实跑 pipeline 时暴露出来的问题：
  - agent 想写 passenger.nationality（模型里没有这个路径）→ 直接 KeyError 崩掉
  - 用户侧信息没有落点，导致「落地后去哪」这类答案存不下来
"""

from datetime import date, datetime

import pytest

from conftest import itin, offer, seg
from flight_assistant.clarification.tools import (
    TRIP_CONTEXT_FIELDS,
    UpdateCollector,
    path_allowed,
)
from flight_assistant.matching import group_and_compare
from flight_assistant.models import Risk, TripContext
from flight_assistant.recompute import FieldUpdate, apply_updates
from flight_assistant.risk_review.agent import build_connections, build_context


def test_trip_context_defaults_are_unknown():
    """默认全未知——不能让缺省值冒充"已确认"。"""
    tc = TripContext()
    assert tc.nationality is None
    assert tc.destination_after_arrival is None
    assert tc.checked_bags is None
    assert tc.ground_transport_ok == "unknown"
    assert tc.visas == {}


def test_trip_context_accepts_real_values():
    tc = TripContext(
        nationality="CN",
        passport_expiry=date(2031, 5, 1),
        visas={"US": "B1/B2 有效至 2031"},
        destination_after_arrival="芝加哥 Loop 酒店",
        ground_transport_ok="public_only",
        checked_bags=1,
    )
    assert tc.visas["US"].startswith("B1/B2")
    assert tc.ground_transport_ok == "public_only"


def test_entry_connection_tight_is_valid_kind():
    """需入境/清关的中转和普通 mct_tight 分开——成因和量级都不同。"""
    r = Risk(
        kind="entry_connection_tight",
        severity="blocker",
        evidence="ORD 是本行程在美国的首个入境口岸，衔接 95 分钟，"
        "需入境审查+提取托运行李+海关+重新托运+二次安检",
        affected_segments=[0, 1],
        needs_user_input=False,
        prob=None,
        cost_if_realized=None,
    )
    assert r.kind == "entry_connection_tight"


# ---------------------------------------------------------------- 衔接计算


def _two_leg():
    return itin(
        [
            seg("MU", "MU523", "PVG", "NRT", "2026-09-26T09:00", "2026-09-26T12:50",
                dep_terminal="T1", arr_terminal="T2"),
            seg("UA", "UA882", "NRT", "ORD", "2026-09-26T13:30", "2026-09-26T11:20",
                dep_terminal="T1", arr_terminal="T5"),
        ]
    )


def test_connection_gap_computed_not_inferred():
    conns = build_connections(_two_leg())
    assert len(conns) == 1
    c = conns[0]
    assert c["at_airport"] == "NRT"
    assert c["gap_min"] == 40  # 12:50 → 13:30
    assert c["terminal_change"] is True  # T2 到达，T1 出发
    assert c["same_carrier"] is False


def test_terminal_change_unknown_when_terminal_missing():
    """航站楼未知时 terminal_change 必须是 None，不能默认 False——
    "不知道要不要换航站楼"和"确认不用换"是两种结论。
    """
    it = itin(
        [
            seg("TK", "TK027", "PVG", "IST", "2026-09-26T09:00", "2026-09-26T15:00"),
            seg("TK", "TK185", "IST", "ORD", "2026-09-26T18:00", "2026-09-26T21:00"),
        ]
    )
    assert build_connections(it)[0]["terminal_change"] is None


def test_direct_flight_has_no_connections():
    it = itin([seg("UA", "UA850", "PVG", "ORD", "2026-09-26T15:30", "2026-09-26T17:05")])
    assert build_connections(it) == []


def test_context_includes_trip_context_and_connections():
    comp = group_and_compare([(_two_leg(), offer("ctrip", "5200"))])[0]
    ctx = build_context(comp, TripContext(nationality="CN", checked_bags=0))
    assert ctx["trip_context"]["nationality"] == "CN"
    assert ctx["trip_context"]["checked_bags"] == 0
    assert ctx["connections"][0]["gap_min"] == 40


def test_context_without_trip_context_is_all_unknown():
    comp = group_and_compare([(_two_leg(), offer("ctrip", "5200"))])[0]
    ctx = build_context(comp)
    assert ctx["trip_context"]["nationality"] is None


# ---------------------------------------------------------------- 字段写回


def test_whitelist_rejects_invented_path():
    """实测 agent 写过 passenger.nationality，模型里没这个路径。"""
    assert not path_allowed("passenger.nationality")
    assert not path_allowed("itinerary.segments.0.dep_local")
    assert path_allowed("itinerary.tickets.0.baggage_through_checked")


def test_apply_updates_gives_readable_error_for_bad_path():
    """白名单是第一道拦截，recompute 是第二道——不能再抛裸 KeyError。"""
    comp = group_and_compare([(_two_leg(), offer("ctrip", "5200"))])[0]
    with pytest.raises(ValueError, match="不存在"):
        apply_updates([comp], [FieldUpdate(comp.itinerary_key, "passenger.nationality", "CN")])


def test_trip_context_collector_roundtrip():
    col = UpdateCollector()
    col.set_trip_field("nationality", "CN")
    col.set_trip_field("destination_after_arrival", "芝加哥 Loop")
    assert col.trip_context.nationality == "CN"
    assert col.trip_context.destination_after_arrival == "芝加哥 Loop"


def test_trip_context_collector_rejects_bad_enum():
    col = UpdateCollector()
    with pytest.raises(Exception):
        col.set_trip_field("ground_transport_ok", "随便")


def test_trip_context_fields_match_model():
    """白名单和模型必须同步，否则 agent 写了合法字段却被拒。"""
    model_fields = set(TripContext.model_fields)
    assert TRIP_CONTEXT_FIELDS <= model_fields
