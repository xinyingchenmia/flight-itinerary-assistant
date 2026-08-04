"""步骤 7：针对性重算。只重算受用户答案影响的候选，不全量重跑。"""

from dataclasses import dataclass

from flight_assistant.models import FlightPriceComparison, Risk


@dataclass
class FieldUpdate:
    """澄清对话 agent 拿到用户回答后产生的一条字段更新。

    itinerary_key 指明这条回答影响哪个候选；field_path 用点号分隔，
    例如 "tickets.0.baggage_through_checked"。
    """

    itinerary_key: str
    field_path: str
    value: object


def affected_keys(updates: list[FieldUpdate]) -> set[str]:
    return {u.itinerary_key for u in updates}


def apply_updates(
    candidates: list[FlightPriceComparison], updates: list[FieldUpdate]
) -> tuple[list[FlightPriceComparison], set[str]]:
    """把字段更新写回受影响的候选，返回 (新候选列表, 受影响的 key 集合)。

    未受影响的候选原样返回同一个对象引用——步骤 8 靠这个判断哪些候选
    需要重新走风险审查，避免全量重审。
    """
    touched = affected_keys(updates)
    by_key: dict[str, list[FieldUpdate]] = {}
    for u in updates:
        by_key.setdefault(u.itinerary_key, []).append(u)

    result: list[FlightPriceComparison] = []
    for c in candidates:
        if c.itinerary_key not in touched:
            result.append(c)  # 同一个对象，未重算
            continue
        data = c.model_dump()
        for u in by_key[c.itinerary_key]:
            _set_by_path(data, u.field_path, u.value)
        result.append(FlightPriceComparison.model_validate(data))
    return result, touched


def _set_by_path(data: dict, path: str, value: object) -> None:
    parts = path.split(".")
    cur: object = data
    try:
        for part in parts[:-1]:
            cur = cur[int(part)] if isinstance(cur, list) else cur[part]
        last = parts[-1]
        if isinstance(cur, list):
            cur[int(last)] = value
        elif last not in cur:
            raise KeyError(last)
        else:
            cur[last] = value
    except (KeyError, IndexError, ValueError) as e:
        # 澄清对话 agent 曾写出 passenger.nationality 这种模型里不存在的路径。
        # 工具层的白名单是第一道拦截，这里是第二道，保证给出可读的错误而不是
        # 一个裸 KeyError。
        raise ValueError(
            f"字段路径 {path!r} 在数据模型里不存在（卡在 {e}）"
        ) from e


def risks_needing_recheck(
    risks_by_key: dict[str, list[Risk]], touched: set[str]
) -> dict[str, list[Risk]]:
    """步骤 8：只把受影响候选的风险交回风险审查 agent 重新审查。"""
    return {k: v for k, v in risks_by_key.items() if k in touched}
