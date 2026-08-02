"""风险审查 agent 的工具。

v0 阶段本地没有接入真实参考数据源（MCT 表、签证规则库、城市末班车
时刻），这三个工具一律返回"无数据"，让 agent 把对应项标成
needs_user_input / unknown，而不是拿训练数据里的旧知识当结论——签证
和时刻类信息变化快，编造一个数字比返回"未知"更危险。

v1 的工作就是把每个函数体换成真实数据源查询，工具签名不用改。

注意：联程保护判断（len(tickets) > 1）刻意没有做成工具——那是纯代码
能算的确定事实，由 build_context() 算好当上下文字段传给 agent。
"""

from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

_NO_DATA = "no_data: v0 未接入该数据源，请把相关风险标为 needs_user_input 或 unknown，不要猜测。"


def _text(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}]}


@tool(
    "lookup_mct",
    "查询某机场（可指定进出航站楼）的官方最小衔接时间 MCT，单位分钟",
    {
        "airport": str,
        "arr_terminal": str,
        "dep_terminal": str,
        "is_international": bool,
    },
)
async def lookup_mct(args: dict[str, Any]) -> dict[str, Any]:
    # v1: 换成真实 MCT 表查询（航司/机场官方 minimum connecting time）
    return _text(f"lookup_mct({args['airport']}): {_NO_DATA}")


@tool(
    "lookup_transit_visa",
    "查询某国籍旅客在某中转国停留指定时长是否需要过境签",
    {"nationality": str, "transit_country": str, "stay_hours": float},
)
async def lookup_transit_visa(args: dict[str, Any]) -> dict[str, Any]:
    # v1: 换成真实签证规则库查询
    return _text(
        f"lookup_transit_visa({args['nationality']} -> {args['transit_country']}): {_NO_DATA}"
    )


@tool(
    "lookup_last_ground_transit",
    "查询到达某机场在某个当地时间点后是否还有地面交通（末班机场大巴/地铁）",
    {"airport": str, "arrival_local_time": str},
)
async def lookup_last_ground_transit(args: dict[str, Any]) -> dict[str, Any]:
    # v1: 换成真实城市交通时刻查询
    return _text(
        f"lookup_last_ground_transit({args['airport']} @ {args['arrival_local_time']}): {_NO_DATA}"
    )


def build_server():
    return create_sdk_mcp_server(
        name="risk_tools",
        version="0.1.0",
        tools=[lookup_mct, lookup_transit_visa, lookup_last_ground_transit],
    )


ALLOWED_TOOLS = [
    "mcp__risk_tools__lookup_mct",
    "mcp__risk_tools__lookup_transit_visa",
    "mcp__risk_tools__lookup_last_ground_transit",
]
