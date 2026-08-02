from decimal import Decimal

import pytest
from pydantic import ValidationError

from conftest import itin, seg
from flight_assistant.models import Risk, TicketGroup


def test_stop_count_and_tickets():
    it = itin(
        [
            seg("MU", "MU587", "PVG", "NRT", "2026-09-26T09:00", "2026-09-26T12:30"),
            seg("UA", "UA882", "NRT", "ORD", "2026-09-26T16:00", "2026-09-26T14:00"),
        ]
    )
    assert it.stop_count == 1
    assert len(it.tickets) == 1


def test_baggage_unknown_is_none_not_false():
    """None 表示未知，和 False（明确不直挂）语义不同，必须能区分。"""
    t = TicketGroup(
        segment_idx=[0], baggage_through_checked=None, source_platform="qunar"
    )
    assert t.baggage_through_checked is None


def test_risk_rejects_unknown_kind():
    with pytest.raises(ValidationError):
        Risk(
            kind="something_made_up",
            severity="blocker",
            evidence="x",
            affected_segments=[0],
            needs_user_input=False,
            prob=None,
            cost_if_realized=None,
        )


def test_risk_accepts_decimal_cost():
    r = Risk(
        kind="mct_tight",
        severity="blocker",
        evidence="PVG T2→T1 衔接 45 分钟 < 官方 MCT 90 分钟",
        affected_segments=[0, 1],
        needs_user_input=False,
        prob=0.4,
        cost_if_realized=Decimal("3200"),
    )
    assert r.cost_if_realized == Decimal("3200")
