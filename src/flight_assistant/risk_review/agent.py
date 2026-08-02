"""Agent 1：风险审查。

对每个候选行程，找出会导致行程失败或体验劣化的具体原因。这是项目里
两个 agent 之一——需要运行时判断"该查什么、查几次、够不够下结论"，
所以用 agent 循环；联程保护等能用代码算的事实提前算好传进来。
"""

import json
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, query

from flight_assistant.models import FlightPriceComparison, Risk
from flight_assistant.risk_review import tools as risk_tools

SYSTEM_PROMPT = """你审查机票行程的风险，输出结构化的 Risk 列表。

硬约束：
- 每条 evidence 必须引用具体数字/规则（例如"PVG T2→T1 换航站楼，
  衔接 45 分钟 < 官方 MCT 90 分钟"），禁止"可能偏紧"这类无依据表述。
- 只报告真正影响决策的风险。不要为了显得全面而堆砌小概率事件。
- 查不到或无法确定的，标 needs_user_input=true，不许猜测后当结论输出。
  工具返回 no_data 时，这就是"查不到"，不要用你训练数据里的旧知识补上——
  签证政策和交通时刻变化快，编一个数字比承认未知更危险。
- 需要外部信息时调用工具查询，不要凭记忆回答。

输入里已经用代码算好的字段（不要重复推断，直接采信）：
- ticket_count / has_through_protection：联程保护情况
- baggage_through_checked：null 表示未知，必须 flag
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


def build_context(candidate: FlightPriceComparison) -> dict[str, Any]:
    """把代码能确定算出的事实预先算好，避免 agent 去推断。

    联程保护判断刻意不做成工具：len(tickets) > 1 是确定性计算，
    包成工具调用只是多一次往返。
    """
    it = candidate.itinerary
    return {
        "itinerary_key": candidate.itinerary_key,
        "segments": [s.model_dump(mode="json") for s in it.segments],
        "total_duration_min": it.total_duration_min,
        "stop_count": it.stop_count,
        "ticket_count": len(it.tickets),
        "has_through_protection": len(it.tickets) == 1,
        "baggage_through_checked": [
            t.baggage_through_checked for t in it.tickets
        ],
        "cheapest_platform": candidate.cheapest_platform,
    }


def build_options() -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        mcp_servers={"risk_tools": risk_tools.build_server()},
        allowed_tools=risk_tools.ALLOWED_TOOLS,
        output_format={"type": "json_schema", "schema": _RISK_SCHEMA},
    )


async def review_candidate(candidate: FlightPriceComparison) -> list[Risk]:
    """审查单个候选，返回 Risk 列表。"""
    ctx = build_context(candidate)
    prompt = (
        "审查这个候选行程的风险，按 schema 输出 risks 数组：\n"
        + json.dumps(ctx, ensure_ascii=False, indent=2)
    )

    raw: str | None = None
    async for message in query(prompt=prompt, options=build_options()):
        result = getattr(message, "result", None)
        if result is not None:
            raw = result

    if raw is None:
        raise RuntimeError(f"风险审查未返回结果: {ctx['itinerary_key']}")

    payload = json.loads(raw) if isinstance(raw, str) else raw
    return [Risk.model_validate(r) for r in payload["risks"]]


async def review_all(
    candidates: list[FlightPriceComparison],
) -> dict[str, list[Risk]]:
    return {c.itinerary_key: await review_candidate(c) for c in candidates}
