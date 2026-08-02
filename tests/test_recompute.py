from conftest import itin, offer, seg
from flight_assistant.matching import build_itinerary_key, group_and_compare
from flight_assistant.models import Risk, TicketGroup
from flight_assistant.recompute import FieldUpdate, apply_updates, risks_needing_recheck


def _two_ticket_itin():
    segs = [
        seg("MU", "MU587", "PVG", "NRT", "2026-09-26T09:00", "2026-09-26T12:30"),
        seg("UA", "UA882", "NRT", "ORD", "2026-09-26T16:00", "2026-09-26T14:00"),
    ]
    return itin(
        segs,
        tickets=[
            TicketGroup(
                segment_idx=[0], baggage_through_checked=None, source_platform="ctrip"
            ),
            TicketGroup(
                segment_idx=[1], baggage_through_checked=None, source_platform="feizhu"
            ),
        ],
    )


def _direct_itin():
    return itin(
        [seg("UA", "UA850", "PVG", "ORD", "2026-09-26T15:30", "2026-09-26T17:05")]
    )


def _candidates():
    return group_and_compare(
        [
            (_two_ticket_itin(), offer("ctrip", "5200")),
            (_direct_itin(), offer("ctrip", "7800")),
        ]
    )


def test_untouched_candidate_keeps_same_object():
    """只重算受影响候选：未受影响的候选返回同一个对象引用。"""
    cands = _candidates()
    target_key = build_itinerary_key(_two_ticket_itin())
    other = next(c for c in cands if c.itinerary_key != target_key)

    updated, touched = apply_updates(
        cands,
        [FieldUpdate(target_key, "itinerary.tickets.0.baggage_through_checked", True)],
    )

    assert touched == {target_key}
    same = next(c for c in updated if c.itinerary_key == other.itinerary_key)
    assert same is other  # 没有重建对象，即没有重算


def test_update_writes_nested_field():
    cands = _candidates()
    target_key = build_itinerary_key(_two_ticket_itin())

    updated, _ = apply_updates(
        cands,
        [FieldUpdate(target_key, "itinerary.tickets.0.baggage_through_checked", True)],
    )

    changed = next(c for c in updated if c.itinerary_key == target_key)
    assert changed.itinerary.tickets[0].baggage_through_checked is True
    assert changed.itinerary.tickets[1].baggage_through_checked is None


def test_no_updates_touches_nothing():
    cands = _candidates()
    updated, touched = apply_updates(cands, [])
    assert touched == set()
    assert all(a is b for a, b in zip(updated, cands))


def test_risks_needing_recheck_only_affected():
    key_a = build_itinerary_key(_two_ticket_itin())
    key_b = build_itinerary_key(_direct_itin())
    risk = Risk(
        kind="no_through_baggage",
        severity="major",
        evidence="两张票分属 ctrip/feizhu，行李直挂未知",
        affected_segments=[0, 1],
        needs_user_input=True,
        prob=None,
        cost_if_realized=None,
    )
    recheck = risks_needing_recheck({key_a: [risk], key_b: []}, {key_a})
    assert set(recheck) == {key_a}
