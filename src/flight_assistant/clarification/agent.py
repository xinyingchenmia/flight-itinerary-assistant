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

from flight_assistant.budget import BudgetExceeded, BudgetLedger
from flight_assistant.clarification import tools as clarify_tools
from flight_assistant.clarification.tools import UpdateCollector
from flight_assistant.models import Risk, TripContext
from flight_assistant.recompute import FieldUpdate

SYSTEM_PROMPT = """你负责就机票行程里的信息缺口追问用户。

行为约束：
- 一次只问一个问题，不要一口气甩多个。
- 判断标准只有一条：这个问题的答案会不会改变候选行程的排序或过滤
  结果。不会改变的就不要问——这不是固定问卷，不需要走完所有项。
- 拿到答案后立刻用工具落盘，然后交回确定性代码重算，你自己不要计算排序
  或价格：
    · 用户侧信息（国籍、护照有效期、落地后去哪、能否打车、托运件数）
      → update_trip_context，只需记一次，对所有候选生效
    · 某个候选特有的信息（某张票的行李是否直挂）
      → update_itinerary_field
- 同一个问题不要问第二遍。如果用户的回答没能解决某一项（含答"不确定"），
  就把它当作无法确定，记进剩余未知项，然后继续下一个问题或结束。
- 工具拒绝了某个字段路径，说明该信息在当前数据模型里没有落点。不要换个
  措辞重问同一件事——直接在结束语里说明这一项无法写回。
- **拿到答案后第一件事就是调工具落盘**，不要先解释、不要先问下一个问题。
  预算可能在任何一轮耗尽，没落盘的答案等于白问。
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


def _risks_of(findings) -> list[Risk]:
    """findings 是 (risks, assurances)；澄清只关心 risks。"""
    return findings[0] if isinstance(findings, tuple) else findings


def _pending(risks_by_key: dict) -> dict[str, list[dict]]:
    """只保留 needs_user_input=True 的项。"""
    out: dict[str, list[dict]] = {}
    for key, findings in risks_by_key.items():
        risks = _risks_of(findings)
        flagged = [r.model_dump(mode="json") for r in risks if r.needs_user_input]
        if flagged:
            out[key] = flagged
    return out


def _compact_pending(risks_by_key: dict) -> list[dict]:
    """把待澄清项压缩成按风险类型聚合的紧凑清单。

    为什么要压：实测一轮澄清对话花了 $0.1511，因为每轮都把 11 条风险的
    完整 evidence（每条 150 字）全塞进上下文。而 agent 决定"该问什么"只
    需要知道：有哪些类型的缺口、各影响几个候选、各缺哪个字段。完整
    evidence 是给用户看的，不是给它决策用的。

    同一类型的风险在多个候选上重复出现是常态（4 个候选全都缺国籍），
    聚合后一条就够，agent 也更容易看出"问一次能解决 4 个候选"。
    """
    by_kind: dict[str, dict] = {}
    for key, findings in risks_by_key.items():
        for r in _risks_of(findings):
            if not r.needs_user_input:
                continue
            slot = by_kind.setdefault(
                r.kind,
                {
                    "kind": r.kind,
                    "severity": r.severity,
                    "affected_candidates": [],
                    "sample_evidence": r.evidence[:120],
                },
            )
            slot["affected_candidates"].append(key[:28])
            # 多个候选里取最严重的那个等级
            order = {"blocker": 0, "major": 1, "minor": 2}
            if order[r.severity] < order[slot["severity"]]:
                slot["severity"] = r.severity
    for slot in by_kind.values():
        slot["affected_count"] = len(slot["affected_candidates"])
        slot.pop("affected_candidates")
    return sorted(
        by_kind.values(),
        key=lambda s: ({"blocker": 0, "major": 1, "minor": 2}[s["severity"]], -s["affected_count"]),
    )


async def clarify(
    risks_by_key: dict,
    answer_fn: AnswerFn,
    max_turns: int = 8,
    trip_context: TripContext | None = None,
    ledger: BudgetLedger | None = None,
) -> tuple[list[FieldUpdate], TripContext]:
    """跑澄清对话，返回 (字段更新列表, 更新后的 TripContext)。

    max_turns 只是防跑飞的上限，正常情况下由 agent 自己判断问完即止。

    ledger 不为 None 时，**每一轮**对话前都检查预算。澄清对话是多轮的，
    每轮都花钱——之前这条路径完全没有预算约束，所谓"每次查询不超过 X"
    是假的。预算用尽时不抛异常，而是带着已收集到的答案正常返回：中途
    停止追问仍然是有效结果，剩下的项留在 unresolved 里。
    """
    pending = _pending(risks_by_key)
    if not pending:
        return [], trip_context or TripContext()

    collector = UpdateCollector(trip_context)
    async with ClaudeSDKClient(options=build_options(collector)) as client:
        await client.query(
            "以下是需要用户澄清的信息缺口，已按风险类型聚合"
            "（affected_count = 影响几个候选）。挑出真正会改变排序的那些，"
            "一次问一个。当前 trip_context:\n"
            + json.dumps(
                (trip_context or TripContext()).model_dump(mode="json"),
                ensure_ascii=False,
            )
            + "\n\n缺口清单:\n"
            + json.dumps(
                _compact_pending(risks_by_key), ensure_ascii=False, indent=1
            )
        )

        for turn in range(max_turns):
            reply = ""
            errored = False
            async for message in client.receive_response():
                result = getattr(message, "result", None)
                if result is not None:
                    reply = result
                    # SDK 把 API 层的失败（余额不足、限流）也放在 result 里，
                    # 不识别的话循环会把报错文本当成 agent 的提问，然后一路
                    # 空转到 max_turns。实测「Credit balance is too low」被
                    # 连问了 6 遍。
                    if getattr(message, "is_error", False) or (
                        getattr(message, "subtype", "success") != "success"
                    ):
                        errored = True
                    if ledger is not None:
                        ledger.record_from_meta(
                            f"澄清第 {turn + 1} 轮",
                            {
                                "total_cost_usd": getattr(
                                    message, "total_cost_usd", None
                                )
                            },
                            kind="clarify",
                        )

            if errored:
                raise RuntimeError(f"澄清对话中断（API 层错误）: {reply}")

            if not reply or DONE_MARKER in reply:
                break

            # 预算检查放在「发答案之前」，不是循环开头。
            #
            # 实测 bug：检查放开头时，最后一轮总是「发出答案 → 下一轮开头
            # 发现预算不够 → break」，agent 从来没机会处理那个答案，
            # 落盘动作永远丢在最后一步。nationality 写进去了但
            # destination_after_arrival 没有，就是这么丢的。
            #
            # 现在的语义：付不起处理这个答案的钱，就干脆不问下去——
            # 该项留作未知，而不是问了却不让它落盘。
            if ledger is not None:
                try:
                    ledger.guard(f"澄清第 {turn + 2} 轮（处理答案）", kind="clarify")
                except BudgetExceeded:
                    break

            answer = await answer_fn(reply)
            await client.query(answer)

    return collector.updates, collector.trip_context
