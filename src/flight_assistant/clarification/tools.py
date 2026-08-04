"""澄清对话 agent 的工具。

只有一个工具：把用户的回答落成一条字段更新。agent 不自己算结果——
拿到答案就调这个工具，之后由 recompute.py 的确定性代码重算。

字段路径必须在白名单里。实测未加约束时 agent 会写 passenger.nationality
这种数据模型里不存在的路径，一路传到 recompute 才 KeyError 崩掉。白名单
让错误在工具调用当场就被拒绝，并把可写字段告诉 agent，让它自己纠正。
"""

import re
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from flight_assistant.models import TripContext
from flight_assistant.recompute import FieldUpdate

# 可写字段白名单。加新字段时同步更新，并确认 recompute._set_by_path 能走到。
#
# trip_context.* 是用户侧信息（国籍、落地后去哪、能不能打车），存在
# TripContext 上而不是某个候选上——这些答案对所有候选都适用，写进单个
# 候选没有意义。所以它们走 update_trip_context 工具，不走这里。
ALLOWED_PATHS: tuple[re.Pattern, ...] = (
    re.compile(r"^itinerary\.tickets\.\d+\.baggage_through_checked$"),
)

_PATHS_HELP = "itinerary.tickets.{N}.baggage_through_checked（N 为票序号，从 0 开始）"

# TripContext 上可写的字段。和 models.TripContext 保持同步。
TRIP_CONTEXT_FIELDS = {
    "nationality",
    "passport_expiry",
    "destination_after_arrival",
    "ground_transport_ok",
    "checked_bags",
    "layover_preference",
}


def path_allowed(path: str) -> bool:
    return any(p.match(path) for p in ALLOWED_PATHS)


class UpdateCollector:
    """收集 agent 产生的更新，交回确定性重算。"""

    def __init__(self, trip_context: TripContext | None = None) -> None:
        self.updates: list[FieldUpdate] = []
        self.rejected: list[tuple[str, str]] = []  # (field_path, 原因)
        self.trip_context = trip_context or TripContext()

    def add(self, itinerary_key: str, field_path: str, value: object) -> None:
        self.updates.append(FieldUpdate(itinerary_key, field_path, value))

    def set_trip_field(self, field: str, value: str) -> None:
        """写 TripContext 字段。经 pydantic 校验，类型不对会抛出来。"""
        data = self.trip_context.model_dump()
        data[field] = value
        self.trip_context = TripContext.model_validate(data)


def build_server(collector: UpdateCollector):
    @tool(
        "update_itinerary_field",
        "把用户的回答写回某个候选行程的字段，之后由确定性代码重算",
        {"itinerary_key": str, "field_path": str, "value": str},
    )
    async def update_itinerary_field(args: dict[str, Any]) -> dict[str, Any]:
        path = args["field_path"]
        if not path_allowed(path):
            collector.rejected.append((path, "不在白名单"))
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"拒绝：{path} 不是可写字段。当前数据模型里只有这些"
                            f"字段可以写回：{_PATHS_HELP}。\n"
                            "旅客国籍/签证/护照这类信息目前没有对应字段，"
                            "写不进去——这类答案请直接在最终说明里陈述，"
                            "不要反复追问同一个问题。"
                        ),
                    }
                ]
            }

        collector.add(args["itinerary_key"], path, args["value"])
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"已记录：{args['itinerary_key']} 的 {path} = {args['value']}。"
                        "重算由确定性代码负责，你不用自己算结果。"
                    ),
                }
            ]
        }

    @tool(
        "update_trip_context",
        "记录用户侧信息（国籍、护照有效期、落地后去哪、能否打车、托运件数）。"
        "这些对所有候选都适用，只需记一次",
        {"field": str, "value": str},
    )
    async def update_trip_context(args: dict[str, Any]) -> dict[str, Any]:
        field, value = args["field"], args["value"]
        if field not in TRIP_CONTEXT_FIELDS:
            collector.rejected.append((f"trip_context.{field}", "不是已知字段"))
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"拒绝：trip_context 没有 {field} 这个字段。"
                            f"可写字段：{', '.join(sorted(TRIP_CONTEXT_FIELDS))}。"
                        ),
                    }
                ]
            }
        try:
            collector.set_trip_field(field, value)
        except Exception as e:
            collector.rejected.append((f"trip_context.{field}", str(e)))
            return {
                "content": [
                    {"type": "text", "text": f"拒绝：{field} 的值 {value!r} 不合法 — {e}"}
                ]
            }
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"已记录 trip_context.{field} = {value}。这一项对所有候选"
                        "都生效，不用逐个候选重复记录。"
                    ),
                }
            ]
        }

    return create_sdk_mcp_server(
        name="clarify_tools",
        version="0.1.0",
        tools=[update_itinerary_field, update_trip_context],
    )


ALLOWED_TOOLS = [
    "mcp__clarify_tools__update_itinerary_field",
    "mcp__clarify_tools__update_trip_context",
]
