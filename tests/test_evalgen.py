"""评测样本生成的测试。

重点在**代码这一侧的约束**：agent 出的题必须落在可改字段白名单里、改完
仍是物理成立的行程、派生字段由代码重算。这些约束保证"agent 自由出题"
不等于"agent 可以出一道无法核对的假题"。

不测 agent 出题的内容质量——那要靠人过一遍生成结果。
"""

from datetime import datetime

import pytest

from conftest import itin, seg
from flight_assistant.evalgen import (
    DefectSpec,
    MutationRejected,
    apply_spec,
)
from flight_assistant.models import TicketGroup


def _base():
    it = itin(
        [
            seg("UA", "UA858", "PVG", "SFO", "2026-12-19T12:10", "2026-12-19T08:35",
                dep_terminal="T2", arr_terminal="I"),
            seg("UA", "UA2827", "SFO", "ORD", "2026-12-19T13:10", "2026-12-19T19:05",
                dep_terminal="T3", arr_terminal="T1"),
        ],
        duration=1015,
    )
    for s in it.segments:
        s.dep_country = "CN" if s.dep_airport == "PVG" else "US"
        s.arr_country = "US"
    return it


def _spec(mutations, risks=None):
    return DefectSpec(
        defect_name="test",
        rationale="test",
        mutations=mutations,
        expected_risks=risks or [{"kind": "mct_tight", "severity": "major", "why": "x"}],
    )


def test_apply_time_mutation():
    it = _base()
    out = apply_spec(
        it,
        _spec([{"path": "segments.1.dep_local", "value": "2026-12-19 09:30:00", "note": "压紧"}]),
    )
    assert out.segments[1].dep_local == datetime(2026, 12, 19, 9, 30)
    # 原对象不该被改动
    assert it.segments[1].dep_local == datetime(2026, 12, 19, 13, 10)


def test_duration_adjusted_by_delta_not_recomputed():
    """总时长按变化量调整，不能重算绝对值。

    local 时刻跨时区不可比：PVG 12:10 起飞 / SFO 08:35 落地是真实的跨日界线
    航班，(末段到达 - 首段出发) 会算出负数。所以用增量调整。
    """
    it = _base()
    out = apply_spec(
        it,
        _spec([{"path": "segments.1.arr_local", "value": "2026-12-19 23:05:00", "note": "延后 4 小时"}]),
    )
    assert out.total_duration_min == 1015 + 240  # 到达推迟 4 小时
    assert out.stop_count == 1


def test_dateline_crossing_itinerary_accepted():
    """跨太平洋航线当地到达时间早于出发时间，这是正常的，不该被判非法。"""
    it = _base()
    assert it.segments[0].arr_local < it.segments[0].dep_local  # 12:10 → 08:35
    out = apply_spec(
        it,
        _spec([{"path": "segments.1.dep_local", "value": "2026-12-19 09:35:00", "note": "衔接压到 60 分钟"}]),
    )
    assert out.segments[1].dep_local == datetime(2026, 12, 19, 9, 35)


def test_ticket_split_mutation():
    it = _base()
    out = apply_spec(
        it,
        _spec(
            [
                {
                    "path": "tickets",
                    "value": [
                        {"segment_idx": [0], "baggage_through_checked": False,
                         "source_platform": "ctrip"},
                        {"segment_idx": [1], "baggage_through_checked": False,
                         "source_platform": "feizhu"},
                    ],
                    "note": "拆票",
                }
            ]
        ),
    )
    assert len(out.tickets) == 2
    assert out.tickets[0].baggage_through_checked is False


def test_rejects_field_outside_whitelist():
    """agent 不能改任意字段——白名单保证改造可审计。"""
    it = _base()
    with pytest.raises(MutationRejected, match="不在可改白名单"):
        apply_spec(it, _spec([{"path": "offers.0.price", "value": "1", "note": "改价"}]))


def test_rejects_nonexistent_field():
    it = _base()
    with pytest.raises(MutationRejected):
        apply_spec(it, _spec([{"path": "total_duration_minutes", "value": 1, "note": "拼错"}]))


def test_rejects_empty_mutations():
    it = _base()
    with pytest.raises(MutationRejected, match="没有任何 mutation"):
        apply_spec(it, _spec([]))


def test_rejects_negative_connection():
    it = _base()
    with pytest.raises(MutationRejected, match="早于"):
        apply_spec(
            it,
            _spec([{"path": "segments.1.dep_local", "value": "2026-12-19 07:00:00", "note": "早于前段到达"}]),
        )


def test_rejects_tickets_not_covering_all_segments():
    """拆票时漏掉某个航段是无效的题。"""
    it = _base()
    with pytest.raises(MutationRejected, match="没有覆盖全部航段"):
        apply_spec(
            it,
            _spec(
                [
                    {
                        "path": "tickets",
                        "value": [
                            {"segment_idx": [0], "baggage_through_checked": None,
                             "source_platform": "ctrip"}
                        ],
                        "note": "只覆盖第一段",
                    }
                ]
            ),
        )


def test_rejects_invalid_enum_value():
    it = _base()
    with pytest.raises(MutationRejected, match="不是合法行程"):
        apply_spec(
            it,
            _spec(
                [
                    {
                        "path": "tickets",
                        "value": [
                            {"segment_idx": [0, 1], "baggage_through_checked": "大概吧",
                             "source_platform": "ctrip"}
                        ],
                        "note": "非法值",
                    }
                ]
            ),
        )


def test_terminal_and_carrier_mutable():
    it = _base()
    out = apply_spec(
        it,
        _spec(
            [
                {"path": "segments.1.dep_terminal", "value": "T1", "note": "换航站楼"},
                {"path": "segments.1.carrier", "value": "KE", "note": "跨联盟"},
            ]
        ),
    )
    assert out.segments[1].dep_terminal == "T1"
    assert out.segments[1].carrier == "KE"


def test_no_hardcoded_defect_types_in_module():
    """缺陷类型不该由代码枚举——那样评测集只能测出我想得到的类型，
    agent 在我没想到的地方漏报就永远发现不了。
    """
    from flight_assistant import evalgen

    src = open(evalgen.__file__).read()
    # 不该有 inject_xxx 这种按缺陷类型分的函数
    assert "def inject_" not in src
    # 不该有硬编码的国家政策表
    assert "ENTRY_REQUIRED" not in src
    assert "CN_TRANSIT_VISA" not in src
