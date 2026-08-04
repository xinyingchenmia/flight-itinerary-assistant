from typing import Literal, Protocol

from playwright.sync_api import Page

from flight_assistant.fetchers.base import FetchResult, PlatformFetcher
from flight_assistant.models import Itinerary, PlatformOffer

BlockedDecision = Literal["continue", "stop_and_flag"]

# 已知会导致无法继续解析页面的阻断信号。命中任意一个就停下标记缺失，
# 不做任何绕过尝试——这是取数流程里唯一允许"判断"的地方，
# 但判断空间只有两种输出，不是完整的自主 agent（见需求文档 official.py 说明）。
_BLOCKED_MARKERS = (
    "verify you are human",
    "请完成安全验证",
    "captcha",
    "访问异常",
    "unusual traffic",
)


def check_blocked_state(page: Page) -> BlockedDecision:
    """判断当前页面是否处于验证码/未知阻断状态。

    纯确定性的关键词匹配，不调用模型——文档明确这一步"决策空间很窄"，
    用简单条件判断即可，不需要包一层 agent 调用。
    """
    text = (page.title() or "") + " " + page.content()
    text_lower = text.lower()
    if any(marker.lower() in text_lower for marker in _BLOCKED_MARKERS):
        return "stop_and_flag"
    return "continue"


class SemanticExtractor(Protocol):
    """第 2 层降级用的语义抽取接口。

    真正的实现（调用 Claude in Chrome 的 find / get_page_text 工具）
    不属于这个纯 Python 模块——那两个工具是 MCP 层暴露给运行时的能力，
    不是可以 import 的 Python 库。真实实现在 scripts/fetch_live.py 里
    通过依赖注入传进来。这里只定义接口，保证 official.py 不依赖具体
    调用方式，方便测试时传入假的 extractor。

    调用方式是单次工具调用（把 page 和字段描述喂进去、拿结构化结果
    就返回），不是一个自主 agent 循环。
    """

    def extract_offer(
        self, page: Page, origin: str, dest: str, date: str, carrier_code: str
    ) -> tuple[Itinerary, PlatformOffer] | None: ...


class OfficialSiteFetcher(PlatformFetcher):
    """官网不是单一站点，需按承运商路由到对应航司官网实现。

    调用模式和另外三个平台不同：不是"查线路拿一批候选"，而是"已知
    具体航班号后，逐个去对应承运商官网核对价格"。因此这一步依赖前
    三个平台先跑出候选集合，取出候选里出现的承运商集合，再逐个路由
    查询（由 pipeline.py 负责按承运商集合循环调用，这个类只处理单个
    承运商）。
    """

    platform_name = "official"

    # 按候选里实际出现的承运商补充，值填对应 Fetcher 类。
    CARRIER_ROUTERS: dict[str, type[PlatformFetcher] | None] = {
        "CA": None,  # AirChinaFetcher
        "MU": None,  # ChinaEasternFetcher
        "UA": None,  # UnitedFetcher
        "AA": None,  # AmericanFetcher
    }

    def __init__(self, semantic_extractor: SemanticExtractor | None = None) -> None:
        self._semantic_extractor = semantic_extractor

    def fetch_offers(
        self,
        origin: str,
        dest: str,
        date: str,
        page: Page,
        carrier_code: str | None = None,
    ) -> list[FetchResult]:
        if carrier_code is None:
            raise ValueError("OfficialSiteFetcher.fetch_offers 需要 carrier_code")

        fetcher_cls = self.CARRIER_ROUTERS.get(carrier_code)
        if fetcher_cls is not None:
            try:
                return fetcher_cls().fetch_offers(origin, dest, date, page)
            except Exception:
                pass  # 降级到第 2 层：语义抽取

        return self._semantic_fallback(origin, dest, date, page, carrier_code)

    def _semantic_fallback(
        self, origin: str, dest: str, date: str, page: Page, carrier_code: str
    ) -> list[FetchResult]:
        decision = check_blocked_state(page)
        if decision == "stop_and_flag":
            raise RuntimeError(
                f"official/{carrier_code}: 页面处于验证码/未知阻断状态，"
                "停下交人工处理，标记该平台缺失"
            )

        if self._semantic_extractor is None:
            raise NotImplementedError(
                "没有注入 SemanticExtractor（正常应由 scripts/fetch_live.py "
                "传入基于 Claude in Chrome find/get_page_text 的实现）"
            )

        offer = self._semantic_extractor.extract_offer(
            page, origin, dest, date, carrier_code
        )
        if offer is None:
            raise RuntimeError(
                f"official/{carrier_code}: 语义抽取未能识别出有效报价，标记该平台缺失"
            )
        return [offer]
