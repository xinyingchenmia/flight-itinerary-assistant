"""Agent 2：澄清对话。

只处理风险审查标记 needs_user_input=True 的项，判断哪些信息缺口真正
会改变排序结果，只问这些。这是项目里第二个（也是最后一个）agent——
"哪个问题值得问、什么时候问够了"没法用规则穷举，所以用 agent。

代码只负责：把风险列表喂进去、把用户回答转发进去、读停止信号。
真正的价值判断在模型这一侧。
"""

import json
from collections.abc import Awaitable, Callable

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from flight_assistant.clarification import tools as clarify_tools
from flight_assistant.clarification.tools import UpdateCollector
from flight_assistant.models import Risk
from flight_assistant.recompute import FieldUpdate

SYSTEM_PROMPT = """你负责就机票行程里的信息缺口追问用户。

行为约束：
- 一次只问一个问题，不要一口气甩多个。
- 判断标准只有一条：这个问题的答案会不会改变候选行程的排序或过滤
  结果。不会改变的就不要问——这不是固定问卷，不需要走完所有项。
- 拿到答案后调用 update_itinerary_field 把答案落到具体字段，然后交回
  确定性代码重算。你自己不要计算排序结果或价格。
- 判断没有更多高价值问题时，回复一行 DONE 并列出剩余的未知项，
  结束追问。
"""

# 用户答复的获取方式由调用方注入：CLI 里是 input()，测试里是预置脚本。
AnswerFn = Callable[[str], Awaitable[str]]

DONE_MARKER = "DONE"


def build_options(collector: UpdateCollector) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        mcp_servers={"clarify_tools": clarify_tools.build_server(collector)},
        allowed_tools=clarify_tools.ALLOWED_TOOLS,
    )


def _pending(risks_by_key: dict[str, list[Risk]]) -> dict[str, list[dict]]:
    """只保留 needs_user_input=True 的项。"""
    out: dict[str, list[dict]] = {}
    for key, risks in risks_by_key.items():
        flagged = [r.model_dump(mode="json") for r in risks if r.needs_user_input]
        if flagged:
            out[key] = flagged
    return out


async def clarify(
    risks_by_key: dict[str, list[Risk]],
    answer_fn: AnswerFn,
    max_turns: int = 8,
) -> list[FieldUpdate]:
    """跑澄清对话，返回要交给 recompute 的字段更新列表。

    max_turns 只是防跑飞的上限，正常情况下由 agent 自己判断问完即止。
    """
    pending = _pending(risks_by_key)
    if not pending:
        return []

    collector = UpdateCollector()
    async with ClaudeSDKClient(options=build_options(collector)) as client:
        await client.query(
            "以下是需要用户澄清的风险项。挑出真正会改变排序的那些，"
            "一次问一个：\n"
            + json.dumps(pending, ensure_ascii=False, indent=2)
        )

        for _ in range(max_turns):
            reply = ""
            async for message in client.receive_response():
                result = getattr(message, "result", None)
                if result is not None:
                    reply = result

            if not reply or DONE_MARKER in reply:
                break

            answer = await answer_fn(reply)
            await client.query(answer)

    return collector.updates
