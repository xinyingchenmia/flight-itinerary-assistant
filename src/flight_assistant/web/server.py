"""本地网页前端：只接携程，聚焦在把两个 agent（风险审查 / 澄清对话）的
真实交互过程暴露给用户用，不做跨平台比价。

流程和 scripts/run_pipeline.py 大体一致，但"选哪几个候选送审"这一步不同：
run_pipeline.py 是代码按单一排序标准截断到前 N 个；这里改成把整个候选池
连同排序诉求和自由偏好一起交给 risk_review.agent.select_and_review，让
agent 自己判断该深入看哪几个——soft_preferences（比如"尽量亚洲转机"）
如果只在截断之后才生效，排序本来就没选中的候选永远没机会被评价。
其余核心逻辑（filter_and_sort / review_all / clarify / apply_updates）
直接复用，不重新实现。CLI 版本（run_pipeline.py）暂时没有跟进这个改动。

携程取数走手动抓包（浏览器反爬会拦自动化 Playwright，见
scripts/import_captured.py 的说明），本模块只负责“扫 ~/Downloads、解析、
喂给后面的确定性代码 + agent”。

会话状态存进程内内存字典，单用户本地使用，不需要数据库/多进程协调——但
会额外落盘一份到 ~/.flight-assistant-web-sessions/，进程重启（比如我
改完代码要重启服务）时从磁盘把会话读回来，浏览器那边还拿着的 session_id
不会突然失效。真正没法跨重启恢复的只有"正卡在等你在菜单里勾选"这一种
状态——那次对话本身（跟模型的连接）随进程一起没了，重启后会直接把它标成
"已中断"，不会让轮询永远空等，但已经算出来的候选/风险审查结果不受影响。
"""

import asyncio
import json
import sys
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from flight_assistant.budget import BudgetExceeded, BudgetLedger, CallRecord, cost_of
from flight_assistant.clarification.agent import plan
from flight_assistant.factcache import FactCache
from flight_assistant.fetchers.ctrip_parse import parse_batch_search
from flight_assistant.filtering import TripRequest, filter_and_sort
from flight_assistant.matching import group_and_compare
from flight_assistant.models import (
    Assurance,
    FlightPriceComparison,
    Itinerary,
    PlatformOffer,
    PreferenceNote,
    Risk,
    TripContext,
    TripTip,
)
from flight_assistant.query_parse.agent import parse_query
from flight_assistant.recompute import FieldUpdate, apply_updates
from flight_assistant.risk_review.agent import review_all, select_and_review

REPO_ROOT = Path(__file__).resolve().parents[3]
STATIC_DIR = Path(__file__).resolve().parent / "static"
CAPTURE_SCRIPT = REPO_ROOT / "scripts" / "capture_pull.js"
FACT_CACHE_PATH = Path.home() / ".flight-assistant-facts.json"

REVIEW_LIMIT = 3  # 深入审查几个候选——这个数字小省时间，跟候选池大小无关
DEFAULT_BUDGET = 0.5
# 和 run_pipeline.py 的 --max-searches 默认一致。降这个值省钱但会漏报——
# 见 run_pipeline.py 里 --max-searches 的注释，PEK/PKX 同城不同机场那个
# 真实 blocker 就是因为搜索预算被砍到 2 才漏掉的。
DEFAULT_MAX_SEARCHES = 4
# 网页版是交互式的，等待感比成本更敏感，所以故意比 REVIEW_LIMIT 小——
# 4 个候选拆成 2 批并发跑，而不是塞进一次串行调用里。代价是重复付一点
# system prompt 的钱，换来的是明显更快：项目实测过 6 个候选串行 382 秒，
# 分批并发后 137 秒，见 risk_review/agent.py 的 review_all() 文档字符串。
REVIEW_BATCH_SIZE = 2

_LIST_URL = "https://flights.ctrip.com/online/list/oneway-{origin}-{dest}?depdate={date}"

# 长期旅客画像：只存跨行程都稳定成立的信息，不存这次特有的（落地后去哪、
# 托运几件——这些每趟行程都可能不一样，存了反而会被下次误当成"上次说过"
# 带进新行程）。国籍/护照几乎不变，两个交通偏好也是"人"的属性不是"这趟
# 行程"的属性，适合长期记住。
PROFILE_PATH = Path.home() / ".flight-assistant-profile.json"
PROFILE_FIELDS = ("nationality", "passport_expiry", "ground_transport_ok", "layover_preference")


def _load_profile() -> dict:
    if not PROFILE_PATH.exists():
        return {}
    try:
        data = json.loads(PROFILE_PATH.read_text())
        return {k: v for k, v in data.items() if k in PROFILE_FIELDS and v not in (None, "unknown")}
    except Exception as e:  # noqa: BLE001 — 画像读不出来不该拦住正常查询
        print(f"旅客画像读取失败（忽略，当作没有记住过）: {e}", file=sys.stderr)
        return {}


def _remember_profile(ctx: TripContext) -> None:
    """把这次确认过的旅客信息存进长期画像，供下次自动带入。"""
    existing = {}
    if PROFILE_PATH.exists():
        try:
            existing = json.loads(PROFILE_PATH.read_text())
        except Exception:
            existing = {}
    data = ctx.model_dump(mode="json")
    for field_name in PROFILE_FIELDS:
        value = data.get(field_name)
        if value not in (None, "unknown"):
            existing[field_name] = value
    try:
        PROFILE_PATH.write_text(json.dumps(existing, ensure_ascii=False, indent=2))
    except Exception as e:  # noqa: BLE001 — 画像存不进去不该拦住正常查询
        print(f"旅客画像保存失败（忽略）: {e}", file=sys.stderr)


@dataclass
class Session:
    id: str
    req: TripRequest
    trip_context: TripContext
    budget: float
    created_at: datetime = field(default_factory=datetime.now)

    candidates: list[FlightPriceComparison] = field(default_factory=list)
    risks_by_key: dict = field(default_factory=dict)
    ledger: BudgetLedger = field(default_factory=lambda: BudgetLedger(cap=DEFAULT_BUDGET))
    stats: list[dict] = field(default_factory=list)

    # 行程规划助手的“单槽信箱”：一次只挂一份待选菜单，轮询接口读它。
    clarify_menu: list = field(default_factory=list)
    clarify_menu_event: asyncio.Event = field(default_factory=asyncio.Event)
    clarify_selection: dict | None = None
    clarify_selection_event: asyncio.Event = field(default_factory=asyncio.Event)
    clarify_done: bool = False
    clarify_started: bool = False
    clarify_updates: list = field(default_factory=list)
    clarify_tips: list = field(default_factory=list)
    clarify_error: str | None = None
    clarify_warning: str | None = None


SESSIONS: dict[str, Session] = {}
SESSION_DIR = Path.home() / ".flight-assistant-web-sessions"


def _serialize_session(s: Session) -> dict:
    return {
        "id": s.id,
        "req": s.req.model_dump(mode="json"),
        "trip_context": s.trip_context.model_dump(mode="json"),
        "budget": s.budget,
        "created_at": s.created_at.isoformat(),
        "candidates": [c.model_dump(mode="json") for c in s.candidates],
        "risks_by_key": {
            k: {
                "risks": [r.model_dump(mode="json") for r in risks],
                "assurances": [a.model_dump(mode="json") for a in assurances],
                "preference_notes": [n.model_dump(mode="json") for n in notes],
            }
            for k, (risks, assurances, notes) in s.risks_by_key.items()
        },
        "stats": s.stats,
        "ledger": {
            "cap": s.ledger.cap,
            "reserve_ratio": s.ledger.reserve_ratio,
            "calls": [
                {
                    "label": c.label,
                    "cost": c.cost,
                    "candidates": c.candidates,
                    "kind": c.kind,
                }
                for c in s.ledger.calls
            ],
        },
        "clarify_menu": s.clarify_menu,
        "clarify_selection": s.clarify_selection,
        "clarify_done": s.clarify_done,
        "clarify_started": s.clarify_started,
        "clarify_updates": [
            {
                "itinerary_key": u.itinerary_key,
                "field_path": u.field_path,
                "value": u.value,
            }
            for u in s.clarify_updates
        ],
        "clarify_tips": [t.model_dump(mode="json") for t in s.clarify_tips],
        "clarify_error": s.clarify_error,
        "clarify_warning": s.clarify_warning,
    }


def _deserialize_session(data: dict) -> Session:
    ledger = BudgetLedger(
        cap=data["ledger"]["cap"], reserve_ratio=data["ledger"]["reserve_ratio"]
    )
    ledger.calls = [CallRecord(**c) for c in data["ledger"]["calls"]]

    s = Session(
        id=data["id"],
        req=TripRequest.model_validate(data["req"]),
        trip_context=TripContext.model_validate(data["trip_context"]),
        budget=data["budget"],
        created_at=datetime.fromisoformat(data["created_at"]),
        candidates=[FlightPriceComparison.model_validate(c) for c in data["candidates"]],
        risks_by_key={
            k: (
                [Risk.model_validate(r) for r in v["risks"]],
                [Assurance.model_validate(a) for a in v["assurances"]],
                [PreferenceNote.model_validate(n) for n in v["preference_notes"]],
            )
            for k, v in data["risks_by_key"].items()
        },
        ledger=ledger,
        stats=data["stats"],
        clarify_menu=data["clarify_menu"],
        clarify_selection=data["clarify_selection"],
        clarify_done=data["clarify_done"],
        clarify_started=data["clarify_started"],
        clarify_updates=[FieldUpdate(**u) for u in data["clarify_updates"]],
        clarify_tips=[TripTip.model_validate(t) for t in data["clarify_tips"]],
        clarify_error=data["clarify_error"],
        clarify_warning=data["clarify_warning"],
    )
    # 重启之后，之前跟模型的那次对话（如果正卡在等用户勾选菜单）已经没了，
    # 不会再有任何东西把 clarify_done 置真——不标记的话轮询接口会永远
    # 空等一个不会再来的答案。已经算出来的候选/风险审查结果不受影响，
    # 只是这一轮规划对话要重新发起。
    if s.clarify_started and not s.clarify_done:
        s.clarify_done = True
        s.clarify_error = (
            "服务重启导致行程规划对话中断，之前的候选和风险审查结果都还在，"
            "可以直接看结果，规划这步需要重新发起。"
        )
    return s


def _save_session(session: Session) -> None:
    try:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        path = SESSION_DIR / f"{session.id}.json"
        path.write_text(
            json.dumps(_serialize_session(session), ensure_ascii=False, indent=2)
        )
    except Exception as e:  # noqa: BLE001 — 落盘失败不该打断正常请求
        print(f"会话落盘失败（不影响本次请求）: {e}", file=sys.stderr)


def _load_sessions_from_disk() -> None:
    if not SESSION_DIR.exists():
        return
    for path in SESSION_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text())
            SESSIONS[data["id"]] = _deserialize_session(data)
        except Exception as e:  # noqa: BLE001 — 单个会话坏了不该拖垮整个启动
            print(f"跳过损坏的会话文件 {path.name}: {e}", file=sys.stderr)


_load_sessions_from_disk()


def _session(session_id: str) -> Session:
    s = SESSIONS.get(session_id)
    if s is None:
        raise HTTPException(404, "会话不存在或已过期，请重新发起查询")
    return s


_SEVERITY_ORDER = {"blocker": 0, "major": 1, "minor": 2}


def _tag(kind: str, obj) -> dict:
    """给序列化结果打一个类型标记，前端靠它决定用哪种卡片样式渲染——
    issues/confirmed_ok/unmet_preferences 混装了 Risk/Assurance/
    PreferenceNote 三种不同形状的对象，不能靠字段名猜是哪种。
    """
    d = obj.model_dump(mode="json")
    d["_kind"] = kind
    return d


def _serialize_candidate(rank: int, c: FlightPriceComparison, risks_by_key: dict) -> dict:
    risks, assurances, notes = risks_by_key.get(c.itinerary_key, ([], [], []))
    it = c.itinerary
    segs = it.segments
    connections = []
    for i in range(len(segs) - 1):
        gap = int((segs[i + 1].dep_local - segs[i].arr_local).total_seconds() // 60)
        connections.append({"airport": segs[i].arr_airport, "gap_min": gap})

    good_prefs = [n for n in notes if n.verdict == "good"]
    unmet_prefs = [n for n in notes if n.verdict != "good"]

    return {
        "rank": rank,
        "itinerary_key": c.itinerary_key,
        "route": [segs[0].dep_airport] + [s.arr_airport for s in segs],
        "price": str(min(o.price for o in c.offers)),
        "platforms": sorted({o.platform for o in c.offers}),
        "duration_min": it.total_duration_min,
        "stop_count": it.stop_count,
        "connections": connections,
        "ticket_count": len(it.tickets),
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
        # 三分类，分类逻辑放在这里做一次——前端不用猜哪个 verdict/severity
        # 该归到哪一栏。
        "issues": [
            _tag("risk", r)
            for r in sorted(risks, key=lambda r: _SEVERITY_ORDER[r.severity])
        ],
        "confirmed_ok": [_tag("assurance", a) for a in assurances]
        + [_tag("preference", n) for n in good_prefs],
        "unmet_preferences": [_tag("preference", n) for n in unmet_prefs],
    }


app = FastAPI(title="机票行程助手")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class SearchBody(BaseModel):
    origin: str
    dest: str
    date: str
    sort: str = "price"
    max_stops: int | None = None
    soft_prefs: list[str] = []
    budget: float = DEFAULT_BUDGET
    nationality: str | None = None
    passport_expiry: str | None = None
    destination_after_arrival: str | None = None
    ground_transport_ok: str = "unknown"
    checked_bags: int | None = None
    layover_preference: str = "unknown"


class ParseBody(BaseModel):
    text: str
    budget: float = DEFAULT_BUDGET


@app.post("/api/parse")
async def parse(body: ParseBody) -> dict:
    """自然语言 → 结构化字段。前端拿到结果先给用户确认，再喂给 /api/search——
    机场码这种解析错了代价很大的字段，不该悄悄跳过确认这一步。
    """
    if not body.text.strip():
        raise HTTPException(422, "说点什么吧")
    try:
        parsed = await parse_query(body.text, known_profile=_load_profile())
    except RuntimeError as e:
        raise HTTPException(502, f"查询解析失败：{e}")
    parsed["budget"] = body.budget
    return parsed


@app.post("/api/search")
async def start_search(body: SearchBody) -> dict:
    req = TripRequest(
        origin=body.origin.upper(),
        dest=body.dest.upper(),
        date=body.date,
        max_stops=body.max_stops,
        sort_pref=body.sort,
        soft_preferences=[p for p in body.soft_prefs if p.strip()],
    )
    ctx = TripContext(
        nationality=body.nationality or None,
        passport_expiry=body.passport_expiry or None,
        destination_after_arrival=body.destination_after_arrival or None,
        ground_transport_ok=body.ground_transport_ok or "unknown",
        checked_bags=body.checked_bags,
        layover_preference=body.layover_preference or "unknown",
    )
    _remember_profile(ctx)  # 这次确认过的国籍/护照/交通偏好，记下来供下次自动带入
    session_id = uuid.uuid4().hex[:12]
    session = Session(
        id=session_id,
        req=req,
        trip_context=ctx,
        budget=body.budget,
        ledger=BudgetLedger(cap=body.budget),
    )
    SESSIONS[session_id] = session
    _save_session(session)
    script = CAPTURE_SCRIPT.read_text()
    return {
        "session_id": session_id,
        "ctrip_url": _LIST_URL.format(origin=req.origin, dest=req.dest, date=req.date),
        "capture_script": script,
        # 书签方式：拖到收藏栏，以后在携程页面点一下即可，不用开 DevTools。
        "capture_bookmarklet": "javascript:" + urllib.parse.quote(script),
    }


def _fresh_ctrip_files(since: datetime) -> list[Path]:
    """~/Downloads 里本次会话创建之后新增的抓包文件。

    只认创建时间之后的文件——避免误把上一次查询留下的旧抓包当成这次的。

    同时认 ctrip-*.json 和 unknown-*.json：抓包脚本按 URL 里有没有
    "ctrip" 字符串猜平台前缀，实测携程的 batchSearch 有时会经过一个
    域名不带 "ctrip" 字样的中间层（比如走反爬用的边缘节点），猜错时
    文件会存成 unknown- 前缀——内容仍然是真实的携程数据，只是文件名
    没猜对，不该因为这个就找不到。这个工具现在只接携程，unknown 前缀
    出现在这里基本上就是猜错的携程数据。
    """
    downloads = Path.home() / "Downloads"
    paths = sorted(downloads.glob("ctrip-*.json")) + sorted(
        downloads.glob("unknown-*.json")
    )
    return [p for p in paths if datetime.fromtimestamp(p.stat().st_mtime) >= since]


@app.get("/api/session/{session_id}/capture-status")
def capture_status(session_id: str) -> dict:
    """前端轮询用：抓包文件出现了没有。出现了就自动触发导入，不用你手动点。"""
    session = _session(session_id)
    return {"ready": bool(_fresh_ctrip_files(session.created_at))}


def _load_captured_ctrip(
    since: datetime, expected_date: str
) -> tuple[list[tuple[Itinerary, PlatformOffer]], set[str]]:
    """解析抓包文件，返回 (匹配目标日期的报价, 抓到但日期对不上被丢弃的日期集合)。

    页面上的抓包指引会让你"重新触发一次搜索"来激活拦截器——如果那次搜索
    改了日期（比如手滑点到别的日期、或者顺手多看了一眼别的航班），
    抓到的就是那天的数据，不是你一开始说的日期。这里按航段实际出发日期
    过滤，日期对不上的直接丢弃，不会悄悄当成你要的数据用——错的日期
    比没有数据更危险，因为它看起来完全正常，你不会意识到查错了。
    """
    fresh = _fresh_ctrip_files(since)
    if not fresh:
        raise HTTPException(
            409,
            f"~/Downloads 里没找到会话开始后新增的 ctrip-*.json。"
            f"先按页面提示在浏览器里手动搜索并保存一次。",
        )

    results: list[tuple[Itinerary, PlatformOffer]] = []
    seen: set[str] = set()
    wrong_dates: set[str] = set()
    for path in fresh:
        payload = json.loads(path.read_text())
        fetched_at = datetime.fromtimestamp(path.stat().st_mtime)
        for itinerary, offer in parse_batch_search(payload, fetched_at):
            actual_date = itinerary.segments[0].dep_local.date().isoformat()
            if actual_date != expected_date:
                wrong_dates.add(actual_date)
                continue
            key = f"{itinerary.model_dump_json()}|{offer.price}"
            if key in seen:
                continue
            seen.add(key)
            results.append((itinerary, offer))

    if not results:
        raise HTTPException(
            422,
            f"抓到了数据，但里面没有 {expected_date} 这天的航班"
            + (f"（抓到的是 {'、'.join(sorted(wrong_dates))}）" if wrong_dates else "")
            + f"。搜索时选的日期和一开始说的日期对不上了，回携程页面按"
            f"{expected_date} 重新搜一次。",
        )
    return results, wrong_dates


@app.post("/api/session/{session_id}/import")
async def import_and_review(session_id: str) -> dict:
    session = _session(session_id)

    fetched, wrong_dates = _load_captured_ctrip(session.created_at, session.req.date)
    pool = filter_and_sort(group_and_compare(fetched), session.req)
    if not pool:
        raise HTTPException(422, "抓到了数据，但过滤后没有符合条件的候选（检查中转次数限制）")

    cache = FactCache(FACT_CACHE_PATH)
    t0 = time.monotonic()
    try:
        risks_by_key, selected_keys, selection_note = await select_and_review(
            pool,
            REVIEW_LIMIT,
            session.stats,
            trip_context=session.trip_context,
            sort_pref=session.req.sort_pref,
            soft_preferences=session.req.soft_preferences,
            cache=cache,
            ledger=session.ledger,
            reserve=0.15,  # 给澄清对话留额度，理由见 budget.py 的 guard() 注释
            web_search=True,  # 不开的话查不到的事实一律标"无数据源"，不会去查
            max_searches=DEFAULT_MAX_SEARCHES,
        )
    except BudgetExceeded as e:
        raise HTTPException(402, str(e))
    except RuntimeError as e:
        raise HTTPException(502, f"风险审查失败：{e}")
    finally:
        cache.save()
    elapsed = time.monotonic() - t0

    by_key = {c.itinerary_key: c for c in pool}
    subject = [by_key[k] for k in selected_keys if k in by_key]
    if not subject:
        raise HTTPException(502, "选择+审查没有选出任何候选，agent 返回结果异常")

    session.candidates = subject
    session.risks_by_key = risks_by_key
    _save_session(session)

    flagged = sum(
        1 for rs, *_ in risks_by_key.values() for r in rs if r.needs_user_input
    )
    return {
        "candidates": [
            _serialize_candidate(i, c, risks_by_key) for i, c in enumerate(subject, 1)
        ],
        "selection_note": selection_note,
        "pool_size": len(pool),
        "pending_clarifications": flagged,
        "cost_usd": cost_of(session.stats),
        "elapsed_s": elapsed,
        "budget_spent": session.ledger.spent,
        "budget_cap": session.ledger.cap,
        "ignored_wrong_date": sorted(wrong_dates),
    }


async def _run_plan(session: Session) -> None:
    async def selection_fn(items: list) -> dict:
        session.clarify_menu = items
        session.clarify_menu_event.set()
        session.clarify_selection_event.clear()
        await session.clarify_selection_event.wait()
        session.clarify_menu_event.clear()
        return session.clarify_selection or {}

    try:
        updates, trip_ctx, tips, budget_warning = await plan(
            session.candidates,
            session.risks_by_key,
            selection_fn,
            trip_context=session.trip_context,
            ledger=session.ledger,
        )
        session.clarify_updates = updates
        session.clarify_tips = tips
        session.trip_context = trip_ctx
        session.clarify_warning = budget_warning
    except Exception as e:  # noqa: BLE001 — 转成可轮询到的错误状态，不吞
        session.clarify_error = f"{type(e).__name__}: {e}"
    finally:
        session.clarify_done = True
        session.clarify_menu_event.set()  # 唤醒还在等的轮询请求
        _save_session(session)


@app.post("/api/session/{session_id}/clarify/start")
async def start_clarify(session_id: str) -> dict:
    session = _session(session_id)
    if not session.clarify_started:
        session.clarify_started = True
        _save_session(session)  # 立刻落盘"已开始"——万一进程在等待期间重启，
        # 重启后 _deserialize_session 能看到 started=True/done=False，
        # 正确判定成"已中断"而不是误判成"还没开始过"
        asyncio.create_task(_run_plan(session))
    return {"needed": True}


def _poll_payload(session: Session, done: bool, menu: list) -> dict:
    return {
        "done": done,
        "menu": menu,
        "error": session.clarify_error,
        "warning": session.clarify_warning,
        "updates_count": len(session.clarify_updates),
        "tips": [t.model_dump(mode="json") for t in session.clarify_tips],
    }


@app.get("/api/session/{session_id}/clarify/poll")
async def poll_clarify(session_id: str) -> dict:
    session = _session(session_id)
    if session.clarify_done:
        return _poll_payload(session, True, [])
    try:
        await asyncio.wait_for(session.clarify_menu_event.wait(), timeout=25)
    except asyncio.TimeoutError:
        pass
    if session.clarify_done:
        return _poll_payload(session, True, [])
    return _poll_payload(session, False, session.clarify_menu)


class SelectionBody(BaseModel):
    selected_ids: list[str] = []
    custom: str = ""


@app.post("/api/session/{session_id}/clarify/answer")
async def answer_clarify(session_id: str, body: SelectionBody) -> dict:
    session = _session(session_id)
    if session.clarify_done:
        raise HTTPException(409, "行程规划已结束")
    session.clarify_selection = {"selected_ids": body.selected_ids, "custom": body.custom}
    session.clarify_selection_event.set()
    return {"ok": True}


@app.post("/api/session/{session_id}/finalize")
async def finalize(session_id: str) -> dict:
    session = _session(session_id)

    after = session.candidates
    risks_before = session.risks_by_key
    if session.clarify_updates:
        after, touched = apply_updates(session.candidates, session.clarify_updates)
        after = filter_and_sort(after, session.req)

    risks_after = risks_before
    if session.trip_context != TripContext():
        cache = FactCache(FACT_CACHE_PATH)
        try:
            risks_after = await review_all(
                after,
                session.stats,
                trip_context=session.trip_context,
                soft_preferences=session.req.soft_preferences,
                cache=cache,
                ledger=session.ledger,
                web_search=True,
                max_searches=DEFAULT_MAX_SEARCHES,
                batch_size=REVIEW_BATCH_SIZE,
            )
        except BudgetExceeded as e:
            risks_after = risks_before  # 预算不够就沿用澄清前的结论
            session.clarify_error = f"重审预算不足，结果沿用澄清前的审查: {e}"
        finally:
            cache.save()

    session.candidates = after
    session.risks_by_key = risks_after
    _save_session(session)
    _remember_profile(session.trip_context)  # 规划助手过程中新问出来的信息也记下来

    return {
        "candidates": [
            _serialize_candidate(i, c, risks_after) for i, c in enumerate(after, 1)
        ],
        "trip_context": session.trip_context.model_dump(mode="json"),
        "updates_applied": len(session.clarify_updates),
        "tips": [t.model_dump(mode="json") for t in session.clarify_tips],
        "budget_spent": session.ledger.spent,
        "budget_cap": session.ledger.cap,
        "warning": session.clarify_error or session.clarify_warning,
    }
