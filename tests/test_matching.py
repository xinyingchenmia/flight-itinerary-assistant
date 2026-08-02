from decimal import Decimal

from conftest import itin, offer, seg
from flight_assistant.matching import build_itinerary_key, group_and_compare


def test_codeshare_same_flight_different_flight_no_groups_together():
    """核心用例：代码共享航班在不同平台显示的航班号不一致。

    同一架飞机、同承运方、同起降机场时刻，携程显示 UA7623、
    飞猪显示 UA0850。按航班号字符串匹配会误判成两趟不同航班；
    itinerary_key 用承运方+航段+时刻，必须归到同一组。
    """
    ctrip_view = itin(
        [seg("UA", "UA7623", "PVG", "ORD", "2026-09-26T15:30", "2026-09-26T17:05")]
    )
    feizhu_view = itin(
        [seg("UA", "UA0850", "PVG", "ORD", "2026-09-26T15:30", "2026-09-26T17:05")]
    )

    assert build_itinerary_key(ctrip_view) == build_itinerary_key(feizhu_view)

    comps = group_and_compare(
        [
            (ctrip_view, offer("ctrip", "6480")),
            (feizhu_view, offer("feizhu", "6120")),
        ]
    )
    assert len(comps) == 1
    assert len(comps[0].offers) == 2
    assert comps[0].cheapest_platform == "feizhu"
    assert comps[0].price_spread == Decimal("360")


def test_different_departure_time_is_different_itinerary():
    """时刻不同就是不同航班，不能因为承运方和机场相同就合并。"""
    morning = itin(
        [seg("MU", "MU587", "PVG", "ORD", "2026-09-26T09:00", "2026-09-26T11:00")]
    )
    evening = itin(
        [seg("MU", "MU587", "PVG", "ORD", "2026-09-26T21:00", "2026-09-26T23:00")]
    )
    assert build_itinerary_key(morning) != build_itinerary_key(evening)
    assert len(group_and_compare([(morning, offer("ctrip", "5000")), (evening, offer("ctrip", "4800"))])) == 2


def test_single_platform_spread_is_zero():
    it = itin([seg("CA", "CA985", "PVG", "LAX", "2026-09-26T13:00", "2026-09-26T09:30")])
    comps = group_and_compare([(it, offer("ctrip", "7200"))])
    assert comps[0].price_spread == Decimal("0")
    assert comps[0].cheapest_platform == "ctrip"


def test_multi_segment_order_matters():
    """航段顺序不同（去程/回程颠倒）应视为不同行程。"""
    a = itin(
        [
            seg("MU", "MU1", "PVG", "NRT", "2026-09-26T09:00", "2026-09-26T12:30"),
            seg("UA", "UA2", "NRT", "ORD", "2026-09-26T16:00", "2026-09-26T14:00"),
        ]
    )
    b = itin(
        [
            seg("UA", "UA2", "NRT", "ORD", "2026-09-26T16:00", "2026-09-26T14:00"),
            seg("MU", "MU1", "PVG", "NRT", "2026-09-26T09:00", "2026-09-26T12:30"),
        ]
    )
    assert build_itinerary_key(a) != build_itinerary_key(b)
