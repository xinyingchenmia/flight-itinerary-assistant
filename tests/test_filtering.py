from conftest import itin, offer, seg
from flight_assistant.filtering import TripRequest, filter_and_sort, filter_candidates
from flight_assistant.matching import group_and_compare


def _direct():
    return itin(
        [seg("UA", "UA850", "PVG", "ORD", "2026-09-26T15:30", "2026-09-26T17:05")],
        duration=875,
    )


def _one_stop():
    return itin(
        [
            seg("MU", "MU587", "PVG", "NRT", "2026-09-26T09:00", "2026-09-26T12:30"),
            seg("UA", "UA882", "NRT", "ORD", "2026-09-26T16:00", "2026-09-26T14:00"),
        ],
        duration=1140,
    )


def _two_stop():
    return itin(
        [
            seg("MU", "MU1", "PVG", "ICN", "2026-09-26T08:00", "2026-09-26T11:00"),
            seg("KE", "KE2", "ICN", "SEA", "2026-09-26T14:00", "2026-09-26T08:00"),
            seg("AS", "AS3", "SEA", "ORD", "2026-09-26T11:00", "2026-09-26T17:00"),
        ],
        duration=1500,
    )


def _comps():
    return group_and_compare(
        [
            (_direct(), offer("ctrip", "7800")),
            (_one_stop(), offer("feizhu", "5200")),
            (_two_stop(), offer("qunar", "4300")),
        ]
    )


def test_max_stops_filters_out_two_stop():
    req = TripRequest(origin="PVG", dest="ORD", date="2026-09-26", max_stops=1)
    kept = filter_candidates(_comps(), req)
    assert {c.itinerary.stop_count for c in kept} == {0, 1}


def test_max_stops_none_keeps_everything():
    req = TripRequest(origin="PVG", dest="ORD", date="2026-09-26", max_stops=None)
    assert len(filter_candidates(_comps(), req)) == 3


def test_sort_by_duration_puts_direct_first():
    req = TripRequest(
        origin="PVG", dest="ORD", date="2026-09-26", max_stops=2, sort_pref="duration"
    )
    ranked = filter_and_sort(_comps(), req)
    assert ranked[0].itinerary.stop_count == 0
    assert [c.itinerary.total_duration_min for c in ranked] == [875, 1140, 1500]


def test_sort_by_price_puts_cheapest_first():
    req = TripRequest(
        origin="PVG", dest="ORD", date="2026-09-26", max_stops=2, sort_pref="price"
    )
    ranked = filter_and_sort(_comps(), req)
    assert ranked[0].cheapest_platform == "qunar"


def test_filter_then_sort_combined():
    """转机≤1 + 总时长优先 —— 文档里的示例查询。"""
    req = TripRequest(
        origin="PVG", dest="ORD", date="2026-09-26", max_stops=1, sort_pref="duration"
    )
    ranked = filter_and_sort(_comps(), req)
    assert len(ranked) == 2
    assert ranked[0].itinerary.total_duration_min == 875
