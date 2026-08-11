"""飞猪 flight_search_result_poller.do 解析测试。

fixture 是从 2026-08-05 真实响应里裁出来的 2 条行程（SHA→CHI，均经停中转，
去掉了 filter 列表和营销字段，保留解析用到的全部字段）：
  - AA182/AA1476：西雅图中转
  - BR711/BR056：台北中转，带 visaRemark（不参与解析，agent 层的事）
"""

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from flight_assistant.fetchers.feizhu_parse import parse_poller_response
from flight_assistant.matching import group_and_compare

FIXTURE = Path(__file__).parent / "fixtures" / "feizhu_poller_sample.json"


@pytest.fixture
def payload() -> dict:
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def parsed(payload):
    items, _delay = parse_poller_response(payload, fetched_at=datetime(2026, 8, 5, 17, 22))
    return items


def test_parses_all_items(parsed):
    assert len(parsed) == 2
    assert {o.platform for _, o in parsed} == {"feizhu"}


def test_delay_for_next_poll_surfaced(payload):
    _items, delay = parse_poller_response(payload)
    assert delay == 0


def test_price_is_tax_inclusive_yuan(payload, parsed):
    """price = (adultPrice + adultTax) / 100，和 totalAdultPrice 对得上，
    单位从分转元。核对依据：358800 + 205900 = 564700 = totalAdultPrice。
    """
    first = payload["data"]["flightItems"][0]
    assert first["adultPrice"] + first["adultTax"] == first["totalAdultPrice"]
    assert parsed[0][1].price == Decimal(str(first["totalAdultPrice"])) / Decimal(100)


def test_blank_terminal_is_none_not_empty_string(parsed):
    """西雅图段 arrTerm/depTerm 是空串，必须转成 None，不能冒充"已知无航站楼"。"""
    it = next(i for i, _ in parsed if any(s.flight_no == "AA182" for s in i.segments))
    terminals = [s.dep_terminal for s in it.segments] + [
        s.arr_terminal for s in it.segments
    ]
    assert None in terminals
    assert "" not in terminals


def test_carrier_and_flight_no(parsed):
    it = next(i for i, _ in parsed if any(s.flight_no == "BR711" for s in i.segments))
    leg = it.segments[0]
    assert leg.carrier == "BR"
    assert leg.flight_no == "BR711"
    assert leg.dep_airport == "PVG"
    assert leg.arr_airport == "TPE"


def test_stop_count_from_transfer_count(parsed):
    for it, _ in parsed:
        assert it.stop_count == 1
        assert len(it.segments) == 2


def test_segment_times_parsed(parsed):
    it = next(i for i, _ in parsed if any(s.flight_no == "AA182" for s in i.segments))
    leg = it.segments[0]
    assert leg.dep_local == datetime(2026, 9, 26, 18, 20)
    assert leg.arr_local == datetime(2026, 9, 26, 13, 45)


def test_baggage_through_checked_unknown(parsed):
    """响应里没有联程行李直挂字段 → 标未知，不猜。"""
    for it, _ in parsed:
        assert it.tickets[0].baggage_through_checked is None
        assert it.tickets[0].source_platform == "feizhu"


def test_country_codes_not_guessed(parsed):
    """响应里只有城市码/城市名，没有国家码字段，不强行推断。"""
    for it, _ in parsed:
        for s in it.segments:
            assert s.dep_country is None
            assert s.arr_country is None


def test_grouping_collapses_correctly(parsed):
    comps = group_and_compare(parsed)
    assert len(comps) == 2
    for c in comps:
        assert len(c.offers) == 1
        assert c.cheapest_platform == "feizhu"


def test_empty_flight_items():
    items, delay = parse_poller_response({"data": {"delayForNextPoll": 0, "flightItems": []}})
    assert items == []
    assert delay == 0
