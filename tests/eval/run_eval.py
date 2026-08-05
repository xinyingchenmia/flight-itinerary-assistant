"""跑评测集，算出真实指标。

## 为什么需要它

在这之前所有质量判断都是肉眼看几条输出。具体答不上来的问题：
  - 热缓存那次风险从 6 条降到 2 条，是解决了还是漏报了？
  - 改了七八次 prompt，每次只用一次运行验证，改坏了根本不知道
  - 误报率完全没测过——对干净行程硬凑风险的话产品就没法用

## 评分口径

- **kind 匹配 + 可接受的替代类型**。同一个缺陷常有多种合理归类（60 分钟的
  YVR 中转，报 entry_connection_tight 或 mct_tight 都对），只认一种会把
  评分方式的问题算成 agent 的失败。
- **严重程度容忍一级**。blocker vs major 的边界本身是主观的；差两级
  （blocker vs minor）才算判断失误。
- **误报只统计干净样本上的**。有缺陷的样本上多报几条相关风险不算误报，
  那可能是真实存在的其他问题。

用法：
    uv run python tests/eval/run_eval.py                      # 全量
    uv run python tests/eval/run_eval.py --limit 4            # 只跑前 4 条
    uv run python tests/eval/run_eval.py --no-cache           # 冷缓存（对比用）
"""

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from flight_assistant.budget import BudgetLedger, cost_of  # noqa: E402
from flight_assistant.factcache import FactCache  # noqa: E402
from flight_assistant.matching import group_and_compare  # noqa: E402
from flight_assistant.models import (  # noqa: E402
    Itinerary,
    PlatformOffer,
    TripContext,
)
from flight_assistant.risk_review.agent import review_batch  # noqa: E402

SEVERITY_ORDER = {"blocker": 0, "major": 1, "minor": 2}


@dataclass
class CaseResult:
    case_id: str
    route: str
    defect: str
    is_clean: bool
    expected: list[dict]
    reported: list[dict]
    assurances: list[str] = field(default_factory=list)

    # ---- 判定

    def _match(self, exp: dict) -> dict | None:
        """在实际报告里找匹配这条期望的风险。"""
        ok_kinds = set(exp.get("acceptable_kinds") or [exp["kind"]])
        for r in self.reported:
            if r["kind"] not in ok_kinds:
                continue
            gap = abs(SEVERITY_ORDER[r["severity"]] - SEVERITY_ORDER[exp["severity"]])
            if gap <= 1:  # 严重程度容忍一级
                return r
            if exp.get("accept_needs_user_input") and r.get("needs_user_input"):
                return r
        # 允许"标为需追问"算合格的情况（该说查不到的场景）
        if exp.get("accept_needs_user_input"):
            for r in self.reported:
                if r["kind"] in ok_kinds and r.get("needs_user_input"):
                    return r
        return None

    @property
    def hits(self) -> list[tuple[dict, dict | None]]:
        return [(exp, self._match(exp)) for exp in self.expected]

    @property
    def missed(self) -> list[dict]:
        return [exp for exp, got in self.hits if got is None]

    @property
    def missed_blockers(self) -> list[dict]:
        return [e for e in self.missed if e["severity"] == "blocker"]

    @property
    def unknown_count(self) -> int:
        return sum(1 for r in self.reported if r.get("needs_user_input"))


def load_cases(path: Path, limit: int | None = None) -> list[dict]:
    rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    return rows[:limit] if limit else rows


def _comparison(case: dict):
    it = Itinerary.model_validate(case["candidate"]["itinerary"])
    offer = PlatformOffer.model_validate(case["candidate"]["offer"])
    return group_and_compare([(it, offer)])[0]


def _result_of(case, comp, risks, assurances) -> CaseResult:
    segs = comp.itinerary.segments
    route = "→".join([segs[0].dep_airport] + [s.arr_airport for s in segs])
    return CaseResult(
        case_id=case["id"],
        route=route,
        defect=case["injected_defect"]["kind"],
        is_clean=not case["ground_truth_risks"],
        expected=case["ground_truth_risks"],
        reported=[r.model_dump(mode="json") for r in risks],
        assurances=[a.statement for a in assurances],
    )


async def run_group(cases: list[dict], args, cache, ledger, stats) -> list[CaseResult]:
    """一组共享同一 trip_context 的样本，合并成一次调用。

    第一版给每条样本单独调用一次，把批量这个主要降本手段整个绕过了——
    单价回到 $0.2993/条，12 条跑了 563 秒。trip_context 不同的样本不能
    混批（同一次调用只能带一份用户信息），所以按 context 分组。
    """
    comps = [_comparison(c) for c in cases]
    ctx = TripContext.model_validate(cases[0]["trip_context"])
    findings = await review_batch(
        comps,
        stats,
        trip_context=ctx,
        model=args.review_model,
        ledger=ledger,
        web_search=not args.no_search,
        max_searches=args.max_searches,
        cache=cache,
    )
    out = []
    for case, comp in zip(cases, comps):
        risks, assurances, _notes = findings.get(comp.itinerary_key, ([], [], []))
        out.append(_result_of(case, comp, risks, assurances))
    return out


def report(results: list[CaseResult], spent: float, elapsed: float) -> None:
    print(f"\n{'=' * 74}\n逐条结果\n{'=' * 74}")
    for r in results:
        tag = "干净样本" if r.is_clean else f"缺陷={r.defect}"
        print(f"\n{r.case_id} {r.route:16s} {tag}")
        for exp, got in r.hits:
            if got:
                mark = "✓ 命中"
                detail = f"{got['kind']}/{got['severity']}"
                if got["kind"] != exp["kind"]:
                    detail += "（替代归类，可接受）"
            else:
                mark = "✗ 漏报"
                detail = "未报告"
            print(f"  {mark} 期望 {exp['kind']}/{exp['severity']} → {detail}")
        extra = [
            x
            for x in r.reported
            if all(x is not got for _e, got in r.hits)
        ]
        for x in extra:
            flag = " [需追问]" if x.get("needs_user_input") else ""
            note = "  ← 干净样本上的误报" if r.is_clean else ""
            print(f"  + 额外 {x['kind']}/{x['severity']}{flag}{note}")
        if r.is_clean and not r.reported:
            print("  ✓ 未报告任何风险（正确）")

    # ---------------------------------------------------------------- 指标
    exp_blockers = sum(
        1 for r in results for e in r.expected if e["severity"] == "blocker"
    )
    missed_blockers = sum(len(r.missed_blockers) for r in results)
    exp_all = sum(len(r.expected) for r in results)
    missed_all = sum(len(r.missed) for r in results)

    clean = [r for r in results if r.is_clean]
    clean_fp = sum(len(r.reported) for r in clean)
    reported_total = sum(len(r.reported) for r in results)
    unknown_total = sum(r.unknown_count for r in results)

    print(f"\n{'=' * 74}\n指标\n{'=' * 74}")

    def pct(a, b):
        return f"{a / b * 100:.0f}%" if b else "n/a"

    print(
        f"blocker 召回率     {pct(exp_blockers - missed_blockers, exp_blockers)}"
        f"  ({exp_blockers - missed_blockers}/{exp_blockers})   ← 漏报权重最高"
    )
    print(
        f"全部风险召回率     {pct(exp_all - missed_all, exp_all)}"
        f"  ({exp_all - missed_all}/{exp_all})"
    )
    print(
        f"误报率(干净样本)   {clean_fp} 条 / {len(clean)} 个干净样本"
        f"   ← 大于 0 就该查"
    )
    print(
        f"unknown 项占比     {pct(unknown_total, reported_total)}"
        f"  ({unknown_total}/{reported_total})   ← 诚实度"
    )
    print(f"已确认没问题       {sum(len(r.assurances) for r in results)} 条")
    print(
        f"成本               ${spent:.4f} / {len(results)} 条 = "
        f"${spent / max(len(results), 1):.4f} 每条，耗时 {elapsed:.0f}s"
    )

    if missed_blockers:
        print(f"\n⚠ 漏了 {missed_blockers} 个 blocker —— 等价于让用户误机，优先修这个")
    if clean_fp:
        print(f"⚠ 干净样本上报了 {clean_fp} 条风险 —— 误报会让用户学会忽略所有警告")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=str(ROOT / "tests/eval/cases.jsonl"))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--review-model", default="claude-sonnet-5")
    ap.add_argument("--max-searches", type=int, default=4)
    ap.add_argument("--budget", type=float, default=3.0)
    ap.add_argument("--no-search", action="store_true", help="关掉联网查证")
    ap.add_argument(
        "--fact-cache",
        default=str(Path.home() / ".flight-assistant-facts.json"),
    )
    ap.add_argument("--no-cache", action="store_true", help="不用事实缓存（冷缓存对比）")
    ap.add_argument("--batch-size", type=int, default=6, help="一次调用审几条样本")
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args()

    path = Path(args.cases)
    if not path.exists():
        print(
            f"找不到 {path}。先跑 scripts/make_eval_cases.py 生成评测集。",
            file=sys.stderr,
        )
        return 1

    cases = load_cases(path, args.limit)
    cache = None if args.no_cache else FactCache(Path(args.fact_cache))
    ledger = BudgetLedger(cap=args.budget)
    stats: list[dict] = []

    print(f"评测集 {len(cases)} 条 | 模型 {args.review_model} | 预算 ${args.budget}")
    if cache is not None:
        print(f"事实缓存: {cache.stats()}")
    else:
        print("事实缓存: 禁用（冷缓存模式）")

    # 按 trip_context 分组，组内再按 batch_size 切批
    groups: dict[str, list[dict]] = {}
    for c in cases:
        groups.setdefault(json.dumps(c["trip_context"], sort_keys=True), []).append(c)
    batches: list[list[dict]] = []
    for grouped in groups.values():
        for i in range(0, len(grouped), args.batch_size):
            batches.append(grouped[i : i + args.batch_size])
    print(
        f"分成 {len(batches)} 批（{len(groups)} 种 trip_context，"
        f"每批最多 {args.batch_size} 条）"
    )

    t0 = time.monotonic()
    sem = asyncio.Semaphore(args.concurrency)

    async def one(batch):
        async with sem:
            try:
                return await run_group(batch, args, cache, ledger, stats)
            except Exception as e:
                ids = ", ".join(c["id"] for c in batch)
                print(f"  [{ids}] 失败: {type(e).__name__}: {e}", file=sys.stderr)
                return []

    results = [
        r for group in await asyncio.gather(*(one(b) for b in batches)) for r in group
    ]
    results.sort(key=lambda r: r.case_id)
    elapsed = time.monotonic() - t0

    if cache is not None:
        cache.save()

    report(results, cost_of(stats), elapsed)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
