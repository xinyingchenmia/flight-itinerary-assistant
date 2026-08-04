"""用真实取数结果跑完整 pipeline：过滤排序 → 风险审查 agent → 澄清对话 agent。

读 fetch_live.py / import_captured.py 产出的 fetched.json。

澄清对话需要有人回答问题。为了能非交互地跑，这里内置一个「预设旅客画像」
应答器：问到画像里有的信息就照答，问到没有的就答「不确定」——不确定的
回答本身也是有效输入（风险审查该把它标成 unknown 而不是猜）。
真人交互模式用 --interactive。

用法：
    uv run python scripts/run_pipeline.py                      # 审查前 8 个候选
    uv run python scripts/run_pipeline.py --limit 47           # 全量
    uv run python scripts/run_pipeline.py --sort duration --max-stops 1
    uv run python scripts/run_pipeline.py --interactive        # 自己回答澄清问题
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from flight_assistant.budget import (  # noqa: E402
    BudgetExceeded,
    BudgetLedger,
    CostEstimate,
    candidates_covered,
)
from flight_assistant.budget import check as budget_check  # noqa: E402
from flight_assistant.budget import cost_of  # noqa: E402
from flight_assistant.clarification.agent import clarify  # noqa: E402
from flight_assistant.filtering import TripRequest, filter_and_sort  # noqa: E402
from flight_assistant.matching import group_and_compare  # noqa: E402
from flight_assistant.models import Itinerary, PlatformOffer  # noqa: E402
from flight_assistant.recompute import apply_updates  # noqa: E402
from flight_assistant.risk_review.agent import review_all, review_batch  # noqa: E402
from flight_assistant.screening import screen_all  # noqa: E402

# 预设旅客画像。刻意留了空白（托运件数不确定），用来看澄清对话 agent
# 会不会把「不确定」当成结论、还是老实标成未知。
#
# 「落地后去芝加哥市区」是关键设定：它让 arrival_no_ground_transit 这类
# 风险真正可判断——同一个 23:50 落地，去市区和去机场旁的酒店是两种结论。
PERSONA = {
    "签证": "有美国 B1/B2 十年签，2031 年到期",
    "护照": "中国护照，有效期到 2031 年",
    "国籍": "中国",
    "落地": "落地后要去芝加哥市区（Loop 附近的酒店），只坐公共交通，不打车",
    "市区": "去 Loop 附近的酒店，只坐公共交通",
    "交通": "只坐公共交通，不打车",
    "行李": "不确定，还没决定托运几件",
    "改签": "希望可改签，但不是硬要求",
}

# 匹配顺序从具体到笼统——上一次跑的时候「签证」那个问题里也出现了「国籍」
# 二字，先匹配到国籍就答成了"中国"，导致 agent 拿不到签证答案连问三遍。
_ANSWER_ORDER = ["签证", "落地", "市区", "交通", "行李", "改签", "护照", "国籍"]


def _canned_answer(question: str) -> str:
    for key in _ANSWER_ORDER:
        if key in question:
            return PERSONA[key]
    return "这个我不确定。"


def build_answer_fn(interactive: bool):
    async def answer(question: str) -> str:
        print(f"\n  【agent 提问】{question.strip()}")
        if interactive:
            reply = input("  你的回答> ")
        else:
            reply = _canned_answer(question)
            print(f"  【预设应答】{reply}")
        return reply

    return answer


def load_fetched(path: Path) -> list[tuple[Itinerary, PlatformOffer]]:
    raw = json.loads(path.read_text())
    return [
        (
            Itinerary.model_validate(row["itinerary"]),
            PlatformOffer.model_validate(row["offer"]),
        )
        for row in raw
    ]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetched", default="fetched.json")
    ap.add_argument("--limit", type=int, default=8, help="审查前 N 个候选")
    ap.add_argument("--sort", default="price", choices=["price", "duration", "stops"])
    ap.add_argument("--max-stops", type=int, default=None)
    ap.add_argument("--interactive", action="store_true")
    ap.add_argument(
        "--batch-size", type=int, default=8, help="一次调用审查几个候选（越大越省）"
    )
    ap.add_argument("--model", default=None, help="如 claude-sonnet-5，默认用 CLI 默认模型")
    ap.add_argument(
        "--budget",
        type=float,
        default=0.5,
        help="本次查询所有路径的成本上限（美元）。含风险审查各批次 + 澄清对话各轮",
    )
    ap.add_argument("--no-screen", action="store_true", help="跳过确定性粗筛，全部送审")
    ap.add_argument(
        "--web-search",
        action="store_true",
        help="给风险审查 agent 联网检索能力（任意国家/机场都能自己查，不限于预设列表）",
    )
    ap.add_argument("--max-searches", type=int, default=4, help="每批次的搜索次数上限")
    ap.add_argument(
        "--review-model",
        default=None,
        help="风险审查用的模型。审查是成本大头，用便宜模型能腾出额度给澄清对话",
    )
    ap.add_argument(
        "--clarify-reserve",
        type=float,
        default=0.15,
        help="给澄清对话预留的额度。风险审查不许花到这部分",
    )
    args = ap.parse_args()

    path = Path(args.fetched)
    if not path.exists():
        print(f"找不到 {path}，先跑 fetch_live.py 或 import_captured.py", file=sys.stderr)
        return 1

    fetched = load_fetched(path)
    req = TripRequest(
        origin="PVG",
        dest="ORD",
        date="2026-09-27",
        max_stops=args.max_stops,
        sort_pref=args.sort,
    )

    # 步骤 3.5 + 4：确定性代码
    ranked = filter_and_sort(group_and_compare(fetched), req)
    print(f"取数 {len(fetched)} 条报价 → 合并 {len(ranked)} 个候选（排序: {args.sort}）")

    subject = ranked[: args.limit]

    # 确定性粗筛：把没有任何可疑信号的候选挡在 agent 之外
    if not args.no_screen:
        to_review, screen_results = screen_all(subject)
        skipped = [r for r in screen_results if not r.needs_review]
        print(f"粗筛：{len(subject)} 个候选 → {len(to_review)} 个需送审，{len(skipped)} 个跳过")
        for r in skipped:
            segs = r.candidate.itinerary.segments
            route = "→".join([segs[0].dep_airport] + [s.arr_airport for s in segs])
            print(f"  跳过 {route}: {'; '.join(r.reasons)}")
        subject = to_review
        if not subject:
            print("\n没有候选需要 agent 审查（全部通过粗筛）。")
            return 0

    print(f"\n送审 {len(subject)} 个候选\n")
    for i, c in enumerate(subject, 1):
        segs = c.itinerary.segments
        route = "→".join([segs[0].dep_airport] + [s.arr_airport for s in segs])
        conn = ""
        if len(segs) > 1:
            gap = int((segs[1].dep_local - segs[0].arr_local).total_seconds() // 60)
            conn = f" 中转{gap}分钟"
        print(
            f"  {i}. ¥{min(o.price for o in c.offers):>7} {route:16s} "
            f"{c.itinerary.total_duration_min // 60}h{c.itinerary.total_duration_min % 60:02d}m{conn}"
        )

    # 步骤 5：风险审查 agent
    #
    # 先用一个小批次探成本，据此推算全量并对照预算——第一次跑真实数据
    # 时花了 $1.8 才知道单价，那时候已经花掉了。现在超预算直接中止。
    print(f"\n{'=' * 72}\n步骤 5：风险审查 agent\n{'=' * 72}")
    stats: list[dict] = []
    t0 = time.monotonic()

    # 账本覆盖这次查询里所有花钱的调用：风险审查各批次 + 澄清对话各轮。
    ledger = BudgetLedger(cap=args.budget)

    probe_n = min(args.batch_size, len(subject))
    probe = subject[:probe_n]
    try:
        risks_by_key = await review_batch(
            probe,
            stats,
            model=args.review_model or args.model,
            ledger=ledger,
            web_search=args.web_search,
            max_searches=args.max_searches,
            reserve=args.clarify_reserve,
        )
    except BudgetExceeded as e:
        print(f"\n中止: {e}", file=sys.stderr)
        return 2

    est = CostEstimate(
        probe_cost=cost_of(stats),
        probe_candidates=candidates_covered(stats),
        total_candidates=len(subject),
        batch_size=args.batch_size,
    )
    print(f"成本探测: {est.render()}  (预算 ${args.budget:.2f})")
    try:
        budget_check(est, args.budget)
    except BudgetExceeded as e:
        print(f"\n中止: {e}", file=sys.stderr)
        print(ledger.render(), file=sys.stderr)
        return 2

    rest = subject[probe_n:]
    if rest:
        try:
            risks_by_key.update(
                await review_all(
                    rest,
                    stats,
                    batch_size=args.batch_size,
                    model=args.review_model or args.model,
                    ledger=ledger,
                    web_search=args.web_search,
                    max_searches=args.max_searches,
                    reserve=args.clarify_reserve,
                )
            )
        except BudgetExceeded as e:
            print(f"\n预算用尽，只审查了前 {probe_n} 个候选: {e}", file=sys.stderr)
    elapsed = time.monotonic() - t0

    total_risks = 0
    blockers = 0
    needs_input = 0
    for i, c in enumerate(subject, 1):
        risks = risks_by_key[c.itinerary_key]
        total_risks += len(risks)
        blockers += sum(1 for r in risks if r.severity == "blocker")
        needs_input += sum(1 for r in risks if r.needs_user_input)
        segs = c.itinerary.segments
        route = "→".join([segs[0].dep_airport] + [s.arr_airport for s in segs])
        print(f"\n候选 {i} ({route}, ¥{min(o.price for o in c.offers)}):")
        if not risks:
            print("  （无风险）")
        for r in risks:
            tag = " [需追问]" if r.needs_user_input else ""
            print(f"  ● {r.kind}/{r.severity}{tag}")
            print(f"    {r.evidence}")

    print(f"\n{'-' * 72}")
    print(
        f"合计 {total_risks} 条风险 | blocker {blockers} | 需追问 {needs_input} | "
        f"耗时 {elapsed:.1f}s"
    )
    covered = candidates_covered(stats)
    spent = cost_of(stats)
    if covered:
        per = spent / covered
        print(
            f"成本: 本次 ${spent:.4f} / {covered} 个候选 = "
            f"${per:.4f} 每候选 → 全量 {len(ranked)} 个约 ${per * len(ranked):.2f}"
        )
    turns = [s["num_turns"] for s in stats if "num_turns" in s]
    if turns:
        print(f"工具调用轮次: 平均 {sum(turns) / len(turns):.1f} 轮")

    # 步骤 6：澄清对话 agent
    print(f"\n{'=' * 72}\n步骤 6：澄清对话 agent\n{'=' * 72}")
    flagged = sum(
        1 for risks in risks_by_key.values() for r in risks if r.needs_user_input
    )
    print(f"标记 needs_user_input 的风险共 {flagged} 条")
    print(f"应答模式: {'真人交互' if args.interactive else '预设画像'}")
    if not args.interactive:
        print(f"预设画像: {json.dumps(PERSONA, ensure_ascii=False)}")

    print(f"进入澄清前已花费 ${ledger.spent:.4f} / ${ledger.cap:.2f}")
    t0 = time.monotonic()
    updates, trip_ctx = await clarify(
        risks_by_key, build_answer_fn(args.interactive), ledger=ledger
    )
    print(f"\n澄清结束，耗时 {time.monotonic() - t0:.1f}s")
    print(f"\n收集到的用户侧信息 (TripContext):")
    for k, v in trip_ctx.model_dump(mode="json").items():
        mark = "  " if v in (None, "unknown", {}, []) else "✓ "
        print(f"  {mark}{k}: {v}")
    print(f"\n候选字段更新 {len(updates)} 条：")
    for u in updates:
        print(f"  {u.field_path} = {u.value}  ({u.itinerary_key[:40]}…)")

    # 步骤 7：针对性重算
    if updates:
        after, touched = apply_updates(subject, updates)
        after = filter_and_sort(after, req)
        print(f"\n步骤 7：重算了 {len(touched)} 个受影响候选（共 {len(subject)} 个）")
        before_order = [c.itinerary_key for c in subject]
        after_order = [c.itinerary_key for c in after]
        print(f"排序是否改变: {'是' if before_order != after_order else '否'}")

    # 步骤 9：剩余未知项
    unresolved = [
        r for risks in risks_by_key.values() for r in risks if r.needs_user_input
    ]
    print(f"\n剩余未知项 {len(unresolved)} 条（诚实度指标的分子）")

    print(f"\n{'=' * 72}")
    print(ledger.render())
    verdict = "✓ 在预算内" if ledger.spent <= args.budget else "✗ 超预算"
    print(f"{verdict}：本次查询全部路径共花费 ${ledger.spent:.4f}（上限 ${args.budget:.2f}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
