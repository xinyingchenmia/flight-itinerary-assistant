"""事实缓存测试。

来由：加了中转体验查证后单价从 $0.0805 涨到 $0.1562/候选，工具轮次 2.0 →
8.0，全是搜索开销。而查的这些事实（DFW Terminal D 有哪些餐厅、CTA 蓝线
几点收班、SFO 官方 MCT）跨查询、跨用户、跨月份都不变，重复查是纯浪费。
"""

import json
from datetime import datetime, timedelta

from conftest import itin, offer, seg
from flight_assistant.factcache import TTL_DAYS, Fact, FactCache
from flight_assistant.matching import group_and_compare
from flight_assistant.risk_review.agent import build_context


def test_put_and_get():
    c = FactCache()
    c.put("mct", "SFO", "国际转国内建议 120-180 分钟", "SFO 官网")
    f = c.get("mct", "SFO")
    assert f is not None
    assert "120-180" in f.value
    assert f.source == "SFO 官网"


def test_airport_code_normalized_to_upper():
    """机场码大小写不该产生两条记录。"""
    c = FactCache()
    c.put("mct", "sfo", "x", "src")
    assert c.get("mct", "SFO") is not None
    assert len(c) == 1


def test_composite_subject_preserved():
    """复合键（SFO:intl_to_domestic）不做大写化——它不是机场码。"""
    c = FactCache()
    c.put("mct", "SFO:intl_to_domestic", "90 分钟", "AA 官网")
    assert c.get("mct", "SFO:intl_to_domestic") is not None


def test_ttl_differs_by_topic():
    """签证政策变化最快，机场设施最稳定——TTL 按真实变化速度定。"""
    assert TTL_DAYS["transit_visa"] < TTL_DAYS["ground_transit"]
    assert TTL_DAYS["ground_transit"] <= TTL_DAYS["airport_facility"]


def test_stale_by_topic_ttl():
    old = (datetime.now() - timedelta(days=45)).isoformat(timespec="seconds")
    visa = Fact("transit_visa", "HK", "中国护照免签", "入境处", old)
    facility = Fact("airport_facility", "DFW", "T-D 有 30 家餐饮", "机场官网", old)

    assert visa.is_stale()  # 45 天 > 30 天
    assert not facility.is_stale()  # 45 天 < 180 天


def test_stale_fact_kept_not_deleted():
    """过期不删只标记——旧值仍比"完全不知道"有用，agent 可以选择重查。"""
    c = FactCache()
    f = c.put("transit_visa", "HK", "免签", "入境处")
    f.fetched_at = (datetime.now() - timedelta(days=90)).isoformat(timespec="seconds")
    assert c.get("transit_visa", "HK") is not None
    assert "需重新核实" in f.render()


def test_render_marks_source():
    c = FactCache()
    f = c.put("ground_transit", "ORD", "蓝线 24 小时运营", "CTA 官网")
    assert "CTA 官网" in f.render()
    assert "需重新核实" not in f.render()


# ---------------------------------------------------------------- 持久化


def test_roundtrip_through_file(tmp_path):
    path = tmp_path / "facts.json"
    c = FactCache(path)
    c.put("mct", "SFO", "120-180 分钟", "SFO 官网")
    c.put("airport_facility", "DFW", "T-D 30 家餐饮", "DFW 官网")
    c.save()

    reloaded = FactCache(path)
    assert len(reloaded) == 2
    assert reloaded.get("mct", "SFO").value == "120-180 分钟"


def test_corrupt_row_skipped_not_crash(tmp_path):
    """旧格式的缓存不该把程序搞崩。"""
    path = tmp_path / "facts.json"
    path.write_text(json.dumps([{"topic": "mct", "unexpected": 1}]))
    c = FactCache(path)
    assert len(c) == 0


def test_save_noop_without_path():
    FactCache().save()  # 不该抛


# ---------------------------------------------------------------- 预取


def _pvg_sfo_ord():
    it = itin(
        [
            seg("UA", "UA858", "PVG", "SFO", "2026-09-27T12:10", "2026-09-27T08:35"),
            seg("UA", "UA2827", "SFO", "ORD", "2026-09-27T10:10", "2026-09-27T16:05"),
        ]
    )
    for s in it.segments:
        s.dep_country = "CN" if s.dep_airport == "PVG" else "US"
        s.arr_country = "US"
    return it


def test_subjects_of_itinerary_covers_airports_and_countries():
    c = FactCache()
    subjects = c.subjects_of_itinerary(_pvg_sfo_ord())
    assert {"PVG", "SFO", "ORD", "CN", "US"} <= set(subjects)


def test_relevant_prefetches_by_airport():
    c = FactCache()
    c.put("mct", "SFO:intl_to_domestic", "120-180 分钟", "SFO 官网")
    c.put("ground_transit", "ORD", "蓝线 24 小时", "CTA")
    c.put("airport_facility", "HND", "T3 有观景台", "羽田官网")  # 不相关

    facts = c.relevant(c.subjects_of_itinerary(_pvg_sfo_ord()))
    subjects = {f.subject for f in facts}
    assert "SFO:intl_to_domestic" in subjects  # 前缀匹配命中
    assert "ORD" in subjects
    assert "HND" not in subjects  # 本行程不经过羽田


def test_known_facts_injected_into_context():
    """命中的事实直接注入 prompt，不让 agent 调工具查——省一次往返。"""
    c = FactCache()
    c.put("ground_transit", "ORD", "蓝线 24 小时运营", "CTA 官网")
    comp = group_and_compare([(_pvg_sfo_ord(), offer("ctrip", "7102"))])[0]

    ctx = build_context(comp, cache=c)
    assert len(ctx["known_facts"]) == 1
    assert "蓝线 24 小时运营" in ctx["known_facts"][0]
    assert "CTA 官网" in ctx["known_facts"][0]


def test_no_cache_means_empty_known_facts():
    comp = group_and_compare([(_pvg_sfo_ord(), offer("ctrip", "7102"))])[0]
    assert build_context(comp)["known_facts"] == []


def test_remember_fact_tool_only_registered_with_cache():
    """没有缓存时不注册写回工具，避免空转的工具占轮次。"""
    from flight_assistant.risk_review import tools

    assert tools.allowed_tools() == []
    assert tools.allowed_tools(FactCache()) == ["mcp__risk_tools__remember_fact"]


def test_stats_reports_counts():
    c = FactCache()
    c.put("mct", "SFO", "x", "s")
    c.put("mct", "DFW", "y", "s")
    c.put("ground_transit", "ORD", "z", "s")
    out = c.stats()
    assert "3 条事实" in out
    assert "mct 2" in out


# ---------------------------------------------------------- subject 格式约束


def test_subject_must_be_structured():
    """实测 bug：subject 让 agent 自由命名时，存出了 "SFO国际转国内" 和
    "SFO Terminal 3(国内)"，按机场码预取全都匹配不上，缓存完全失效——
    第二次跑成本只降 5%，事实条数反而翻倍。
    """
    from flight_assistant.factcache import normalize_subject

    assert normalize_subject("SFO") == "SFO"
    assert normalize_subject("sfo") == "SFO"
    assert normalize_subject("SFO:mct_intl_to_domestic") == "SFO:mct_intl_to_domestic"
    assert normalize_subject("HK:transit_visa_cn") == "HK:transit_visa_cn"

    # 自然语言键名一律拒绝
    assert normalize_subject("SFO国际转国内") is None
    assert normalize_subject("SFO Terminal 3(国内)") is None
    assert normalize_subject("") is None


def test_put_rejects_bad_subject():
    import pytest

    c = FactCache()
    with pytest.raises(ValueError, match="格式不合规"):
        c.put("mct", "SFO国际转国内", "x", "src")


def test_structured_subject_is_prefetchable():
    """结构化键名才能被机场码预取命中——这是缓存有效的前提。"""
    c = FactCache()
    c.put("mct", "SFO:mct_intl_to_domestic", "120-180 分钟", "SFO 官网")
    facts = c.relevant(["SFO", "ORD"])
    assert len(facts) == 1


def test_same_fact_overwrites_not_duplicates():
    """同一 (topic, subject) 重复写入应覆盖，不该累积成两条。"""
    c = FactCache()
    c.put("mct", "SFO:mct_intl_to_domestic", "旧值", "src1")
    c.put("mct", "sfo:mct_intl_to_domestic", "新值", "src2")
    assert len(c) == 1
    assert c.get("mct", "SFO:mct_intl_to_domestic").value == "新值"
