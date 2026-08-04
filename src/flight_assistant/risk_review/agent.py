"""Agent 1：风险审查。

对每个候选行程，找出会导致行程失败或体验劣化的具体原因。这是项目里
两个 agent 之一——需要运行时判断"该查什么、查几次、够不够下结论"，
所以用 agent 循环；联程保护等能用代码算的事实提前算好传进来。
"""

import asyncio
import json
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, query

from flight_assistant.models import FlightPriceComparison, Risk, TripContext
from flight_assistant.risk_review import tools as risk_tools

SYSTEM_PROMPT = """你审查机票行程的风险，输出结构化的 Risk 列表。

## 你的定位：只报订票平台上看不到的东西

用户在携程/飞猪上已经能看到价格、时长、中转次数、行李额度、退改政策。
把这些复述一遍毫无价值。你的价值在于那些订票页面不会告诉他、但会真正
毁掉行程的事：

1. **需要入境/清关的中转，时间够不够**
   美国是最典型的：在美国的第一个入境口岸必须入境、提取托运行李、
   过海关、重新托运、再过一次安检——即使后续航段是国内段、即使是同
   一张票。这个流程通常要 90-180 分钟，远超普通国际转机的 MCT。
   这条规则本身是稳定的，你可以直接引用；但具体机场的排队时长和官方
   MCT 数值要查工具。加拿大、申根区内外转换也有类似情况。
   → kind = entry_connection_tight

2. **深夜/凌晨落地后到不了目的地**
   落地时间晚 + 目的地在市区 = 可能没有地面交通。判断要用
   trip_context.destination_after_arrival：去市区和去机场旁的酒店是
   两种结论；ground_transport_ok = public_only 时打车不算退路。
   还要算上下机、入境、取行李的时间——落地时刻不是出航站楼时刻。
   → kind = arrival_no_ground_transit

3. **过境签 / 落地签 / 通程行李的真实约束**
   取决于国籍和是否需要离开管制区。查工具，不要凭记忆。
   → kind = transit_visa_required / no_through_baggage

4. **拆票行程的连带风险**
   前段延误后段不保护、行李不直挂、需要自己重新值机。
   → kind = self_transfer_no_protection

5. **末班航班 / 换航站楼 / 护照有效期**

## 硬约束

- 每条 evidence 必须引用具体数字或具体规则。"衔接 95 分钟，而 ORD 是本
  行程在美国的首个入境口岸，需入境+提取行李+重新托运+二次安检" 是合格的；
  "转机时间可能偏紧" 不合格。
- 不要报订票平台上已经明示的信息（行李额度、退改条款原文、价格构成）。
  只有当它和其他条件组合出平台没提示的后果时才报。
- 只报真正影响决策的风险，不要为了显得全面而堆砌小概率事件。宁可 2 条
  切中要害，不要 8 条里 6 条是废话。
- 查不到或无法确定的，标 needs_user_input=true。工具返回 no_data 就是
  查不到，不要用训练数据里的旧知识补上——签证政策和交通时刻变化快，
  编一个数字比承认未知更危险。
  例外：上面第 1 条里"美国首个入境口岸必须提取行李重新托运"这类稳定的
  制度性规则可以直接引用，但涉及具体时长/官方 MCT 数值仍须查工具。
- trip_context 里为 null 的字段表示用户还没提供。如果某条风险的成立与否
  取决于它，标 needs_user_input=true 并在 evidence 里说清缺哪一项。

## 输入里已经用代码算好的字段（直接采信，不要重复推断）

- ticket_count / has_through_protection：联程保护情况
- baggage_through_checked：null 表示未知，必须 flag
- connections：各中转点的衔接分钟数、是否换航站楼、是否跨国
- trip_context：用户侧信息，null 表示未知
"""

_RISK_SCHEMA = {
    "type": "object",
    "properties": {
        "risks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": [
                            "mct_tight",
                            "entry_connection_tight",
                            "no_through_baggage",
                            "self_transfer_no_protection",
                            "transit_visa_required",
                            "last_flight_of_day",
                            "terminal_change",
                            "arrival_no_ground_transit",
                            "passport_validity",
                        ],
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["blocker", "major", "minor"],
                    },
                    "evidence": {"type": "string"},
                    "affected_segments": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                    "needs_user_input": {"type": "boolean"},
                    "prob": {"type": ["number", "null"]},
                    "cost_if_realized": {"type": ["number", "null"]},
                },
                "required": [
                    "kind",
                    "severity",
                    "evidence",
                    "affected_segments",
                    "needs_user_input",
                    "prob",
                    "cost_if_realized",
                ],
            },
        }
    },
    "required": ["risks"],
}


def build_connections(it) -> list[dict[str, Any]]:
    """各中转点的衔接情况。确定性计算，不让 agent 自己减时刻。

    换航站楼、跨国、衔接分钟数都是从数据直接算出来的事实——agent 需要的是
    在这些事实之上做判断（够不够、要不要入境），不是重新算一遍减法。
    """
    out = []
    for i in range(len(it.segments) - 1):
        arr, dep = it.segments[i], it.segments[i + 1]
        out.append(
            {
                "at_airport": arr.arr_airport,
                "after_segment": i,
                "gap_min": int((dep.dep_local - arr.arr_local).total_seconds() // 60),
                "arr_terminal": arr.arr_terminal,
                "dep_terminal": dep.dep_terminal,
                "terminal_change": (
                    None
                    if arr.arr_terminal is None or dep.dep_terminal is None
                    else arr.arr_terminal != dep.dep_terminal
                ),
                "same_carrier": arr.carrier == dep.carrier,
            }
        )
    return out


def build_context(
    candidate: FlightPriceComparison, trip_context: TripContext | None = None
) -> dict[str, Any]:
    """把代码能确定算出的事实预先算好，避免 agent 去推断。

    联程保护判断刻意不做成工具：len(tickets) > 1 是确定性计算，
    包成工具调用只是多一次往返。衔接时长同理。
    """
    it = candidate.itinerary
    return {
        "itinerary_key": candidate.itinerary_key,
        "segments": [s.model_dump(mode="json") for s in it.segments],
        "connections": build_connections(it),
        "total_duration_min": it.total_duration_min,
        "stop_count": it.stop_count,
        "ticket_count": len(it.tickets),
        "has_through_protection": len(it.tickets) == 1,
        "baggage_through_checked": [
            t.baggage_through_checked for t in it.tickets
        ],
        "cheapest_platform": candidate.cheapest_platform,
        "trip_context": (trip_context or TripContext()).model_dump(mode="json"),
    }


def build_options() -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        mcp_servers={"risk_tools": risk_tools.build_server()},
        allowed_tools=risk_tools.ALLOWED_TOOLS,
        output_format={"type": "json_schema", "schema": _RISK_SCHEMA},
    )


async def review_candidate(
    candidate: FlightPriceComparison,
    stats: list[dict] | None = None,
    trip_context: TripContext | None = None,
) -> list[Risk]:
    """审查单个候选，返回 Risk 列表。

    stats 不为 None 时，往里追加一条本次调用的成本/耗时记录——评测集里的
    「单次审计成本 vs 可避免损失」指标需要这个数据。
    """
    ctx = build_context(candidate, trip_context)
    prompt = (
        "审查这个候选行程的风险，按 schema 输出 risks 数组：\n"
        + json.dumps(ctx, ensure_ascii=False, indent=2)
    )

    raw: str | None = None
    meta: dict = {"itinerary_key": ctx["itinerary_key"]}
    async for message in query(prompt=prompt, options=build_options()):
        result = getattr(message, "result", None)
        if result is not None:
            if getattr(message, "is_error", False) or (
                getattr(message, "subtype", "success") != "success"
            ):
                raise RuntimeError(f"风险审查失败（API 层错误）: {result}")
            raw = result
            for attr in ("total_cost_usd", "duration_ms", "num_turns", "usage"):
                value = getattr(message, attr, None)
                if value is not None:
                    meta[attr] = value

    if stats is not None:
        stats.append(meta)

    if raw is None:
        raise RuntimeError(f"风险审查未返回结果: {ctx['itinerary_key']}")

    payload = json.loads(raw) if isinstance(raw, str) else raw
    return [Risk.model_validate(r) for r in payload["risks"]]


async def review_all(
    candidates: list[FlightPriceComparison],
    stats: list[dict] | None = None,
    concurrency: int = 6,
    trip_context: TripContext | None = None,
) -> dict[str, list[Risk]]:
    """并发审查多个候选。

    每个候选是独立的一次 agent 循环（互不依赖），串行跑纯属浪费——实测
    6 个候选串行 382 秒，其中大部分时间在等工具往返。concurrency 限制
    同时在跑的数量，避免一次性打出几十个并发请求。
    """
    sem = asyncio.Semaphore(concurrency)

    async def one(c: FlightPriceComparison) -> tuple[str, list[Risk]]:
        async with sem:
            return c.itinerary_key, await review_candidate(c, stats, trip_context)

    pairs = await asyncio.gather(*(one(c) for c in candidates))
    return dict(pairs)
