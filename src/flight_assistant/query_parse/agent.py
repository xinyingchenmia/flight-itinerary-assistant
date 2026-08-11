"""Agent 0：查询解析。

用户不该被迫填一堆结构化字段——真实需求是一句话："我9/26要从上海去芝加哥，
想尽量在亚洲转机，价格低，转机次数少"。这个 agent 只做一件事：把这句话拆成
下游需要的结构化字段（取数必须的 origin/dest/date）+ 一份自由文本偏好列表
（soft_preferences，代码不解释含义，交给风险审查/澄清对话 agent 判断）。

刻意不在这里做任何"偏好分类"：能穷举的字段（sort_pref 几种、ground_transport_ok
几种）已经有位置放；穷举不了的（"转机地别太无聊"、"尽量亚洲转机"这类）原样
摘出来，不试图归类——这是项目一贯的原则：分类穷举不完，硬分只会丢信息或
分错，交给后面的 agent 用判断力去理解比代码猜测更可靠。

同城多机场（上海 PVG/SHA、北京 PEK/PKX 等）是这一步唯一需要谨慎的地方：
选错机场会查错整座城市的价格，比留 null 更糟——prompt 里明确要求拿不准就
留空，不要猜。
"""

import json
from datetime import date
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, query

SYSTEM_PROMPT = """你负责把用户一句自然语言描述的机票需求，解析成结构化字段。

只做抽取，不做判断——转机好不好、风险大不大不是你的事，那是另外两个 agent
的工作；排序偏好、旅客信息里没提到的字段也不要替用户猜。

## 必须抽出的（缺了就没法取数，抽不出就填 null 并写进 missing）

- origin / dest：三字机场码，大写。用户会说城市名而不是机场码，你自己转换。
  **同城多机场默认选国际航班常用的主力机场**（上海→PVG，北京→PEK，
  东京→NRT，纽约→JFK，首尔→ICN），除非用户明确指定了另一个机场。
  拿不准就填 null，不要瞎猜——下游会因为你猜错的机场码查错整座城市的价格，
  这比留空更糟。
- date：转成 YYYY-MM-DD。用户可能说"9/26""下周三"这类相对/不完整表达，
  今天的日期见下方，用它换算，年份缺失时取最近的未来一次出现（不会是过去）。

## 能抽就抽、拿不准就留 null/"unknown" 的字段

- max_stops：只有用户明确说了硬性上限（"最多转一次"）才填这个。
  "转机次数少"是偏好不是硬上限，不要填这里，放进 soft_preferences。
- sort_pref：只能选一个（price/duration/stops/layover）。用户提了不止一个
  排序诉求时，选最像"硬要求"的那个，其余降级进 soft_preferences，
  不要因为只能选一个就把其他诉求直接丢掉。
- nationality / passport_expiry（YYYY-MM-DD）/ destination_after_arrival /
  ground_transport_ok（taxi_ok/public_only/unknown）/ checked_bags（整数）/
  layover_preference（shorter/explore/no_preference/unknown）：
  用户主动提到才填，没提到的一律留空，不要推断。

  **例外**：如果下面给了"已知的旅客画像"（用户之前告诉过的信息），
  这次没提到的字段就用画像里的值填上，不算推断——那是真实记录过的信息，
  不是你猜的。但**这次原话如果明确说了不一样的内容，以这次为准**，
  用户可能是换了本护照、或者这次是帮别人订。

## soft_preferences

所有识别出但在上面找不到位置放的偏好，原文摘出来（可以精简措辞，但不要
改变意思、不要总结成一个模糊的词，比如"尽量在亚洲转机"不要压缩成"偏好中转"）。
这是这个系统故意不做穷举分类的地方，交给后面的 agent 处理。

## summary

给用户看的一句话确认，口语化、简短（一句话，最多两句），像朋友复述你说的
话那样，不是写公文。**只提机场码选择这一件事**（因为这个可能选错、用户
需要一眼确认），其余字段（国籍、护照、偏好……）不用在这里逐条复述——
下面另有一行会列出结构化信息，不用你在这句话里重复。

不要用"硬性上限""结构化字段""soft_preferences"这类术语，用户看不懂这些
黑话。也不要因为某个字段没提到就在这句话里专门指出"未指定 xxx"——
没提到就是没提到，不用逐条报告缺了什么，那种查户口式的话术很烦人。

**如果有字段是从"已知的旅客画像"带过来的、这次原话根本没提**，用一句
顺带的话提一下在用旧信息（比如"护照信息还是沿用你之前说的中国护照"），
让用户知道这不是凭空冒出来的、也有机会发现"我护照换了要更新"——但只
提真的从画像带过来的那些，不要为了这句话硬凑。

好例子："上海(PVG)飞芝加哥(ORD)，9/26。"
坏例子："已解析：出发地为上海(PVG)，目的地为芝加哥(ORD)，日期
2026-09-26，排序方式为价格优先，未指定国籍、护照有效期、托运行李件数，
如需过境签风险评估请补充相关信息。"

## missing 只能装 origin / dest / date

**只有这三个字段抽不出来才写进 missing**——没有它们连取数用的搜索链接
都拼不出来，必须在这一步拦下来问清楚。国籍、护照有效期、托运件数、
落地后去哪、地面交通、中转偏好这些字段，用户没提到就填 null/"unknown"，
**永远不要把它们写进 missing**——这些缺口该留给澄清对话 agent 去问，
不是在这一步就卡住用户。missing 里出现 origin/dest/date 以外的字符串
是错的，不管你觉得那个字段多重要。
"""

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "origin": {"type": ["string", "null"]},
        "dest": {"type": ["string", "null"]},
        "date": {"type": ["string", "null"]},
        "max_stops": {"type": ["integer", "null"]},
        "sort_pref": {
            "type": "string",
            "enum": ["price", "duration", "stops", "layover"],
        },
        "soft_preferences": {"type": "array", "items": {"type": "string"}},
        "nationality": {"type": ["string", "null"]},
        "passport_expiry": {"type": ["string", "null"]},
        "destination_after_arrival": {"type": ["string", "null"]},
        "ground_transport_ok": {
            "type": "string",
            "enum": ["taxi_ok", "public_only", "unknown"],
        },
        "checked_bags": {"type": ["integer", "null"]},
        "layover_preference": {
            "type": "string",
            "enum": ["shorter", "explore", "no_preference", "unknown"],
        },
        "missing": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": [
        "origin",
        "dest",
        "date",
        "sort_pref",
        "soft_preferences",
        "ground_transport_ok",
        "layover_preference",
        "missing",
        "summary",
    ],
}


def build_options(model: str | None = None) -> ClaudeAgentOptions:
    kwargs: dict[str, Any] = {
        "system_prompt": SYSTEM_PROMPT,
        "output_format": {"type": "json_schema", "schema": _SCHEMA},
    }
    if model:
        kwargs["model"] = model
    return ClaudeAgentOptions(**kwargs)


async def parse_query(
    text: str,
    today: str | None = None,
    model: str | None = None,
    known_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """解析一句自然语言需求，返回结构化字段（dict，键名对齐 web 层的表单字段）。

    known_profile：上次记住的旅客画像（国籍、护照有效期这类跨行程都
    适用的信息），没有就是 None（第一次用、或者本地没存过）。
    """
    today = today or date.today().isoformat()
    prompt = f"今天的日期：{today}\n\n"
    if known_profile:
        prompt += (
            "已知的旅客画像（你之前告诉过的信息，这次没提到的字段可以用它，"
            "但这次原话如果说了不一样的内容，以这次为准）：\n"
            + json.dumps(known_profile, ensure_ascii=False)
            + "\n\n"
        )
    prompt += f"用户的需求原话：\n{text.strip()}"

    raw: str | None = None
    async for message in query(prompt=prompt, options=build_options(model)):
        result = getattr(message, "result", None)
        if result is not None:
            if getattr(message, "is_error", False) or (
                getattr(message, "subtype", "success") != "success"
            ):
                raise RuntimeError(f"查询解析失败（API 层错误）: {result}")
            raw = result

    if raw is None:
        raise RuntimeError("查询解析未返回结果")

    return json.loads(raw) if isinstance(raw, str) else raw
