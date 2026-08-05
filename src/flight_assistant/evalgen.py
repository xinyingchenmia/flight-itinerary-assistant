"""评测样本生成：缺陷由 agent 自己想，代码只负责确定性地施加。

## 为什么不能由我枚举

第一版在脚本里写了 inject_mct_tight / inject_entry_connection_tight /
inject_split_ticket 这些函数，还硬编码了「美国和加拿大转机须入境」。后果是
**评测集只能测出我想得到的缺陷类型**——agent 在我没想到的地方漏报，评测集
永远发现不了。而"想不到的失败模式"恰恰是评测最该覆盖的东西。

现在的分工：
  - **agent 想缺陷**：给它一条真实行程，让它设计一个"看起来正常、实际会
    让旅客栽跟头"的改造，并说明期望报出什么风险。prompt 里给 2 个 few-shot
    示例说明输出格式，但不列举缺陷类型的清单。
  - **代码施加改造**：agent 只输出「改哪个字段、改成什么」，由确定性代码
    应用。这样每条样本的改造都可审计、可复现，不是 agent 自由编一条行程。

## 自己出题自己答的问题

同一个模型既出题又答题，分数会偏乐观。缓解手段：
  1. 出题和答题用不同的 system prompt（对抗角色 vs 审查角色），可以指定
     不同模型（--adversary-model）
  2. 改造由代码施加，agent 不能"出一道假题"——字段路径和新值都要落在
     白名单里，改完还要通过 pydantic 校验和时序检查
  3. 生成的样本会打印出来给人过一遍，ground truth 不合理可以手工删改

这仍然弱于人工标注，但它能覆盖我想不到的失败模式，而且可以持续扩量。
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, query

from flight_assistant.models import Itinerary, TicketGroup

# 可改的字段。限制范围是为了让改造可审计——agent 不能凭空造一条行程，
# 只能在真实行程上动这些字段。
MUTABLE = (
    re.compile(r"^segments\.\d+\.(dep_local|arr_local)$"),
    re.compile(r"^segments\.\d+\.(dep_terminal|arr_terminal)$"),
    re.compile(r"^segments\.\d+\.(dep_airport|arr_airport)$"),
    re.compile(r"^segments\.\d+\.(dep_country|arr_country)$"),
    re.compile(r"^segments\.\d+\.(carrier|flight_no)$"),
    re.compile(r"^tickets$"),  # 整体替换，用于拆票
    re.compile(r"^tickets\.\d+\.baggage_through_checked$"),
    re.compile(r"^total_duration_min$"),
)

RISK_KINDS = [
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

ADVERSARY_PROMPT = """你负责为机票风险审查系统出考题。

给你一条**真实**行程，你要设计一个改造，让它变成"订票页面上看起来完全正常、
但实际会让旅客栽跟头"的行程，并给出正确答案（期望被报出什么风险）。

## 你的目标

考出审查系统的漏洞。**不要只出那些显而易见的题**——把中转时间压到 20 分钟
谁都能看出来。真正有价值的考题是那些：
- 名义上时间充裕，但因为某个隐含流程（入境清关、换航站楼、重新托运、
  跨联盟不通票）实际不够
- 单看某一个字段都正常，几个字段组合起来才出问题
- 订票平台会显示"联程"或"直挂"，但实际规则不适用
- 你认为审查系统容易忽略的角度

**不要局限于任何预设的缺陷清单。** 你觉得什么样的改造能考出问题，就设计
什么样的。同一条行程有多种改造方向时，选你认为最容易被漏报的那个。

## 输出格式

只输出改造规格，由代码去施加——这样每条考题都可复现、可审计。

defect_name: 你给这个缺陷起的名字（自由文本，不用套用任何清单）
rationale: 为什么这个改造是现实存在的、为什么容易被漏报
mutations: 要改的字段列表。可改字段（其余一律不接受）：
    segments.{i}.dep_local / arr_local     时刻，格式 "YYYY-MM-DD HH:MM:SS"
    segments.{i}.dep_terminal / arr_terminal
    segments.{i}.dep_airport / arr_airport
    segments.{i}.dep_country / arr_country
    segments.{i}.carrier / flight_no
    tickets                                整体替换（拆票用）
    tickets.{i}.baggage_through_checked    true / false / null
    total_duration_min
expected_risks: 改造后应该被报出的风险。kind 从这个枚举里选：__KINDS__
    每条可给 acceptable_kinds（多种合理归类都算对）和
    accept_needs_user_input（标"查不到"也算合格）

## 两个示例（只示范格式，不代表缺陷类型的范围）

示例一 —— 时刻类改造：
{"defect_name": "跨联盟中转名义 2 小时实际不通票",
  "rationale": "两段分属不同联盟，行李不直挂且需重新值机，2 小时在大机场不够；"
               "订票页显示为一个行程，用户以为是联程",
  "mutations": [
    {"path": "segments.1.carrier", "value": "KE", "note": "第二段换成不同联盟的承运人"},
    {"path": "segments.1.dep_local", "value": "2026-12-20 10:30:00", "note": "衔接压到 120 分钟"}
  ],
  "expected_risks": [
    {"kind": "self_transfer_no_protection", "severity": "major",
      "acceptable_kinds": ["self_transfer_no_protection", "no_through_baggage", "mct_tight"],
      "why": "跨联盟不通票，前段延误后段不保护"}
  ]}

示例二 —— 票务结构改造：
{"defect_name": "行李只挂到中转点",
  "rationale": "同一承运人但两段分开开票，行李标只到中转站，用户需入境提取重挂",
  "mutations": [
    {"path": "tickets", "value": [
        {"segment_idx": [0], "baggage_through_checked": false, "source_platform": "ctrip"},
        {"segment_idx": [1], "baggage_through_checked": false, "source_platform": "ctrip"}
      ], "note": "拆成两张票，行李明确不直挂"}
  ],
  "expected_risks": [
    {"kind": "no_through_baggage", "severity": "major",
      "acceptable_kinds": ["no_through_baggage", "self_transfer_no_protection"],
      "why": "行李不直挂，中转须提取重挂"}
  ]}

## 硬约束

- 改造后行程必须仍然物理成立：每段 arr_local 晚于 dep_local，后一段
  dep_local 不早于前一段 arr_local。
- 只改必要的字段。改得越少，考题越干净。
- expected_risks 必须是改造**直接导致**的。原本就存在的风险不要列进去。
- rationale 要写清"为什么容易被漏报"——这是这道题的价值所在。
"""

_SCHEMA = {
    "type": "object",
    "properties": {
        "defect_name": {"type": "string"},
        "rationale": {"type": "string"},
        "mutations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "value": {},
                    "note": {"type": "string"},
                },
                "required": ["path", "value", "note"],
            },
        },
        "expected_risks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": RISK_KINDS},
                    "severity": {
                        "type": "string",
                        "enum": ["blocker", "major", "minor"],
                    },
                    "acceptable_kinds": {
                        "type": "array",
                        "items": {"type": "string", "enum": RISK_KINDS},
                    },
                    "accept_needs_user_input": {"type": "boolean"},
                    "why": {"type": "string"},
                },
                "required": ["kind", "severity", "why"],
            },
        },
    },
    "required": ["defect_name", "rationale", "mutations", "expected_risks"],
}


class MutationRejected(ValueError):
    """改造规格不合法。出题不合规就丢掉这条，不硬套。"""


@dataclass
class DefectSpec:
    defect_name: str
    rationale: str
    mutations: list[dict]
    expected_risks: list[dict]


def _check_path(path: str) -> None:
    if not any(p.match(path) for p in MUTABLE):
        raise MutationRejected(f"字段 {path!r} 不在可改白名单里")


def _set(data: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    cur: Any = data
    for part in parts[:-1]:
        cur = cur[int(part)] if isinstance(cur, list) else cur[part]
    last = parts[-1]
    if isinstance(cur, list):
        cur[int(last)] = value
    else:
        if last not in cur:
            raise MutationRejected(f"字段 {path!r} 在数据里不存在")
        cur[last] = value


def apply_spec(itinerary: Itinerary, spec: DefectSpec) -> Itinerary:
    """确定性地施加改造。不合法就抛 MutationRejected。

    agent 只出规格、不出行程——这样每条考题都能溯源到"在真实行程上改了
    哪几个字段"，而不是一条无法核对的凭空生成的行程。
    """
    if not spec.mutations:
        raise MutationRejected("没有任何 mutation")

    data = itinerary.model_dump(mode="json")
    for m in spec.mutations:
        _check_path(m["path"])
        _set(data, m["path"], m["value"])

    try:
        out = Itinerary.model_validate(data)
    except Exception as e:
        raise MutationRejected(f"改造后不是合法行程: {e}") from e

    _check_physically_possible(out)
    _adjust_duration(itinerary, out)
    out.stop_count = len(out.segments) - 1
    return out


def _check_physically_possible(it: Itinerary) -> None:
    """只检查能可靠检查的部分。

    **不能**检查"某一段的到达晚于出发"——local 时刻跨时区不可比。
    PVG 12:10 起飞、SFO 08:35 落地是真实的跨日界线航班，向东飞时当地
    到达时间本来就早于出发时间。第一版加了这条检查，会把所有跨太平洋
    航班判为非法。

    衔接时间是可以检查的：同一个机场的两个时刻在同一时区。
    """
    for i in range(len(it.segments) - 1):
        gap = (it.segments[i + 1].dep_local - it.segments[i].arr_local).total_seconds()
        if gap < 0:
            raise MutationRejected(
                f"第 {i + 1} 段出发({it.segments[i + 1].dep_local:%m-%d %H:%M})"
                f"早于第 {i} 段到达({it.segments[i].arr_local:%m-%d %H:%M})"
            )
    idx = sorted(i for t in it.tickets for i in t.segment_idx)
    if idx != list(range(len(it.segments))):
        raise MutationRejected(f"tickets 的 segment_idx 没有覆盖全部航段: {idx}")


def _adjust_duration(before: Itinerary, after: Itinerary) -> None:
    """按时刻变化量调整总时长，而不是重算绝对值。

    同样是时区问题：(末段到达 - 首段出发) 跨时区算出来是错的（跨太平洋
    航线会算出负数）。但**同一个机场同一个字段的前后差值**是可比的，
    所以用增量来调：
        新总时长 = 原总时长 + 到达推迟量 - 出发推迟量
    原总时长来自平台数据，本来就是对的。
    """
    if len(before.segments) != len(after.segments):
        return  # 航段数变了就没法用增量，保留平台给的值
    arr_delta = (
        after.segments[-1].arr_local - before.segments[-1].arr_local
    ).total_seconds() / 60
    dep_delta = (
        after.segments[0].dep_local - before.segments[0].dep_local
    ).total_seconds() / 60
    after.total_duration_min = int(before.total_duration_min + arr_delta - dep_delta)


def build_options(model: str | None = None) -> ClaudeAgentOptions:
    kwargs: dict[str, Any] = {
        # 用 replace 而不是 format：prompt 里有 segments.{i} 和 JSON 的花括号，
        # format 会把它们当占位符（实测 KeyError: 'i'）。
        "system_prompt": ADVERSARY_PROMPT.replace("__KINDS__", ", ".join(RISK_KINDS)),
        "output_format": {"type": "json_schema", "schema": _SCHEMA},
    }
    if model:
        kwargs["model"] = model
    return ClaudeAgentOptions(**kwargs)


async def design_defect(
    itinerary: Itinerary,
    model: str | None = None,
    avoid: list[str] | None = None,
) -> DefectSpec:
    """让 agent 为这条行程设计一个缺陷。

    avoid 里是已经出过的 defect_name，用来促使它换角度，避免十二条题全是
    同一类改造。
    """
    payload = {
        "itinerary": itinerary.model_dump(mode="json"),
        "connections_min": [
            int(
                (
                    itinerary.segments[i + 1].dep_local - itinerary.segments[i].arr_local
                ).total_seconds()
                // 60
            )
            for i in range(len(itinerary.segments) - 1)
        ],
        "already_used_defects": avoid or [],
    }
    prompt = (
        "为这条真实行程设计一个考题。already_used_defects 里是已经出过的"
        "缺陷，换个角度，不要重复：\n"
        + json.dumps(payload, ensure_ascii=False, indent=1)
    )

    raw: str | None = None
    err: str | None = None
    async for message in query(prompt=prompt, options=build_options(model)):
        result = getattr(message, "result", None)
        if result is not None:
            if getattr(message, "is_error", False) or (
                getattr(message, "subtype", "success") != "success"
            ):
                err = str(result)
                continue
            raw = result
    if err:
        raise RuntimeError(f"出题失败（API 层错误）: {err}")
    if raw is None:
        raise RuntimeError("出题未返回结果")

    data = json.loads(raw) if isinstance(raw, str) else raw
    return DefectSpec(
        defect_name=data["defect_name"],
        rationale=data["rationale"],
        mutations=data["mutations"],
        expected_risks=data["expected_risks"],
    )
