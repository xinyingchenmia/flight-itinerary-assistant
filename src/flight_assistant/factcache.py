"""事实缓存：agent 查到的静态事实存下来，下次直接用。

## 为什么

实测加了中转体验查证后，单价从 $0.0805 涨到 $0.1562/候选，工具轮次 2.0 →
8.0，全是搜索开销。但查的这些事实——DFW Terminal D 有哪些餐厅、CTA 蓝线
几点收班、SFO 官方国际转国内 MCT——**跨查询、跨用户、跨月份都不变**。
每次重新查是纯浪费。

## 设计要点

**优先注入，而不是让 agent 调工具查缓存。** 命中的事实由代码按机场码预取，
直接放进 prompt 上下文。一次工具往返比多塞几百 token 贵得多，能省掉往返
就别用工具。写回才用工具（agent 才知道自己新学到了什么）。

**TTL 按事实类型分。** 机场设施和 MCT 半年不变；签证政策在变，30 天就该
重查；末班车时刻按季度调图。过期不删，只标 stale——过期的旧值仍然比"完全
不知道"有用，agent 可以选择重查或标注不确定。

这个缓存攒起来就是文档里 v1 说的「真实参考数据源」的渐进版本：不用一次性
去买 MCT 数据库，用着用着就有了。
"""

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

# 事实类型 → 有效期天数。按真实变化速度定，不是随便取的：
#   airport_facility 机场设施：航站楼餐饮/休息室，改造周期以年计
#   mct             官方最小衔接时间：航司/机场公布值，很少调
#   entry_procedure 入境流程耗时：制度稳定，但排队时长会随季节波动
#   ground_transit  地面交通末班时刻：轨道交通按季度调图
#   transit_visa    过境签政策：变化最快，很多国家在调整
FactTopic = Literal[
    "airport_facility",
    "mct",
    "entry_procedure",
    "ground_transit",
    "transit_visa",
]

TTL_DAYS: dict[str, int] = {
    "airport_facility": 180,
    "mct": 180,
    "entry_procedure": 90,
    "ground_transit": 90,
    "transit_visa": 30,
}

DEFAULT_TTL_DAYS = 60


# subject 必须是「机场码/国家码 + 可选细分」的形式，如 SFO、SFO:mct_intl_to_domestic。
#
# 为什么强制格式：subject 让 agent 自由命名时，同一个事实每次换个说法就是
# 一条新记录（实测存出 "SFO国际转国内" 和 "SFO Terminal 3(国内)" 两条），
# 而按机场码预取又匹配不上，缓存等于没用——实测第二次跑成本只降 5%，
# 事实条数翻倍。
SUBJECT_RE = re.compile(r"^([A-Z]{2,3})(?::([A-Za-z0-9_]+))?$")

SUBJECT_HELP = (
    "subject 必须是「机场三字码或国家两字码」，可选加冒号和英文细分，例如："
    "SFO、SFO:mct_intl_to_domestic、DFW:terminal_d_facility、ORD:ground_transit、"
    "HK:transit_visa_cn。不要用中文或自然语言短语——那样同一个事实会存成多条，"
    "后续查询也匹配不上。"
)


def normalize_subject(subject: str) -> str | None:
    """校验并规范化 subject。不合格返回 None。"""
    s = (subject or "").strip()
    m = SUBJECT_RE.match(s.upper() if ":" not in s else s.split(":", 1)[0].upper() + ":" + s.split(":", 1)[1])
    if not m:
        return None
    code, detail = m.group(1), m.group(2)
    return f"{code}:{detail}" if detail else code


@dataclass
class Fact:
    topic: str
    subject: str  # 机场码 / 国家码 / "SFO:intl_to_domestic" 这类复合键
    value: str  # 事实内容，自然语言
    source: str  # 来源，必填——没有来源的"事实"不该进缓存
    fetched_at: str  # ISO 时间

    def age_days(self, now: datetime | None = None) -> float:
        now = now or datetime.now()
        try:
            then = datetime.fromisoformat(self.fetched_at)
        except ValueError:
            return float("inf")
        return (now - then).total_seconds() / 86400

    def is_stale(self, now: datetime | None = None) -> bool:
        ttl = TTL_DAYS.get(self.topic, DEFAULT_TTL_DAYS)
        return self.age_days(now) > ttl

    def render(self, now: datetime | None = None) -> str:
        mark = "（已过期，需重新核实）" if self.is_stale(now) else ""
        return f"[{self.topic}] {self.subject}: {self.value}（来源: {self.source}）{mark}"


class FactCache:
    """JSON 文件持久化的事实缓存。

    刻意用最笨的存储：一个 JSON 文件、全量读写。事实条数是百级别，
    没必要上数据库；而且纯文本可读、可手工修正、可提交进版本库。
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else None
        self._facts: dict[tuple[str, str], Fact] = {}
        if self.path and self.path.exists():
            self.load()

    # ---------------------------------------------------------------- 持久化

    def load(self) -> None:
        raw = json.loads(self.path.read_text())
        self._facts = {}
        for row in raw:
            try:
                f = Fact(**row)
            except TypeError:
                continue  # 格式变了就跳过这条，不让旧缓存把程序搞崩
            self._facts[(f.topic, f.subject)] = f

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                [asdict(f) for f in self._facts.values()],
                ensure_ascii=False,
                indent=2,
            )
        )

    # ---------------------------------------------------------------- 读写

    def put(self, topic: str, subject: str, value: str, source: str) -> Fact:
        norm = normalize_subject(subject)
        if norm is None:
            raise ValueError(f"subject 格式不合规: {subject!r}。{SUBJECT_HELP}")
        f = Fact(
            topic=topic,
            subject=norm,
            value=value,
            source=source,
            fetched_at=datetime.now().isoformat(timespec="seconds"),
        )
        self._facts[(f.topic, f.subject)] = f
        return f

    def get(self, topic: str, subject: str) -> Fact | None:
        norm = normalize_subject(subject)
        return self._facts.get((topic, norm)) if norm else None

    def __len__(self) -> int:
        return len(self._facts)

    # ---------------------------------------------------------------- 预取

    def relevant(
        self, subjects: list[str], now: datetime | None = None
    ) -> list[Fact]:
        """按主体（机场码、国家码）预取相关事实。

        用前缀匹配而不是精确匹配：subject 可能是 "SFO" 也可能是
        "SFO:intl_to_domestic"，按机场预取时两者都该命中。
        """
        wanted = {s.upper() for s in subjects if s}
        out = []
        for (_, subject), fact in self._facts.items():
            head = subject.split(":", 1)[0].upper()
            if head in wanted:
                out.append(fact)
        return sorted(out, key=lambda f: (f.topic, f.subject))

    def subjects_of_itinerary(self, itinerary) -> list[str]:
        """一个行程涉及的所有机场码和国家码。"""
        out: set[str] = set()
        for s in itinerary.segments:
            out.update({s.dep_airport, s.arr_airport})
            for c in (s.dep_country, s.arr_country):
                if c:
                    out.add(c)
        return sorted(out)

    def stats(self, now: datetime | None = None) -> str:
        stale = sum(1 for f in self._facts.values() if f.is_stale(now))
        by_topic: dict[str, int] = {}
        for f in self._facts.values():
            by_topic[f.topic] = by_topic.get(f.topic, 0) + 1
        parts = ", ".join(f"{k} {v}" for k, v in sorted(by_topic.items()))
        return f"{len(self)} 条事实（{parts or '空'}），其中 {stale} 条已过期"
