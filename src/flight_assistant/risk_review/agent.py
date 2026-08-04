"""Agent 1：风险审查。

对每个候选行程，找出会导致行程失败或体验劣化的具体原因。这是项目里
两个 agent 之一——需要运行时判断"该查什么、查几次、够不够下结论"，
所以用 agent 循环；联程保护等能用代码算的事实提前算好传进来。
"""

import asyncio
import json
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, query

from flight_assistant.budget import BudgetLedger
from flight_assistant.models import (
    Assurance,
    FlightPriceComparison,
    PreferenceNote,
    Risk,
    TripContext,
)

# 一个候选的审查结论：
#   risks       —— 会导致行程失败或劣化的问题
#   assurances  —— 查过了、确认没问题的项（只报坏消息的话用户看不到已经替他确认过什么）
#   pref_notes  —— 针对用户主观偏好的结论（中转地好不好逛之类）
Findings = tuple[list[Risk], list[Assurance], list[PreferenceNote]]
from flight_assistant.risk_review import tools as risk_tools

SYSTEM_PROMPT = """你审查机票行程的风险，输出结构化的 Risk 列表。

## 你的定位：只报订票平台上看不到的东西

用户在携程/飞猪上已经能看到价格、时长、中转次数、行李额度、退改政策。
把这些复述一遍毫无价值。你的价值在于那些订票页面不会告诉他、但会真正
毁掉行程的事：

1. **需要入境/清关/重新托运的中转，时间够不够**
   有些国家不设国际中转区，转机旅客必须正式入境、提取托运行李、过海关、
   重新托运、再过一次安检——即使后续航段是国内段、即使是同一张票。这类
   流程通常要 90-180 分钟，远超普通国际转机的 MCT。

   输入里的 connections 已经给出每个中转点的国别、是否是进入该国的首个
   落点、衔接分钟数。**由你判断该国的转机规则**——代码不维护"哪些国家
   需要入境"的表，那种表穷举不完。判断时区分三种情况：
   - 你确知该国要求入境（如美国、加拿大是长期稳定的制度）→ 直接说明并
     引用这条规则
   - 你确知该国有国际中转区、可全程空侧中转 → 不报这条风险
   - 你不确定该国规则，或规则近年有变动（很多国家在调整）→ 标
     needs_user_input=true，写明"需确认 XX 国转机是否须入境"，不要猜

   具体机场的排队时长和官方 MCT 数值一律不要凭记忆给数字。
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

## 同时报告"查过了、没问题"的项（assurances）

只报坏消息的话，用户看不到哪些事已经替他确认过。凡是你实际查证过、
结论是"没问题"的关注点，写成一条 assurance，用用户会读的那种话：

  "半夜 00:14 落地 ORD 也能进市区：CTA 蓝线 O'Hare 站 24 小时运营"
  "SFO 入境后有 207 分钟衔接，够走完入境+取行李+重新托运+安检"
  "香港中转全程空侧，持中国护照不需要过境签"

规则：
- **只写你真的查证过或能确切引用规则的项**，不要为了凑数写"航班号正常"
  这类空话。没查过的不要写成 assurance。
- 每个候选最多 3 条，每条 statement 一句话（≤60 字），evidence 写依据
  （数字 + 来源）。
- 只写用户会担心的那几类：能不能赶上中转、半夜到了怎么走、要不要签证、
  行李会不会跟丢。
- 同一个 topic 上已经报了 Risk 就不要再写 assurance，两者不能自相矛盾。

## 用户的主观偏好（soft_preferences）

输入里的 soft_preferences 是用户原话里那些映射不到结构化字段的要求，比如
"转机的地方别太无聊"。对每条偏好，按它实际的含义处理：

- **如果 trip_context.layover_preference 已经是 explore**：用户想在中转地
  逛逛。那就去查中转机场/航站楼实际有什么（观景台、免税区规模、能不能出
  机场进市区、有无过夜设施），结合衔接时长给结论，写成 preference_note。
  例："HND T3 有观景台和江户小路餐饮区，295 分钟衔接足够逛完再回来登机"
- **如果是 shorter**：用户嫌等得烦。这时不用查航站楼有什么，直接按中转
  时长给结论即可（排序由代码按 layover 时长做，你不用管排序）。
- **如果是 unknown**：这句话有歧义，两种解读会导出相反的排序。**不要替
  用户拍一个解读**——报一条 needs_user_input=true 的 Risk（kind 取最接近的，
  没有合适的就不报 Risk），或者写一条 verdict="unknown" 的 preference_note
  说明缺什么信息。消歧是澄清对话 agent 的事。

preference_note 的 verdict 只影响排序权重，不参与过滤——主观偏好不该把
候选直接删掉。查不到的写 verdict="unknown"，不要凭印象说某个机场好玩。

## 已经缓存的事实（known_facts）

输入里的 known_facts 是之前查证过、已存档的静态事实（官方 MCT、航站楼设施、
末班车时刻等）。**直接采信，不要重复查**——重复查同一个事实是这个系统最大
的成本浪费。标了"已过期"的可以重查，其余不要动。

反过来，你**新查到的静态事实要调 remember_fact 存起来**，供后续查询复用。
判断标准：这条事实跨用户、跨行程、跨月份都成立吗？
  存：SFO 官方国际转国内 MCT 是多少、DFW Terminal D 有哪些餐饮、
      CTA 蓝线是否 24 小时运营、中国护照过境香港是否免签
  不存：这个候选的 95 分钟够不够（那是针对具体行程的判断，不是事实）

**subject 必须用机场码/国家码开头的结构化键**，如 SFO:mct_intl_to_domestic、
DFW:terminal_d_facility、ORD:ground_transit、HK:transit_visa_cn。用中文短语
命名会导致同一事实存成多条、后续也匹配不上——实测这样让缓存完全失效。
同一个事实已经在 known_facts 里出现过，就不要再存一遍。

## 硬约束

- 每条 evidence 必须引用具体数字或具体规则。"衔接 95 分钟，而 ORD 是本
  行程在美国的首个入境口岸，需入境+提取行李+重新托运+二次安检" 是合格的；
  "转机时间可能偏紧" 不合格。
- **evidence 控制在 150 字以内**，只写：具体数字 + 为什么这是问题 + 缺哪项
  信息。不要写应对建议、不要重复输入里已有的字段值、不要解释常识。
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
- connections：各中转点的衔接分钟数、国别、是否是进入该国的首个落点、
  是否换航站楼、是否跨承运人
- trip_context：用户侧信息，null 表示未知

这些是事实。你的工作是判断这些事实意味着什么，不是重新算一遍。
"""

_RISK_KINDS = [
    "mct_tight",
    "entry_connection_tight",
    "no_through_baggage",
    "self_transfer_no_protection",
    "transit_visa_required",
    "last_flight_of_day",
    "terminal_change",
    "arrival_no_ground_transit",
    "passport_validity",
]

_PREFERENCE_ITEM = {
    "type": "object",
    "properties": {
        "preference": {"type": "string"},
        "verdict": {"type": "string", "enum": ["good", "poor", "unknown"]},
        "statement": {"type": "string"},
        "evidence": {"type": "string"},
        "affected_segments": {"type": "array", "items": {"type": "integer"}},
    },
    "required": [
        "preference",
        "verdict",
        "statement",
        "evidence",
        "affected_segments",
    ],
}

_ASSURANCE_ITEM = {
    "type": "object",
    "properties": {
        "topic": {"type": "string", "enum": _RISK_KINDS},
        "statement": {"type": "string"},
        "evidence": {"type": "string"},
        "affected_segments": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["topic", "statement", "evidence", "affected_segments"],
}

_RISK_SCHEMA = {
    "type": "object",
    "properties": {
        "risks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": _RISK_KINDS},
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
        },
        "assurances": {"type": "array", "items": _ASSURANCE_ITEM},
        "preference_notes": {"type": "array", "items": _PREFERENCE_ITEM},
    },
    "required": ["risks", "assurances", "preference_notes"],
}


def _explain_api_error(stage: str, message: str) -> str:
    """给 API 层错误补上可操作的排查方向。

    裸报「403 Request not allowed」时用户无从下手，而这类错误的原因高度
    集中在几种环境问题上。
    """
    hint = ""
    low = message.lower()
    if "403" in message or "authenticate" in low or "not allowed" in low:
        hint = (
            "\n排查方向（按可能性排序）：\n"
            "  1. shell 里有失效的 ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / "
            "ANTHROPIC_BASE_URL。环境变量优先于 claude CLI 的登录态，"
            "旧 key 会直接 403。\n"
            "     检查: echo ${#ANTHROPIC_API_KEY} ${#ANTHROPIC_AUTH_TOKEN} "
            "$ANTHROPIC_BASE_URL\n"
            "     临时绕开: env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN "
            "-u ANTHROPIC_BASE_URL <原命令>\n"
            "  2. claude CLI 登录态过期，需要重新登录\n"
            "  3. 指定的模型当前账号无权访问（本次 --review-model / --model）"
        )
    elif "credit balance" in low or "quota" in low:
        hint = "\n余额不足。到 console.anthropic.com/settings/billing 充值。"
    elif "rate limit" in low or "429" in message:
        hint = "\n触发速率限制。降低 --batch-size 的并发，或稍后重试。"
    return f"{stage}失败（API 层错误）: {message}{hint}"


def build_connections(it) -> list[dict[str, Any]]:
    """各中转点的衔接情况。确定性计算，不让 agent 自己减时刻。

    换航站楼、跨国、衔接分钟数都是从数据直接算出来的事实——agent 需要的是
    在这些事实之上做判断（够不够、要不要入境），不是重新算一遍减法。
    """
    out = []
    origin_country = it.segments[0].dep_country
    for i in range(len(it.segments) - 1):
        arr, dep = it.segments[i], it.segments[i + 1]
        country = arr.arr_country
        prior = {s.arr_country for s in it.segments[:i]}
        out.append(
            {
                "at_airport": arr.arr_airport,
                "after_segment": i,
                "gap_min": int((dep.dep_local - arr.arr_local).total_seconds() // 60),
                "country": country,
                # 是否是进入该国的第一个落点。是事实，不含"该国是否要求
                # 入境"的判断——那由 agent 根据国别自己判断。
                "first_arrival_in_country": (
                    None
                    if country is None
                    else country != origin_country and country not in prior
                ),
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
    candidate: FlightPriceComparison,
    trip_context: TripContext | None = None,
    soft_preferences: list[str] | None = None,
    cache=None,
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
        # 用户原话里映射不到结构化字段的偏好。代码不解释它们的含义。
        "soft_preferences": soft_preferences or [],
        # 已缓存的事实，按本行程涉及的机场/国家预取后直接注入。
        # 注入而不是让 agent 调工具查——省掉一次工具往返，往返比多塞
        # 几百 token 贵得多。
        "known_facts": (
            [f.render() for f in cache.relevant(cache.subjects_of_itinerary(it))]
            if cache is not None
            else []
        ),
    }


_NO_SOURCES_NOTE = """

## 本次运行没有专用数据源

以下能力没有专用数据源：{missing}。

不要编造官方 MCT 数值、末班车时刻或签证条款。需要这些数据才能下结论的，
标 needs_user_input=true 并写明缺哪一项。制度性的稳定规则（如某些国家
首个入境口岸必须提取行李重新托运）可以直接引用。
"""

_WEB_SEARCH_NOTE = """

## 你可以联网查证

有 WebSearch 工具。**这是你查任意国家/机场/城市信息的手段**——不要因为
没有专用数据库就放弃，也不要只对熟悉的国家下结论。

什么时候该查（判断标准：查到了会改变结论吗）：
- 不确定某国转机是否必须入境、是否需要过境签 → 查
- 需要某机场的官方最小衔接时间（尤其换航站楼/跨航司）→ 查
- 需要某城市机场到市区的末班车时刻 → 查
- 已经确知的稳定规则、或衔接时间明显充裕到无论如何都够 → 不用查

查证要求：
- 优先航司官网、机场官网、政府移民局这类一手来源
- evidence 里写明数字来自哪里（如"FRA 官网列明国际转国际 MCT 45 分钟"）
- **查不到或来源冲突就标 needs_user_input=true**，不要拿搜索结果里
  不确定的说法当结论
- 一次审查里控制搜索次数，只查会改变结论的那几项

## 搜索预算

本次最多做 {max_searches} 次搜索。不够就优先查影响最大的那几项，
其余标 needs_user_input。
"""


def _system_prompt(web_search: bool = False, max_searches: int = 6) -> str:
    prompt = SYSTEM_PROMPT
    missing = risk_tools.missing_sources()
    if missing:
        prompt += _NO_SOURCES_NOTE.format(missing="、".join(missing))
    if web_search:
        prompt += _WEB_SEARCH_NOTE.format(max_searches=max_searches)
    return prompt


def build_options(
    schema: dict | None = None,
    model: str | None = None,
    web_search: bool = False,
    max_searches: int = 6,
    cache=None,
) -> ClaudeAgentOptions:
    """组装 agent 配置。

    专用工具只在真正有数据源时才注册——注册一堆必然返回 no_data 的工具会
    让 agent 白跑 5 轮往返（实测平均 6.5 轮，是主要成本来源）。

    web_search=True 时给它联网检索能力。这是让"自己找信息"成真的关键：
    不需要预先枚举国家或机场，它想查哪个查哪个。代价是 token 和轮次，
    所以 prompt 里给了搜索次数上限，外层还有 BudgetLedger 兜底。
    """
    allowed: list[str] = []
    kwargs: dict[str, Any] = {
        "system_prompt": _system_prompt(web_search, max_searches),
        "output_format": {
            "type": "json_schema",
            "schema": schema or _RISK_SCHEMA,
        },
    }
    server = risk_tools.build_server(cache)
    if server is not None:
        kwargs["mcp_servers"] = {"risk_tools": server}
        allowed += risk_tools.allowed_tools(cache)
    if web_search:
        allowed += ["WebSearch", "WebFetch"]
    if allowed:
        kwargs["allowed_tools"] = allowed
    if model:
        kwargs["model"] = model
    return ClaudeAgentOptions(**kwargs)


async def review_candidate(
    candidate: FlightPriceComparison,
    stats: list[dict] | None = None,
    trip_context: TripContext | None = None,
    soft_preferences: list[str] | None = None,
) -> Findings:
    """审查单个候选，返回 (风险, 已确认没问题的项, 偏好结论)。

    stats 不为 None 时，往里追加一条本次调用的成本/耗时记录——评测集里的
    「单次审计成本 vs 可避免损失」指标需要这个数据。
    """
    ctx = build_context(candidate, trip_context, soft_preferences)
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
    return (
        [Risk.model_validate(r) for r in payload["risks"]],
        [Assurance.model_validate(a) for a in payload.get("assurances", [])],
        [
            PreferenceNote.model_validate(n)
            for n in payload.get("preference_notes", [])
        ],
    )


# 批量审查的输出 schema：按候选序号回填，避免在输出里重复长长的
# itinerary_key（那是纯粹的输出 token 浪费）。
_BATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "integer"},
                    "risks": _RISK_SCHEMA["properties"]["risks"],
                    "assurances": {"type": "array", "items": _ASSURANCE_ITEM},
                    "preference_notes": {
                        "type": "array",
                        "items": _PREFERENCE_ITEM,
                    },
                },
                "required": [
                    "candidate_id",
                    "risks",
                    "assurances",
                    "preference_notes",
                ],
            },
        }
    },
    "required": ["results"],
}


async def review_batch(
    candidates: list[FlightPriceComparison],
    stats: list[dict] | None = None,
    trip_context: TripContext | None = None,
    model: str | None = None,
    ledger: BudgetLedger | None = None,
    web_search: bool = False,
    max_searches: int = 6,
    reserve: float = 0.0,
    soft_preferences: list[str] | None = None,
    cache=None,
) -> dict[str, Findings]:
    """一次调用审查一批候选。

    为什么批量：system prompt 有 1500+ 字，逐个候选调用就要付 N 遍。
    实测单候选单调用 $0.30，其中相当一部分是重复的 system prompt 和规则
    说明。一批 8 个候选共享一次 prompt，同时模型还能横向对比（"这四条都
    在 DFW 中转，只有第一条衔接不足"），输出更简洁。
    """
    if not candidates:
        return {}

    if ledger is not None:
        ledger.guard(
            f"风险审查({len(candidates)} 个候选)", reserve=reserve, kind="review"
        )

    payload = [
        {"candidate_id": i, **build_context(c, trip_context, soft_preferences, cache)}
        for i, c in enumerate(candidates)
    ]
    prompt = (
        f"审查以下 {len(candidates)} 个候选行程。对每个候选按 schema 输出一条 "
        "results 元素，candidate_id 用输入里给的序号。\n"
        "多个候选常有相同的结构性问题——相同的问题不必逐条重复长篇解释，"
        "但每个候选该报的风险都要报全。\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=1)
    )

    raw: str | None = None
    meta: dict = {"batch_size": len(candidates)}
    # 在 async for 内部抛异常会让异步生成器无法正常关闭，真正的错误会被
    # "aclose(): asynchronous generator is already running" 这个二次异常
    # 盖住。所以先记下来，等循环正常结束后再抛。
    api_error: str | None = None
    async for message in query(
        prompt=prompt,
        options=build_options(
            _BATCH_SCHEMA, model, web_search, max_searches, cache
        ),
    ):
        result = getattr(message, "result", None)
        if result is not None:
            if getattr(message, "is_error", False) or (
                getattr(message, "subtype", "success") != "success"
            ):
                api_error = str(result)
                continue
            raw = result
            for attr in ("total_cost_usd", "duration_ms", "num_turns"):
                value = getattr(message, attr, None)
                if value is not None:
                    meta[attr] = value

    if stats is not None:
        stats.append(meta)
    if ledger is not None:
        ledger.record_from_meta(f"风险审查({len(candidates)} 个)", meta, kind="review")

    if api_error is not None:
        raise RuntimeError(_explain_api_error("风险审查", api_error))
    if raw is None:
        raise RuntimeError("批量风险审查未返回结果")

    parsed = json.loads(raw) if isinstance(raw, str) else raw
    out: dict[str, Findings] = {c.itinerary_key: ([], [], []) for c in candidates}
    for row in parsed["results"]:
        idx = row["candidate_id"]
        if not 0 <= idx < len(candidates):
            continue  # 序号越界就丢弃，不猜它指的是哪个候选
        out[candidates[idx].itinerary_key] = (
            [Risk.model_validate(r) for r in row["risks"]],
            [Assurance.model_validate(a) for a in row.get("assurances", [])],
            [
                PreferenceNote.model_validate(n)
                for n in row.get("preference_notes", [])
            ],
        )
    return out


async def review_all(
    candidates: list[FlightPriceComparison],
    stats: list[dict] | None = None,
    concurrency: int = 3,
    trip_context: TripContext | None = None,
    batch_size: int = 8,
    model: str | None = None,
    ledger: BudgetLedger | None = None,
    web_search: bool = False,
    max_searches: int = 6,
    reserve: float = 0.0,
    soft_preferences: list[str] | None = None,
    cache=None,
) -> dict[str, Findings]:
    """审查多个候选：分批 + 批间并发。

    两层优化都是实测数据驱动的：
      - 串行 6 个候选 382 秒 → 并发后 137 秒
      - 逐个调用 $0.30/候选，主要浪费在重复付 system prompt
    """
    if batch_size <= 1:
        sem = asyncio.Semaphore(concurrency)

        async def one(c: FlightPriceComparison) -> tuple[str, Findings]:
            async with sem:
                return c.itinerary_key, await review_candidate(
                    c, stats, trip_context, soft_preferences
                )

        return dict(await asyncio.gather(*(one(c) for c in candidates)))

    batches = [
        candidates[i : i + batch_size]
        for i in range(0, len(candidates), batch_size)
    ]

    # 有账本时先整体检查一次：一次性发起 N 个并发批次会绕过逐次 guard
    # （并发调用在各自 record 之前就都发出去了），所以按批次数预判。
    if ledger is not None:
        ledger.guard(
            f"风险审查({len(batches)} 个批次)",
            expected_calls=len(batches),
            reserve=reserve,
            kind="review",
        )

    sem = asyncio.Semaphore(concurrency)

    async def run_batch(batch: list[FlightPriceComparison]) -> dict[str, Findings]:
        async with sem:
            return await review_batch(
                batch,
                stats,
                trip_context,
                model,
                ledger,
                web_search,
                max_searches,
                reserve,
                soft_preferences,
                cache,
            )

    merged: dict[str, Findings] = {}
    for part in await asyncio.gather(*(run_batch(b) for b in batches)):
        merged.update(part)
    return merged
