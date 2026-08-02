"""手工验证风险审查 agent 的产出质量。不需要爬虫、不需要真实取数。

目的不是"跑通"，是让你人工判断三件事：
  1. 植入的缺陷有没有被抓出来（漏报 = 让用户误机，权重最高）
  2. evidence 有没有引用具体数字，还是"可能偏紧"这类废话
  3. 干净行程上会不会硬凑风险（误报）

这是评测集的手动版本。看完这三条如果结论是"没用"，就别急着去核对
选择器了。

用法：
    uv run python scripts/try_risk_review.py           # 跑全部 3 个样例
    uv run python scripts/try_risk_review.py tight_mct # 只跑一个
"""

import asyncio
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from flight_assistant.models import (  # noqa: E402
    FlightPriceComparison,
    Itinerary,
    PlatformOffer,
    Segment,
    TicketGroup,
)
from flight_assistant.matching import build_itinerary_key  # noqa: E402
from flight_assistant.risk_review.agent import review_candidate  # noqa: E402


def _seg(carrier, flight_no, dep, arr, dep_t, arr_t, dep_term=None, arr_term=None):
    return Segment(
        carrier=carrier,
        flight_no=flight_no,
        dep_airport=dep,
        dep_terminal=dep_term,
        arr_airport=arr,
        arr_terminal=arr_term,
        dep_local=datetime.fromisoformat(dep_t),
        arr_local=datetime.fromisoformat(arr_t),
    )


def _wrap(itinerary: Itinerary, price: str) -> FlightPriceComparison:
    offer = PlatformOffer(
        platform="ctrip",
        price=Decimal(price),
        currency="CNY",
        fetched_at=datetime.now(),
        booking_url=None,
        fare_conditions_raw=None,
        confidence="confirmed",
    )
    return FlightPriceComparison(
        itinerary_key=build_itinerary_key(itinerary),
        itinerary=itinerary,
        offers=[offer],
        cheapest_platform="ctrip",
        price_spread=Decimal("0"),
    )


# ---------------------------------------------------------------- 样例

def case_tight_mct() -> tuple[Itinerary, str, str]:
    """植入缺陷：NRT 中转只有 40 分钟，且是国际转国际、跨航站楼。

    NRT T1↔T2 之间要坐摆渡车，官方 MCT 远高于 40 分钟。
    期望：mct_tight，severity=blocker，evidence 里出现 40 分钟这个数字。
    """
    it = Itinerary(
        segments=[
            _seg("MU", "MU523", "PVG", "NRT", "2026-09-26T09:00", "2026-09-26T12:50",
                 dep_term="T1", arr_term="T2"),
            _seg("UA", "UA882", "NRT", "ORD", "2026-09-26T13:30", "2026-09-26T11:20",
                 dep_term="T1", arr_term="T5"),
        ],
        tickets=[
            TicketGroup(segment_idx=[0, 1], baggage_through_checked=True,
                        source_platform="ctrip")
        ],
        total_duration_min=1100,
        stop_count=1,
    )
    return it, "5200", "中转 40 分钟 + NRT T2→T1 换航站楼"


def case_split_ticket() -> tuple[Itinerary, str, str]:
    """植入缺陷：拆成两张票（两个 PNR），行李是否直挂未知。

    期望：self_transfer_no_protection（前段延误后段不保护）
    + no_through_baggage 且 needs_user_input=True（因为 None = 未知）。
    衔接时间本身是充裕的（3h05m），不该报 mct_tight。
    """
    it = Itinerary(
        segments=[
            _seg("MU", "MU587", "PVG", "ICN", "2026-09-26T08:30", "2026-09-26T11:40"),
            _seg("KE", "KE037", "ICN", "ORD", "2026-09-26T14:45", "2026-09-26T13:10"),
        ],
        tickets=[
            TicketGroup(segment_idx=[0], baggage_through_checked=None,
                        source_platform="ctrip"),
            TicketGroup(segment_idx=[1], baggage_through_checked=None,
                        source_platform="feizhu"),
        ],
        total_duration_min=1240,
        stop_count=1,
    )
    return it, "4300", "两张票 / 两个 PNR，行李直挂未知，但衔接时间充裕"


def case_clean_direct() -> tuple[Itinerary, str, str]:
    """对照组：干净的直飞，没有植入任何缺陷。

    期望：不报 blocker。如果这条上冒出一堆风险，就是在硬凑，
    误报率会毁掉整个产品的可信度。
    """
    it = Itinerary(
        segments=[
            _seg("UA", "UA850", "PVG", "ORD", "2026-09-26T15:30", "2026-09-26T17:05",
                 dep_term="T2", arr_term="T5"),
        ],
        tickets=[
            TicketGroup(segment_idx=[0], baggage_through_checked=True,
                        source_platform="ctrip")
        ],
        total_duration_min=875,
        stop_count=0,
    )
    return it, "7800", "无（对照组）"


CASES = {
    "tight_mct": case_tight_mct,
    "split_ticket": case_split_ticket,
    "clean_direct": case_clean_direct,
}


async def run_case(name: str) -> None:
    itinerary, price, defect = CASES[name]()
    print("=" * 72)
    print(f"样例: {name}")
    print(f"植入缺陷: {defect}")
    print(f"航段: " + " → ".join(
        f"{s.dep_airport}{'/' + s.dep_terminal if s.dep_terminal else ''}"
        f"({s.dep_local:%H:%M})" for s in itinerary.segments
    ) + f" → {itinerary.segments[-1].arr_airport}")
    print(f"票数: {len(itinerary.tickets)} 张 PNR")
    print("-" * 72)

    try:
        risks = await review_candidate(_wrap(itinerary, price))
    except Exception as e:
        print(f"[失败] {type(e).__name__}: {e}")
        return

    if not risks:
        print("(agent 没有报告任何风险)")
    for r in risks:
        flag = " [需追问]" if r.needs_user_input else ""
        print(f"\n● {r.kind} / {r.severity}{flag}")
        print(f"  affected_segments: {r.affected_segments}")
        print(f"  evidence: {r.evidence}")
        if r.prob is not None:
            print(f"  prob: {r.prob}")
        if r.cost_if_realized is not None:
            print(f"  cost_if_realized: {r.cost_if_realized}")
    print()


async def main() -> int:
    names = sys.argv[1:] or list(CASES)
    for n in names:
        if n not in CASES:
            print(f"未知样例 {n}，可选: {', '.join(CASES)}", file=sys.stderr)
            return 2
        await run_case(n)

    print("=" * 72)
    print("""人工判断这三条：
  1. 漏报？tight_mct 应报 mct_tight/blocker；split_ticket 应报
     self_transfer_no_protection + no_through_baggage。
  2. evidence 有没有具体数字（"40 分钟 < 官方 MCT"），还是
     "可能偏紧"这种没依据的话？
  3. clean_direct 上有没有硬凑风险？

注意：v0 的三个工具都返回 no_data，所以 agent 拿不到官方 MCT 数值。
它应该老老实实标 needs_user_input，而不是编一个 MCT 数字出来——
如果它编了，那是 system prompt 的约束没生效，比漏报更值得警惕。""")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
