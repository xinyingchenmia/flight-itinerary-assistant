"""生成评测集：真实行程交给对抗 agent 出题，代码确定性地施加改造。

## 和第一版的区别

第一版我在这里枚举了 inject_mct_tight / inject_entry_connection_tight /
inject_split_ticket 这些函数，还硬编码了「美国和加拿大转机须入境」。后果是
**评测集只能测出我想得到的缺陷类型**——agent 在我没想到的地方漏报，评测集
永远发现不了。而"想不到的失败模式"恰恰是评测最该覆盖的。

现在：对抗 agent 自己设计缺陷（prompt 里只给 2 个格式示例，不列举类型），
代码只负责按它给的字段路径施加改造并校验。见 src/flight_assistant/evalgen.py。

## 干净样本仍然由代码生成

对照组不需要创造性——把行程改成"单票 + 行李直挂已确认"就是干净样本，
用来测误报率。真实携程数据里 baggage_through_checked 永远是 null，不显式
补上的话每条都会带一条"行李直挂未知"的风险，测不出误报。

用法：
    uv run python scripts/make_eval_cases.py                  # 生成
    uv run python scripts/make_eval_cases.py --per-route 3    # 每条航线出几道题
    uv run python scripts/make_eval_cases.py --dry-run        # 不调 agent，只列出选中的行程
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from flight_assistant.budget import BudgetLedger  # noqa: E402
from flight_assistant.evalgen import (  # noqa: E402
    MutationRejected,
    apply_spec,
    design_defect,
)
from flight_assistant.matching import group_and_compare  # noqa: E402
from flight_assistant.models import Itinerary, PlatformOffer, TicketGroup  # noqa: E402

ROUTES = Path("data/routes")
OUT = Path("tests/eval/cases.jsonl")

ROUTE_FILES = {
    "ORD→PVG": (ROUTES / "ORD-PVG-1220.json", "芝加哥到上海，12 月 20 日，圣诞节前后"),
    "PVG→ICN": (ROUTES / "PVG-ICN-20261219.json", "上海到首尔，12 月 19 日"),
    "PVG→FUK": (ROUTES / "PVG-FUK-20261219.json", "上海到福冈，12 月 19 日"),
    "PVG→YYZ": (ROUTES / "PVG-YYZ-20261219.json", "上海到多伦多，12 月 19 日"),
    "PVG→BKK": (ROUTES / "PVG-BKK-20261219.json", "上海到泰国，12 月 19 日"),
}

BASE_CTX = {
    "nationality": "CN",
    "passport_expiry": "2031-05-01",
    "destination_after_arrival": "市区酒店",
    "ground_transport_ok": "public_only",
    "checked_bags": 1,
}


def _load(path: Path):
    rows = json.loads(path.read_text())
    return [
        (
            Itinerary.model_validate(r["itinerary"]),
            PlatformOffer.model_validate(r["offer"]),
        )
        for r in rows
    ]


def _route_str(it: Itinerary) -> str:
    return "→".join([it.segments[0].dep_airport] + [s.arr_airport for s in it.segments])


def pick_candidates(fetched, n_connecting: int, n_direct: int):
    """挑出用于出题的中转行程和用于对照的直飞行程。

    刻意不按"缺陷类型"挑——挑选条件只有航段数和价格，出什么题由 agent 定。
    """
    comps = group_and_compare(fetched)
    comps.sort(key=lambda c: min(o.price for o in c.offers))
    connecting = [c for c in comps if len(c.itinerary.segments) >= 2][:n_connecting]
    direct = [c for c in comps if len(c.itinerary.segments) == 1][:n_direct]
    return connecting, direct


def make_clean(it: Itinerary) -> Itinerary:
    out = it.model_copy(deep=True)
    out.tickets = [
        TicketGroup(
            segment_idx=list(range(len(out.segments))),
            baggage_through_checked=True,
            source_platform="ctrip",
        )
    ]
    return out


def _case(cid, route, query, itinerary, offer, defect, note, gt, ctx=None):
    return {
        "id": cid,
        "route": route,
        "source": "synthetic" if defect != "none" else "real",
        "query": query,
        "trip_context": ctx or BASE_CTX,
        "injected_defect": {"kind": defect, "note": note},
        "ground_truth_risks": gt,
        "candidate": {
            "itinerary": itinerary.model_dump(mode="json"),
            "offer": offer.model_dump(mode="json"),
        },
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-route", type=int, default=2, help="每条航线出几道题")
    ap.add_argument("--clean-per-route", type=int, default=1, help="每条航线几个干净对照")
    ap.add_argument("--adversary-model", default=None, help="出题用的模型，默认 CLI 默认")
    ap.add_argument("--budget", type=float, default=2.0)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true", help="不调 agent，只列出选中的行程")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    missing = [k for k, (p, _) in ROUTE_FILES.items() if not p.exists()]
    if missing:
        print(f"缺少取数文件: {missing}，先跑 fetch_live.py", file=sys.stderr)
        return 1

    # ---- 选行程
    jobs: list[tuple[str, str, object]] = []  # (route, query, comparison)
    cases: list[dict] = []
    cid = 0

    for route, (path, query) in ROUTE_FILES.items():
        fetched = _load(path)
        connecting, direct = pick_candidates(
            fetched, args.per_route, args.clean_per_route
        )
        for c in connecting:
            jobs.append((route, query, c))
        for c in direct:
            cid += 1
            cases.append(
                _case(
                    f"case-{cid:03d}",
                    route,
                    query,
                    make_clean(c.itinerary),
                    c.offers[0],
                    "none",
                    "对照组：单票 + 行李直挂已确认",
                    [],
                )
            )
        print(f"{route}: 出题 {len(connecting)} 条，干净对照 {len(direct)} 条")

    if args.dry_run:
        print(f"\n将向 agent 请求 {len(jobs)} 道题：")
        for route, _q, c in jobs:
            gaps = [
                int(
                    (
                        c.itinerary.segments[i + 1].dep_local
                        - c.itinerary.segments[i].arr_local
                    ).total_seconds()
                    // 60
                )
                for i in range(len(c.itinerary.segments) - 1)
            ]
            print(f"  {route:9s} {_route_str(c.itinerary):18s} 衔接 {gaps} 分钟")
        return 0

    # ---- 让 agent 出题
    ledger = BudgetLedger(cap=args.budget)
    sem = asyncio.Semaphore(args.concurrency)
    used: list[str] = []  # 已出过的 defect_name，促使 agent 换角度

    async def one(job):
        route, query, comp = job
        async with sem:
            try:
                spec = await design_defect(
                    comp.itinerary, args.adversary_model, avoid=list(used)
                )
            except Exception as e:
                print(f"  {route} 出题失败: {type(e).__name__}: {e}", file=sys.stderr)
                return None
            try:
                mutated = apply_spec(comp.itinerary, spec)
            except MutationRejected as e:
                print(f"  {route} 改造被拒（{spec.defect_name}）: {e}", file=sys.stderr)
                return None
            used.append(spec.defect_name)
            return route, query, comp, spec, mutated

    results = [r for r in await asyncio.gather(*(one(j) for j in jobs)) if r]

    for route, query, comp, spec, mutated in results:
        cid += 1
        cases.append(
            _case(
                f"case-{cid:03d}",
                route,
                query,
                mutated,
                comp.offers[0],
                spec.defect_name,
                spec.rationale,
                spec.expected_risks,
            )
        )

    # ---- 输出，给人过一遍
    print(f"\n{'=' * 74}\n生成 {len(cases)} 条样本（请人工过一遍 ground truth）\n{'=' * 74}")
    for c in cases:
        it = Itinerary.model_validate(c["candidate"]["itinerary"])
        gt = ", ".join(f"{r['kind']}/{r['severity']}" for r in c["ground_truth_risks"])
        print(f"\n{c['id']}  {c['route']}  {_route_str(it)}")
        print(f"  缺陷: {c['injected_defect']['kind']}")
        print(f"  理由: {c['injected_defect']['note']}")
        print(f"  期望: {gt or '（不该报任何风险）'}")

    n_clean = sum(1 for c in cases if not c["ground_truth_risks"])
    n_blocker = sum(
        1 for c in cases for r in c["ground_truth_risks"] if r["severity"] == "blocker"
    )
    print(
        f"\n干净样本 {n_clean} 条（测误报率）| 期望 blocker {n_blocker} 个（测召回率）"
    )
    print(f"出题成本 ${ledger.spent:.4f}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(json.dumps(c, ensure_ascii=False) for c in cases) + "\n")
    print(f"已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
