"""Agent 2：行程规划助手（原"澄清对话"）。

不再是"逐个追问缺口直到能下结论"的问答机器。风险审查 agent 查出来的
东西——中转很久、落地很晚、缺国籍这类信息——很多本身不是非问不可的
障碍，而是可以主动帮用户挖得更深的线索："转机 4 小时，要不要查查机场
有什么好逛的？"和"不确定国籍，答不了签证问题"是同一类事：都是"我能
帮你多查一点"，只是前者是加分项、后者是真的会卡住结论。

所以这个 agent 的产出是一份"建议菜单"，用户勾选想深挖的几条，agent 去
查、回报结果，再看查到的东西有没有牵出新的可查项，循环几轮直到没有
更多有价值的建议或者用户不想再选了。真正会影响结论的缺口（国籍、护照
这类）仍然会出现在菜单里，只是标成高优先级，不再强制打断用户。

代码只负责：把风险审查的完整发现喂进去、把用户的勾选转发进去、把
agent 查到的东西和字段更新收集起来交回确定性代码。菜单该开哪些项、
查到什么算够、什么时候没有更多可挖的——这些判断留给模型。
"""

import json
from collections.abc import Awaitable, Callable
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from flight_assistant.budget import BudgetExceeded, BudgetLedger
from flight_assistant.clarification import tools as clarify_tools
from flight_assistant.clarification.tools import UpdateCollector
from flight_assistant.models import FlightPriceComparison, TripContext, TripTip
from flight_assistant.recompute import FieldUpdate
from flight_assistant.risk_review.agent import build_connections

SYSTEM_PROMPT = """你是行程规划助手，负责在风险审查结束后，帮用户把还
没研究透的地方挖深一点——不管是"必须搞清楚才能下结论"的信息缺口，还是
"查了会让行程体验更好"的锦上添花的信息，都算你的工作范围。

## 你的产出：一份建议菜单，不是强制提问

输入里每个候选除了风险审查的发现（risks/assurances/preference_notes），
还有**原始行程数据**（flights 各段起降时间、connections 各中转点的
机场/时长/国别）。**不要只看风险审查写了什么**——风险审查只报订票页面
看不到的问题，一个中转 4 小时但没有安全隐患的候选，风险审查大概率什么
都不会写，但这依然是你该主动看一眼、想到"要不要查查这机场怎么打发
时间"的地方。原始数据和风险发现要一起看。列出你觉得值得帮用户查一查
的事，每条建议对应一个具体的发现，比如：

- 风险里写"落地 23:50，目的地是市区，没查到公共交通信息" →
  建议:"要不要我查一下这个时间点从机场打车到你说的地方大概多少钱、
  要多久？"
- 中转 4 小时 20 分钟、没有换航站楼 →
  建议:"这个中转时间不短，要不要我查查这个机场有什么好吃好逛的，
  值不值得出去转转？"
- trip_context.nationality 是空的，且行程里有中转点、某条风险因此标了
  "需要确认" →
  建议:"不确定你的护照是哪国的——这会决定中转XX需不需要办过境签，
  要不要现在告诉我？"（这类"缺了会卡住结论"的建议标 priority=high）

**签证类建议只关心中转点，不关心目的地**：用户既然已经在查这趟航班，
说明目的地本身要不要签证、要办什么类型的签证是他自己会处理的事，不是
你的工作范围——不要因为不知道国籍就建议"确认一下要不要办目的地签证/
学生签证"这种问题。你只该关心行程结构带来的、订票页面看不到的额外
负担：**某个中转点是否需要旅客正式入境**（离开管制区、取行李过海关），
这才跟"我该不该确认国籍"有关。如果所有中转都能全程空侧转机，国籍这条
就不用开成建议。

**不是每条发现都要开一条建议**——只挑真的值得用户花时间选的，宁可少而
精，不要为了凑数把每条风险都翻译成一条建议。同一类问题（比如 3 个候选
都缺国籍）只开一条，不要重复。

## 循环怎么跑

1. 你输出一批建议（items），用户会勾选零个或多个（也可能自己额外说点
   什么）。
2. 拿到用户的勾选后，去查——用得上工具就用（联网搜索、写回结构化字段），
   查完把结论写进 tips，每条一句话 + 依据。
3. 查到的东西可能牵出新问题（比如查到打车要 45 分钟，正好赶不上，这本身
   又值得再确认一下用户能不能接受）——这时可以在同一轮再开新的 items。
   如果没有更多值得挖的，就把 done 设成 true，items 留空，closing_note
   写一句收尾的话。
4. 用户如果什么都没选、或者明确说不用再查了，直接收尾，不要死缠烂打。

**处理用户勾选的那一轮，`tips` 不能是空的**——用户是冲着"这轮会有查到
的结果"才勾选的，如果这一轮只顾着抛出更多 items、一条 tips 都不给，
用户会觉得选了跟没选一样、白等一轮。哪怕东西没查全、只有阶段性结论，
也要先把已经查到的写成 tips 给用户看，新问题可以同时问，但不能只问不
答。真查不到任何东西（比如工具都没数据）也要写一条 tips 说清楚"查不到"
和原因，不能什么都不返回。

## 硬约束

- **不要研究、不要建议、不要提目的地国家的签证/入境要求**。用户已经在
  订这趟航班，去不去得了目的地国家是他自己的事，不属于你的工作范围——
  哪怕你知道"去美国读书要办 F-1"这种真实规则，也不要主动说，那不是
  这个行程结构带来的、订票页面看不到的信息。签证只关心**中转点**：
  某个中转是否需要旅客正式入境、这跟国籍有没有关系。
- **一次最多开 4 条建议**，别把菜单拉得很长让用户挑花眼。
- 每条建议的 label 要让用户一看就懂你要查什么、为什么值得查——不要写
  "查询 XX 相关信息"这种空话。
- 涉及结构化信息的（国籍、护照有效期、落地后去哪、能否打车、托运件数、
  某张票的行李是否直挂），查到答案后要调工具落盘：
    · 对所有候选都适用的（国籍等）→ update_trip_context
    · 某个候选特有的（某张票的行李是否直挂）→ update_itinerary_field
  **拿到答案先落盘，再继续**，预算可能随时耗尽，没落盘的等于白问。
- 查不到就老实说查不到，不要编。
- **禁止在 label/statement/evidence 里出现字段原名（trip_context、
  needs_user_input 这种下划线命名）或只有开发者懂的缩写**——写给普通
  用户看的大白话。
- **机场只写数据里给的三字码（如 ORD），不要自己翻译成中文城市名**——
  输入数据里没有城市名字段，"ORD 是芝加哥"这种对应关系是凭训练记忆猜的，
  猜错了就是编造事实（实测出过真实事故）。三字码是数据里明确给出、
  不会错的，直接用。
- 总轮数有限，判断没有更多高价值的东西可挖时就利落收尾，不要为了显得
  周到硬凑建议。
"""

AnswerFn = Callable[[list[dict[str, Any]]], Awaitable[dict[str, Any]]]

_MENU_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "label": {"type": "string"},
        "based_on": {"type": "string"},
        "priority": {"type": "string", "enum": ["high", "normal"]},
    },
    "required": ["id", "label", "based_on", "priority"],
}

_TIP_SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string"},
        "statement": {"type": "string"},
        "evidence": {"type": "string"},
    },
    "required": ["label", "statement", "evidence"],
}

_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "tips": {"type": "array", "items": _TIP_SCHEMA},
        "items": {"type": "array", "items": _MENU_ITEM_SCHEMA},
        "done": {"type": "boolean"},
        "closing_note": {"type": "string"},
    },
    "required": ["tips", "items", "done", "closing_note"],
}


def build_options(
    collector: UpdateCollector, web_search: bool = True, max_searches: int = 4
) -> ClaudeAgentOptions:
    allowed = list(clarify_tools.ALLOWED_TOOLS)
    if web_search:
        allowed += ["WebSearch", "WebFetch"]
    return ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        mcp_servers={"clarify_tools": clarify_tools.build_server(collector)},
        allowed_tools=allowed,
        output_format={"type": "json_schema", "schema": _PLAN_SCHEMA},
    )


def _findings_summary(
    candidates: list[FlightPriceComparison], risks_by_key: dict
) -> list[dict[str, Any]]:
    """把原始行程数据 + 风险审查的完整发现整理成给 agent 看的清单。

    只给风险审查的结论文字不够——风险审查没提到的事实（比如某段中转
    时间很长但不构成风险，压根没写进 risks/assurances），规划助手就完全
    看不到，也就想不出"中转这么久要不要推荐点好玩的"这种建议。所以这里
    把 build_connections 算出的原始衔接数据（中转机场、时长、国别）和
    每段航班的起降时间也带上，让它能直接看到"这段中转多久"，不用依赖
    风险审查有没有提过。
    """
    out = []
    for c in candidates:
        risks, assurances, notes = risks_by_key.get(c.itinerary_key, ([], [], []))
        it = c.itinerary
        segs = it.segments
        out.append(
            {
                "itinerary_key": c.itinerary_key,
                "route": [segs[0].dep_airport] + [s.arr_airport for s in segs],
                "total_duration_min": it.total_duration_min,
                "flights": [
                    {
                        "carrier": s.carrier,
                        "flight_no": s.flight_no,
                        "dep_airport": s.dep_airport,
                        "arr_airport": s.arr_airport,
                        "dep_time": s.dep_local.isoformat(),
                        "arr_time": s.arr_local.isoformat(),
                    }
                    for s in segs
                ],
                "connections": build_connections(it),
                "risks": [r.model_dump(mode="json") for r in risks],
                "assurances": [a.model_dump(mode="json") for a in assurances],
                "preference_notes": [n.model_dump(mode="json") for n in notes],
            }
        )
    return out


async def _read_reply(
    client: ClaudeSDKClient, round_no: int, ledger: BudgetLedger | None
) -> tuple[str, bool]:
    reply = ""
    errored = False
    try:
        async for message in client.receive_response():
            result = getattr(message, "result", None)
            if result is not None:
                reply = result
                if getattr(message, "is_error", False) or (
                    getattr(message, "subtype", "success") != "success"
                ):
                    errored = True
                if ledger is not None:
                    ledger.record_from_meta(
                        f"行程规划第 {round_no} 轮",
                        {"total_cost_usd": getattr(message, "total_cost_usd", None)},
                        kind="clarify",
                    )
    except Exception as e:
        # SDK 层偶尔会在响应异常时直接抛裸异常，不经过 is_error/subtype
        # 分支——兜住，转成同一套"出错但可继续"的处理。
        reply = f"{type(e).__name__}: {e}"
        errored = True
    return reply, errored


async def plan(
    candidates: list[FlightPriceComparison],
    risks_by_key: dict,
    selection_fn: AnswerFn,
    max_rounds: int = 4,
    trip_context: TripContext | None = None,
    ledger: BudgetLedger | None = None,
) -> tuple[list[FieldUpdate], TripContext, list[TripTip], str | None]:
    """跑规划助手循环，返回 (字段更新列表, 更新后的 TripContext, 查到的结论列表,
    预算警告)。

    selection_fn 接收当前这轮的建议菜单（list of {id,label,based_on,
    priority}），返回 {"selected_ids": [...], "custom": "..."}（custom
    是用户额外想说的话，可以为空）。返回空字典或两者都为空视为"不再继续"。

    max_rounds 只是防跑飞的上限，正常情况下由 agent 自己判断挖得差不多
    就收尾。**预算超了不会替用户拦下他已经做的选择**——用户勾了要查的
    东西，说明他认为这值得查，是否愿意为此超一点预算是他的判断，不是
    代码该替他做的决定。真正超预算时只是把这句话如实告诉他（通过第四个
    返回值），然后照常继续查，不中断。
    """
    if not candidates:
        return [], trip_context or TripContext(), [], None

    collector = UpdateCollector(trip_context)
    tips: list[TripTip] = []
    budget_warning: str | None = None

    findings = _findings_summary(candidates, risks_by_key)

    async with ClaudeSDKClient(options=build_options(collector)) as client:
        await client.query(
            "以下是这次要展示给用户的候选行程，及风险审查 agent 的完整发现"
            "（risks/assurances/preference_notes，不只是需要确认的项）。"
            "当前 trip_context:\n"
            + json.dumps(
                (trip_context or TripContext()).model_dump(mode="json"),
                ensure_ascii=False,
            )
            + "\n\n候选与发现:\n"
            + json.dumps(findings, ensure_ascii=False, indent=1)
        )

        for round_n in range(1, max_rounds + 1):
            reply, errored = await _read_reply(client, round_n, ledger)
            if errored:
                raise RuntimeError(f"行程规划中断（API 层错误）: {reply}")
            if not reply:
                break

            try:
                parsed = json.loads(reply)
            except json.JSONDecodeError:
                break  # 没输出合法 JSON，当作没有更多建议，正常收尾

            tips.extend(TripTip.model_validate(t) for t in parsed.get("tips", []))
            items = parsed.get("items", [])
            if parsed.get("done") or not items:
                break

            # 菜单已经算出来、钱也付了，不管预算够不够都要先展示给用户看——
            # 预算检查放在这里只会把已经付费生成的菜单直接扔掉，用户永远
            # 看不到。预算该管的是"要不要再花钱处理你的选择、发起下一轮"，
            # 不是"要不要把已经算出来的结果给你看"。
            selection = await selection_fn(items)
            selected_ids = (selection or {}).get("selected_ids") or []
            custom = (selection or {}).get("custom") or ""
            if not selected_ids and not custom:
                break

            if ledger is not None:
                try:
                    ledger.guard(f"行程规划第 {round_n + 1} 轮（处理选择）", kind="clarify")
                except BudgetExceeded as e:
                    budget_warning = (
                        f"这次选择可能会让花费超过预算上限（{e}）。"
                        "你已经选了要查的东西，所以还是照常去查了，不会因为超预算就不理你的选择。"
                    )

            await client.query(
                json.dumps(
                    {"selected_ids": selected_ids, "custom": custom},
                    ensure_ascii=False,
                )
            )

    return collector.updates, collector.trip_context, tips, budget_warning
