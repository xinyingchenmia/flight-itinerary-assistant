"""澄清对话 agent 的工具。

只有一个工具：把用户的回答落成一条字段更新。agent 不自己算结果——
拿到答案就调这个工具，之后由 recompute.py 的确定性代码重算。
"""

from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from flight_assistant.recompute import FieldUpdate


class UpdateCollector:
    """收集 agent 产生的字段更新，交回确定性重算。"""

    def __init__(self) -> None:
        self.updates: list[FieldUpdate] = []

    def add(self, itinerary_key: str, field_path: str, value: object) -> None:
        self.updates.append(FieldUpdate(itinerary_key, field_path, value))


def build_server(collector: UpdateCollector):
    @tool(
        "update_itinerary_field",
        "把用户的回答写回某个候选行程的字段，之后由确定性代码重算",
        {"itinerary_key": str, "field_path": str, "value": str},
    )
    async def update_itinerary_field(args: dict[str, Any]) -> dict[str, Any]:
        collector.add(args["itinerary_key"], args["field_path"], args["value"])
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"已记录：{args['itinerary_key']} 的 "
                        f"{args['field_path']} = {args['value']}。"
                        "重算由确定性代码负责，你不用自己算结果。"
                    ),
                }
            ]
        }

    return create_sdk_mcp_server(
        name="clarify_tools",
        version="0.1.0",
        tools=[update_itinerary_field],
    )


ALLOWED_TOOLS = ["mcp__clarify_tools__update_itinerary_field"]
