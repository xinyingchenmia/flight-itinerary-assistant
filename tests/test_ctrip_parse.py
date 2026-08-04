"""携程 batchSearch 解析测试。

fixture 是从 2026-08-04 真实响应里裁出来的 3 条行程（去掉了 token、
优惠券和营销文案，保留解析用到的全部字段）：
  - LH733/LH9152：代码共享，LH9152 实际由 UA945 执飞
  - TK027/TK185：部分航段缺 departureTerminal
  - UA199/UA1962：普通两段行程
"""

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from flight_assistant.fetchers.ctrip_parse import parse_batch_search
from flight_assistant.matching import build_itinerary_key, group_and_compare

FIXTURE = Path(__file__).parent / "fixtures" / "ctrip_batchsearch_sample.json"


@pytest.fixture
def payload() -> dict:
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def parsed(payload):
    return parse_batch_search(payload, fetched_at=datetime(2026, 8, 4, 19, 31))


def test_parses_all_offers(parsed):
    # 3 条行程 × 每条 2 个运价 = 6 个报价
    assert len(parsed) == 6
    assert {o.platform for _, o in parsed} == {"ctrip"}


def test_price_is_tax_inclusive(payload, parsed):
    """price = adultPrice + adultTax。核对依据：该口径下全量最低价 5212，
    与页面「¥5212起 含税价」一致；单用 adultPrice 最低 2627，与页面不符。
    """
    first = payload["data"]["flightItineraryList"][0]["priceList"][0]
    expected = Decimal(str(first["adultPrice"])) + Decimal(str(first["adultTax"]))
    assert parsed[0][1].price == expected


def test_codeshare_uses_operating_carrier(parsed):
    """LH9152 由 UA945 实际执飞 → carrier 取 UA，flight_no 保留 LH9152。

    这是 itinerary_key 不被代码共享误判的前提。
    """
    it = next(i for i, _ in parsed if any(s.flight_no == "LH9152" for s in i.segments))
    leg = next(s for s in it.segments if s.flight_no == "LH9152")
    assert leg.carrier == "UA"  # 实际承运方
    assert leg.flight_no == "LH9152"  # 携程展示的市场航班号

    # 同一行程的第一段没有 operate* 字段，回落到 marketAirlineCode
    first = it.segments[0]
    assert first.flight_no == "LH733"
    assert first.carrier == "LH"


def test_codeshare_key_matches_operating_view(parsed):
    """如果另一个平台把这趟航班显示成 UA945（实际承运方的航班号），
    itinerary_key 必须和携程视角算出的一致。
    """
    ctrip_view = next(
        i for i, _ in parsed if any(s.flight_no == "LH9152" for s in i.segments)
    )
    other_view = ctrip_view.model_copy(deep=True)
    other_view.segments[1].flight_no = "UA945"  # 只改航班号字符串

    assert build_itinerary_key(ctrip_view) == build_itinerary_key(other_view)


def test_missing_terminal_is_none_not_empty_string(parsed):
    """航站楼是 MCT 判断的关键输入，缺失必须是 None（未知），
    不能用空串冒充"已知无航站楼"。
    """
    it = next(i for i, _ in parsed if any(s.flight_no == "TK027" for s in i.segments))
    terminals = [s.dep_terminal for s in it.segments] + [
        s.arr_terminal for s in it.segments
    ]
    assert None in terminals
    assert "" not in terminals


def test_baggage_through_checked_unknown(parsed):
    """luggageDirectType 枚举含义未确定 → 标未知，不猜。"""
    for it, _ in parsed:
        assert it.tickets[0].baggage_through_checked is None
        assert it.tickets[0].source_platform == "ctrip"


def test_stop_count_from_transfer_count(parsed):
    """stop_count 用 transferCount（中转次数），不是 stopCount（技术经停）。"""
    for it, _ in parsed:
        assert it.stop_count == 1
        assert len(it.segments) == 2


def test_segment_times_parsed(parsed):
    it = next(i for i, _ in parsed if any(s.flight_no == "LH733" for s in i.segments))
    leg = it.segments[0]
    assert leg.dep_airport == "PVG"
    assert leg.dep_terminal == "T2"
    assert leg.arr_airport == "FRA"
    assert leg.dep_local == datetime(2026, 9, 27, 23, 50)
    assert leg.arr_local == datetime(2026, 9, 28, 7, 30)
    # 跨零点的行程，到达日期必须晚于出发日期
    assert leg.arr_local > leg.dep_local


def test_fare_conditions_kept_raw(parsed):
    """退改政策原文按文档要求原样保留，不解析。"""
    assert all(o.fare_conditions_raw for _, o in parsed)


def test_grouping_collapses_multiple_fares(parsed):
    """同一行程的多个运价产品应归到同一组，最低价由代码算出。"""
    comps = group_and_compare(parsed)
    assert len(comps) == 3  # 3 条行程
    for c in comps:
        assert len(c.offers) == 2
        assert c.cheapest_platform == "ctrip"
        assert c.price_spread == max(o.price for o in c.offers) - min(
            o.price for o in c.offers
        )


def test_rejects_non_success_status():
    with pytest.raises(RuntimeError, match="非成功状态"):
        parse_batch_search({"status": "-1", "msg": "risk control"})
