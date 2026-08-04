"""风险审查 agent 的工具。

v0 阶段本地没有接入真实参考数据源（MCT 表、签证规则库、城市末班车
时刻），这些工具一律返回"无数据"，让 agent 把对应项标成
needs_user_input / unknown，而不是拿训练数据里的旧知识当结论——签证
和时刻类信息变化快，编造一个数字比返回"未知"更危险。

**成本要点**：既然工具必然返回 no_data，就不该把它们注册进去让 agent
去试。实测注册了工具的情况下平均 6.5 轮往返，其中 5 轮多是在依次调用
四个工具、依次拿到 no_data——纯浪费。DATA_SOURCES 为空时 build_server()
返回 None，agent 侧改为在 system prompt 里直接声明"本次没有外部数据源"。

v1 接入真实数据源时：把对应函数体换成真实查询，并把工具名加进
DATA_SOURCES，其余代码不用动。

注意：联程保护判断（len(tickets) > 1）和衔接时长刻意没有做成工具——
那些是纯代码能算的确定事实，由 build_context() 算好当上下文传给 agent。
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
    "查询某个当地时间点从机场出发是否还有到指定目的地的地面交通"
    "（末班机场大巴/轨道交通/夜班车），以及打车大致费用和耗时",
    {
        "airport": str,
        "depart_airport_local_time": str,
        "destination": str,
        "public_transport_only": bool,
    },
)
async def lookup_last_ground_transit(args: dict[str, Any]) -> dict[str, Any]:
    # v1: 换成真实城市交通时刻查询
    #
    # 注意入参是"出航站楼时刻"而不是"落地时刻"——落地到走出航站楼之间还有
    # 滑行、下机、入境、取行李，国际航班常常是 40-90 分钟。这个换算由
    # 调用方（agent）根据行程判断，工具只按给定时刻查。
    return _text(
        f"lookup_last_ground_transit({args['airport']} @ "
        f"{args['depart_airport_local_time']} → {args['destination']}): {_NO_DATA}"
    )


@tool(
    "lookup_entry_procedure",
    "查询在某机场入境所需的典型时长（入境审查+提取行李+海关+重新托运+"
    "二次安检），以及该机场是否要求转机旅客入境",
    {"airport": str, "nationality": str, "is_first_port_of_entry": bool},
)
async def lookup_entry_procedure(args: dict[str, Any]) -> dict[str, Any]:
    # v1: 换成真实数据源（各机场公布的 connection time、CBP 排队统计等）
    #
    # 「美国首个入境口岸必须提取行李重新托运」这条制度性规则是稳定的，
    # 已经写进 agent 的 system prompt，不依赖这个工具。这里查的是具体
    # 机场的时长数据——那部分会变，必须查。
    return _text(
        f"lookup_entry_procedure({args['airport']}, {args['nationality']}, "
        f"first_port={args['is_first_port_of_entry']}): {_NO_DATA}"
    )


_ALL_TOOLS = {
    "lookup_mct": lookup_mct,
    "lookup_transit_visa": lookup_transit_visa,
    "lookup_last_ground_transit": lookup_last_ground_transit,
    "lookup_entry_procedure": lookup_entry_procedure,
}

# 已经接上真实数据源的工具名。v0 为空——四个函数体都还是 no_data 桩。
# v1 每接一个数据源就把名字加进来，agent 侧会自动开始调用它。
DATA_SOURCES: set[str] = set()


def build_server():
    """只注册真正有数据的工具。全都没有时返回 None，不注册空转的工具。"""
    live = [_ALL_TOOLS[name] for name in sorted(DATA_SOURCES) if name in _ALL_TOOLS]
    if not live:
        return None
    return create_sdk_mcp_server(name="risk_tools", version="0.1.0", tools=live)


def allowed_tools() -> list[str]:
    return [f"mcp__risk_tools__{name}" for name in sorted(DATA_SOURCES)]


def missing_sources() -> list[str]:
    """没有数据源的能力清单，用来在 prompt 里明确告知 agent。"""
    return sorted(set(_ALL_TOOLS) - DATA_SOURCES)
