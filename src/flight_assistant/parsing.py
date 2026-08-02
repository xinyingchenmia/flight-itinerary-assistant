"""步骤 2：自然语言 → 结构化请求。

单轮 LLM 抽取，不是 agent：一次 API 调用、强制走一个工具、拿到 JSON
就返回，不存在"决定下一步做什么"的循环。
"""

import json

from anthropic import Anthropic

from flight_assistant.filtering import TripRequest

MODEL = "claude-sonnet-5"

_EXTRACT_TOOL = {
    "name": "emit_trip_request",
    "description": "把用户的行程需求抽取成结构化字段",
    "input_schema": {
        "type": "object",
        "properties": {
            "origin": {
                "type": "string",
                "description": "出发地三字机场码或城市码，如 SHA/PVG",
            },
            "dest": {"type": "string", "description": "目的地三字机场码或城市码"},
            "date": {"type": "string", "description": "出发日期 YYYY-MM-DD"},
            "max_stops": {
                "type": ["integer", "null"],
                "description": "最多可接受的中转次数；用户没提就填 null",
            },
            "sort_pref": {
                "type": "string",
                "enum": ["duration", "price", "stops"],
                "description": "排序偏好；用户没明确说就填 price",
            },
        },
        "required": ["origin", "dest", "date", "max_stops", "sort_pref"],
    },
}

_SYSTEM = (
    "你把用户的中文行程需求抽取成结构化字段。只抽取用户明确说了的信息，"
    "没说的按 schema 里的说明填默认值或 null，不要推测用户没提的约束。"
)


def parse_request(text: str, client: Anthropic | None = None) -> TripRequest:
    client = client or Anthropic()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=_SYSTEM,
        tools=[_EXTRACT_TOOL],
        tool_choice={"type": "tool", "name": "emit_trip_request"},
        messages=[{"role": "user", "content": text}],
    )
    for block in resp.content:
        if block.type == "tool_use":
            return TripRequest.model_validate(block.input)
    raise RuntimeError(f"结构化抽取未返回 tool_use: {json.dumps(resp.model_dump())}")
