"""从真实取数结果生成评测集：拿正常行程改造出已知缺陷。

## 为什么要植入缺陷

只有真实行程的话没有 ground truth——我们不知道 agent 报的风险对不对。
把一条正常行程故意改坏（把中转压到 35 分钟、拆成两张票、改成凌晨落地），
就知道正确答案是什么了，于是能算召回率。

## ground truth 写在这里是合理的

这个脚本里有「美国/加拿大转机需入境提取行李」「中国护照过境埃及需签证」
这类知识。**评测答案本来就是人工标注的**，标注者当然可以有知识。

但这些知识绝不能进 src/ 下的产品代码——那是 agent 该判断的东西。
screening.py 里有一条测试专门扫源码防止国家字面量渗进去。

用法：
    uv run python scripts/make_eval_cases.py            # 生成 tests/eval/cases.jsonl
    uv run python scripts/make_eval_cases.py --list     # 只列出会生成什么，不写文件
"""

import argparse
import copy
import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from flight_assistant.matching import build_itinerary_key, group_and_compare  # noqa: E402
from flight_assistant.models import Itinerary, PlatformOffer, TicketGroup  # noqa: E402

ROUTES = Path("data/routes")
OUT = Path("tests/eval/cases.jsonl")

# 标注者的知识，只用于生成 ground truth，不进产品代码。
ENTRY_REQUIRED = {"US", "CA"}  # 转机须入境+提取行李+重新托运的国家
CN_TRANSIT_VISA_NEEDED = {"EG", "GB", "CA"}  # 中国护照过境通常需签证/eTA 的地方


def _load(path: Path):
    rows = json.loads(path.read_text())
    return [
        (
            Itinerary.model_validate(r["itinerary"]),
            PlatformOffer.model_validate(r["offer"]),
        )
        for r in rows
    ]


def _gap(it: Itinerary, i: int) -> int:
    return int(
        (it.segments[i + 1].dep_local - it.segments[i].arr_local).total_seconds() // 60
    )


def _transit_countries(it: Itinerary) -> list[str]:
    """中转点所在国家（不含终点）。"""
    return [s.arr_country for s in it.segments[:-1] if s.arr_country]


def _first_entry_country(it: Itinerary) -> str | None:
    """行程中第一个「转机须入境」国家的中转点国别。"""
    origin = it.segments[0].dep_country
    for s in it.segments[:-1]:
        if s.arr_country and s.arr_country != origin and s.arr_country in ENTRY_REQUIRED:
            return s.arr_country
    return None


# ---------------------------------------------------------------- 缺陷植入


def inject_mct_tight(it: Itinerary, minutes: int = 35) -> tuple[Itinerary, str]:
    """把第一个中转压到极短。不选须入境的中转点——那属于另一类风险。"""
    out = it.model_copy(deep=True)
    shift = _gap(out, 0) - minutes
    for s in out.segments[1:]:
        s.dep_local -= timedelta(minutes=shift)
        s.arr_local -= timedelta(minutes=shift)
    out.total_duration_min -= shift
    return out, f"把 {out.segments[0].arr_airport} 中转从 {_gap(it, 0)} 压到 {minutes} 分钟"


def inject_entry_connection_tight(it: Itinerary, minutes: int = 60) -> tuple[Itinerary, str]:
    """把须入境国家的中转压到 60 分钟——入境+取行李+重挂根本走不完。"""
    out = it.model_copy(deep=True)
    shift = _gap(out, 0) - minutes
    for s in out.segments[1:]:
        s.dep_local -= timedelta(minutes=shift)
        s.arr_local -= timedelta(minutes=shift)
    out.total_duration_min -= shift
    at = out.segments[0].arr_airport
    return out, f"{at}（须入境国）中转从 {_gap(it, 0)} 压到 {minutes} 分钟"


def inject_split_ticket(it: Itinerary) -> tuple[Itinerary, str]:
    """拆成两张票，且行李直挂未知。"""
    out = it.model_copy(deep=True)
    n = len(out.segments)
    out.tickets = [
        TicketGroup(segment_idx=[0], baggage_through_checked=None, source_platform="ctrip"),
        TicketGroup(
            segment_idx=list(range(1, n)),
            baggage_through_checked=None,
            source_platform="feizhu",
        ),
    ]
    return out, "拆成两张票（ctrip + feizhu），行李直挂未知"


def inject_late_arrival(it: Itinerary, hour: int = 1, minute: int = 30) -> tuple[Itinerary, str]:
    """把终点到达改到凌晨。"""
    out = it.model_copy(deep=True)
    last = out.segments[-1]
    old = last.arr_local
    new = old.replace(hour=hour, minute=minute)
    if new <= last.dep_local:
        new += timedelta(days=1)
    last.arr_local = new
    return out, f"终点到达从 {old:%H:%M} 改到 {new:%H:%M}（凌晨）"


def inject_terminal_change(it: Itinerary) -> tuple[Itinerary, str]:
    """制造换航站楼 + 衔接偏紧。"""
    out = it.model_copy(deep=True)
    out.segments[0].arr_terminal = "T2"
    out.segments[1].dep_terminal = "T1"
    shift = _gap(out, 0) - 70
    if shift > 0:
        for s in out.segments[1:]:
            s.dep_local -= timedelta(minutes=shift)
            s.arr_local -= timedelta(minutes=shift)
        out.total_duration_min -= shift
    return out, f"{out.segments[0].arr_airport} 到达 T2 / 出发 T1，衔接压到 70 分钟"


def make_clean(it: Itinerary) -> tuple[Itinerary, str]:
    """把行程改成确认干净：单票、行李直挂已确认。

    真实携程数据里 baggage_through_checked 永远是 null，所以"干净样本"必须
    显式补上——否则每条都会因为"行李直挂未知"而带一条风险，测不出误报率。
    """
    out = it.model_copy(deep=True)
    out.tickets = [
        TicketGroup(
            segment_idx=list(range(len(out.segments))),
            baggage_through_checked=True,
            source_platform="ctrip",
        )
    ]
    return out, "无（对照组：单票 + 行李直挂已确认）"


# ---------------------------------------------------------------- 挑选候选


def pick(fetched, want: str):
    """从真实候选里挑符合条件的一条。

    want:
      entry      经须入境国中转
      plain      中转国不须入境、衔接充裕
      visa       经中国护照需过境签的地方
      direct     直飞
    """
    comps = group_and_compare(fetched)
    for c in sorted(comps, key=lambda x: min(o.price for o in x.offers)):
        it = c.itinerary
        segs = it.segments
        countries = _transit_countries(it)
        if want == "direct" and len(segs) == 1:
            return c
        if len(segs) != 2:
            continue
        if want == "entry" and _first_entry_country(it) and _gap(it, 0) >= 150:
            return c
        if want == "plain" and not _first_entry_country(it) and _gap(it, 0) >= 180:
            return c
        if want == "visa" and any(x in CN_TRANSIT_VISA_NEEDED for x in countries):
            return c
    return None


BASE_CTX = {
    "nationality": "CN",
    "passport_expiry": "2031-05-01",
    "destination_after_arrival": "市区酒店",
    "ground_transport_ok": "public_only",
    "checked_bags": 1,
}


def build_cases(route_files: dict[str, Path], verbose: bool = False) -> list[dict]:
    cases: list[dict] = []

    def add(cid, route, query, comp, defect_kind, note, gt, ctx=None):
        # gt 里每项可带 acceptable_kinds：同一个缺陷有多种合理归类时，任一
        # 命中都算召回。不加这个会把评分方式的问题算成 agent 的失败——
        # 例如 60 分钟的 YVR 中转，报 entry_connection_tight 或 mct_tight
        # 都对，只认一种就会同时记一次漏报和一次误报。
        cases.append(
            {
                "id": cid,
                "route": route,
                "source": "synthetic" if defect_kind != "none" else "real",
                "query": query,
                "trip_context": ctx or BASE_CTX,
                "injected_defect": {"kind": defect_kind, "note": note},
                "ground_truth_risks": gt,
                "candidate": {
                    "itinerary": comp.itinerary.model_dump(mode="json"),
                    "offer": comp.offers[0].model_dump(mode="json"),
                },
            }
        )

    def wrap(it, offer_src):
        return group_and_compare([(it, offer_src)])[0]

    # ---- 1. ORD→PVG：须入境国中转（美国境内首站）压紧
    fetched = _load(route_files["ORD-PVG"])
    c = pick(fetched, "entry")
    if c:
        it2, note = inject_entry_connection_tight(c.itinerary)
        add(
            "case-001",
            "ORD→PVG",
            "芝加哥到上海，12 月 20 日",
            wrap(it2, c.offers[0]),
            "entry_connection_tight",
            note,
            [
                {
                    "kind": "entry_connection_tight",
                    "severity": "blocker",
                    # 加拿大对中国护照有 China Transit Program，可走中转通道
                    # 不入境，所以"必须提取行李重挂"这个前提未必成立；但 60
                    # 分钟在 YVR 无论如何都不够（国际转国际 MCT 约 90 分钟），
                    # 报 mct_tight 同样正确。
                    "acceptable_kinds": ["entry_connection_tight", "mct_tight"],
                }
            ],
        )

    # ---- 2. ORD→PVG：经中国护照需过境签的地方
    c = pick(fetched, "visa")
    if c:
        add(
            "case-002",
            "ORD→PVG",
            "芝加哥到上海，12 月 20 日，圣诞节前后",
            c,
            "none",
            f"未改造，真实行程经 {_transit_countries(c.itinerary)}（中国护照需过境签）",
            [
                {
                    "kind": "transit_visa_required",
                    "severity": "major",
                    # 空侧过境 24 小时内是否免签因航司/航站楼而异，agent 标
                    # needs_user_input 也算合格——这正是它该说"查不到"的场景。
                    "acceptable_kinds": ["transit_visa_required"],
                    "accept_needs_user_input": True,
                }
            ],
        )

    # ---- 3. ORD→PVG：凌晨落地上海
    c = pick(fetched, "plain") or pick(fetched, "entry")
    if c:
        it2, note = inject_late_arrival(c.itinerary)
        add(
            "case-003",
            "ORD→PVG",
            "芝加哥到上海，12 月 20 日，落地后要去市区",
            wrap(it2, c.offers[0]),
            "arrival_no_ground_transit",
            note,
            [{"kind": "arrival_no_ground_transit", "severity": "major"}],
        )

    # ---- 4/5. PVG→ICN：直飞干净样本（测误报）+ 拆票
    fetched = _load(route_files["PVG-ICN"])
    c = pick(fetched, "direct")
    if c:
        it2, note = make_clean(c.itinerary)
        add(
            "case-004",
            "PVG→ICN",
            "上海到首尔，12 月 19 日",
            wrap(it2, c.offers[0]),
            "none",
            note,
            [],  # 干净样本：不该报任何风险
        )
    c2 = pick(fetched, "plain")
    if c2:
        it2, note = inject_split_ticket(c2.itinerary)
        add(
            "case-005",
            "PVG→ICN",
            "上海到首尔，12 月 19 日",
            wrap(it2, c2.offers[0]),
            "self_transfer_no_protection",
            note,
            [
                {"kind": "self_transfer_no_protection", "severity": "major"},
                {"kind": "no_through_baggage", "severity": "major"},
            ],
        )

    # ---- 6/7. PVG→FUK：直飞干净 + 中转压紧
    fetched = _load(route_files["PVG-FUK"])
    c = pick(fetched, "direct")
    if c:
        it2, note = make_clean(c.itinerary)
        add(
            "case-006",
            "PVG→FUK",
            "上海到福冈，12 月 19 日",
            wrap(it2, c.offers[0]),
            "none",
            note,
            [],
        )
    c2 = pick(fetched, "plain")
    if c2:
        it2, note = inject_mct_tight(c2.itinerary)
        add(
            "case-007",
            "PVG→FUK",
            "上海到福冈，12 月 19 日",
            wrap(it2, c2.offers[0]),
            "mct_tight",
            note,
            [
                {
                    "kind": "mct_tight",
                    "severity": "blocker",
                    "acceptable_kinds": ["mct_tight", "entry_connection_tight"],
                }
            ],
        )

    # ---- 8/9. PVG→YYZ：加拿大入境 + 换航站楼
    fetched = _load(route_files["PVG-YYZ"])
    c = pick(fetched, "entry")
    if c:
        it2, note = inject_entry_connection_tight(c.itinerary)
        add(
            "case-008",
            "PVG→YYZ",
            "上海到多伦多，12 月 19 日",
            wrap(it2, c.offers[0]),
            "entry_connection_tight",
            note,
            [
                {
                    "kind": "entry_connection_tight",
                    "severity": "blocker",
                    "acceptable_kinds": ["entry_connection_tight", "mct_tight"],
                }
            ],
        )
    c2 = pick(fetched, "plain") or c
    if c2:
        it2, note = inject_terminal_change(c2.itinerary)
        add(
            "case-009",
            "PVG→YYZ",
            "上海到多伦多，12 月 19 日",
            wrap(it2, c2.offers[0]),
            "terminal_change",
            note,
            [{"kind": "terminal_change", "severity": "minor"}],
        )

    # ---- 10/11/12. PVG→BKK：干净、深夜落地、护照将过期
    fetched = _load(route_files["PVG-BKK"])
    c = pick(fetched, "direct")
    if c:
        it2, note = make_clean(c.itinerary)
        add(
            "case-010",
            "PVG→BKK",
            "上海到泰国，12 月 19 日",
            wrap(it2, c.offers[0]),
            "none",
            note,
            [],
        )
        it3, note3 = inject_late_arrival(c.itinerary, hour=2)
        add(
            "case-011",
            "PVG→BKK",
            "上海到泰国，12 月 19 日，落地后去市区",
            wrap(it3, c.offers[0]),
            "arrival_no_ground_transit",
            note3,
            [{"kind": "arrival_no_ground_transit", "severity": "major"}],
        )
        it4, note4 = make_clean(c.itinerary)
        add(
            "case-012",
            "PVG→BKK",
            "上海到泰国，12 月 19 日",
            wrap(it4, c.offers[0]),
            "passport_validity",
            "护照有效期改为 2027-01-10（落地后不足 6 个月）",
            [{"kind": "passport_validity", "severity": "major"}],
            ctx={**BASE_CTX, "passport_expiry": "2027-01-10"},
        )

    return cases


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="只列出，不写文件")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    files = {
        "ORD-PVG": ROUTES / "ORD-PVG-1220.json",
        "PVG-ICN": ROUTES / "PVG-ICN-20261219.json",
        "PVG-FUK": ROUTES / "PVG-FUK-20261219.json",
        "PVG-YYZ": ROUTES / "PVG-YYZ-20261219.json",
        "PVG-BKK": ROUTES / "PVG-BKK-20261219.json",
    }
    missing = [k for k, v in files.items() if not v.exists()]
    if missing:
        print(f"缺少取数文件: {missing}，先跑 fetch_live.py", file=sys.stderr)
        return 1

    cases = build_cases(files)
    print(f"生成 {len(cases)} 条评测样本：\n")
    for c in cases:
        segs = c["candidate"]["itinerary"]["segments"]
        route = "→".join([segs[0]["dep_airport"]] + [s["arr_airport"] for s in segs])
        gt = ", ".join(f"{r['kind']}/{r['severity']}" for r in c["ground_truth_risks"])
        print(f"  {c['id']}  {route:16s} 缺陷={c['injected_defect']['kind']}")
        print(f"           改造: {c['injected_defect']['note']}")
        print(f"           期望: {gt or '（不该报任何风险）'}")

    n_clean = sum(1 for c in cases if not c["ground_truth_risks"])
    n_blocker = sum(
        1
        for c in cases
        for r in c["ground_truth_risks"]
        if r["severity"] == "blocker"
    )
    print(f"\n干净样本 {n_clean} 条（测误报率），期望 blocker {n_blocker} 个（测召回率）")

    if args.list:
        return 0

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in cases) + "\n"
    )
    print(f"\n已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
