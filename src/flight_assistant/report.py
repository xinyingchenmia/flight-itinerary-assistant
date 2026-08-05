"""最终结果渲染：把查到的每一条信息都给用户看。

## 为什么单独一层

之前风险、已确认项、偏好结论散在调试输出里，用户看不到完整画面。而
"半夜落地还有没有地铁进市区"这种**已确认没问题**的结论恰恰是用户最想
读的——只报坏消息的话，他不知道哪些事已经替他确认过。

渲染层不做任何判断，只负责把 agent 的结论按用户关心的顺序摆出来：
先结论（能不能用），再问题（严重的在前），再已确认项，最后待确认。
"""

from decimal import Decimal

from flight_assistant.models import (
    Assurance,
    FlightPriceComparison,
    PreferenceNote,
    Risk,
)

SEVERITY_ORDER = {"blocker": 0, "major": 1, "minor": 2}
SEVERITY_LABEL = {
    "blocker": "无法成行",
    "major": "重要",
    "minor": "留意",
}
VERDICT_ICON = {"good": "☺", "poor": "☹", "unknown": "?"}


def _route(c: FlightPriceComparison) -> str:
    segs = c.itinerary.segments
    return " → ".join([segs[0].dep_airport] + [s.arr_airport for s in segs])


def _duration(minutes: int) -> str:
    return f"{minutes // 60}h{minutes % 60:02d}m"


def _connections(c: FlightPriceComparison) -> str:
    segs = c.itinerary.segments
    if len(segs) == 1:
        return "直飞"
    parts = []
    for i in range(len(segs) - 1):
        gap = int((segs[i + 1].dep_local - segs[i].arr_local).total_seconds() // 60)
        parts.append(f"{segs[i].arr_airport} 停 {_duration(gap)}")
    return " / ".join(parts)


def render_candidate(
    rank: int,
    c: FlightPriceComparison,
    risks: list[Risk],
    assurances: list[Assurance],
    notes: list[PreferenceNote],
) -> str:
    it = c.itinerary
    cheapest = min(o.price for o in c.offers)
    platforms = sorted({o.platform for o in c.offers})
    spread = max(o.price for o in c.offers) - cheapest

    lines: list[str] = []
    head = (
        f"{rank}. ¥{cheapest}  {_route(c)}  "
        f"{_duration(it.total_duration_min)}  {_connections(c)}"
    )
    lines.append(head)

    flights = " + ".join(f"{s.carrier}{s.flight_no[len(s.carrier):]}" for s in it.segments)
    detail = f"   航班 {flights}"
    if len(it.tickets) > 1:
        detail += f"  ⚠ 分 {len(it.tickets)} 张票"
    lines.append(detail)

    price_line = f"   {'/'.join(platforms)}"
    if spread > 0:
        price_line += f"，同行程各平台差价 ¥{spread}"
    lines.append(price_line)

    blockers = [r for r in risks if r.severity == "blocker"]
    if blockers:
        lines.append("   ⛔ 这个方案可能走不通：")

    for r in sorted(risks, key=lambda x: SEVERITY_ORDER[x.severity]):
        tag = "（待你确认）" if r.needs_user_input else ""
        lines.append(f"   ● [{SEVERITY_LABEL[r.severity]}]{tag} {r.evidence}")

    for a in assurances:
        lines.append(f"   ✓ {a.statement}")
        lines.append(f"     依据：{a.evidence}")

    for n in notes:
        lines.append(f"   {VERDICT_ICON[n.verdict]} 关于「{n.preference}」：{n.statement}")
        if n.verdict != "unknown":
            lines.append(f"     依据：{n.evidence}")

    if not risks and not assurances and not notes:
        lines.append("   （未发现需要提示的问题）")

    return "\n".join(lines)


def render(
    ranked: list[FlightPriceComparison],
    findings: dict,
    missing_platforms: list[str] | None = None,
    trip_context=None,
    cost_usd: float | None = None,
    elapsed_s: float | None = None,
) -> str:
    """完整结果。findings 是 {itinerary_key: (risks, assurances, notes)}。"""
    out: list[str] = []
    out.append("=" * 74)
    out.append(f"共 {len(ranked)} 个候选")
    out.append("=" * 74)

    unresolved: list[Risk] = []
    for i, c in enumerate(ranked, 1):
        risks, assurances, notes = findings.get(c.itinerary_key, ([], [], []))
        unresolved += [r for r in risks if r.needs_user_input]
        out.append("")
        out.append(render_candidate(i, c, risks, assurances, notes))

    # 未确认项集中列一遍——用户需要知道哪些结论是有前提的
    if unresolved:
        out.append("")
        out.append("-" * 74)
        out.append(f"以下 {len(unresolved)} 项还需要你确认或自行核实：")
        seen: set[str] = set()
        for r in unresolved:
            key = r.kind
            if key in seen:
                continue
            seen.add(key)
            out.append(f"  · {r.evidence}")

    if missing_platforms:
        out.append("")
        out.append(
            f"⚠ 以下平台本次取数失败，价格可能不是全网最低：{', '.join(missing_platforms)}"
        )

    if cost_usd is not None or elapsed_s is not None:
        out.append("")
        bits = []
        if elapsed_s is not None:
            bits.append(f"耗时 {elapsed_s:.0f}s")
        if cost_usd is not None:
            bits.append(f"成本 ${cost_usd:.4f}")
        out.append("（" + "，".join(bits) + "）")

    return "\n".join(out)


def summarize_findings(findings: dict) -> dict[str, int]:
    """给指标用的汇总。"""
    risks = [r for rs, _, _ in findings.values() for r in rs]
    return {
        "risks": len(risks),
        "blockers": sum(1 for r in risks if r.severity == "blocker"),
        "needs_input": sum(1 for r in risks if r.needs_user_input),
        "assurances": sum(len(a) for _, a, _ in findings.values()),
        "preference_notes": sum(len(n) for _, _, n in findings.values()),
    }


def total_price_spread(ranked: list[FlightPriceComparison]) -> Decimal:
    """所有候选里最大的跨平台价差。只有接了多个平台才有意义。"""
    spreads = [
        max(o.price for o in c.offers) - min(o.price for o in c.offers)
        for c in ranked
        if len(c.offers) > 1
    ]
    return max(spreads) if spreads else Decimal("0")
