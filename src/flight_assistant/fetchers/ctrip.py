"""携程取数。

走 JSON 而不是 DOM：页面自己会向 batchSearch / search/pull 请求航班数据，
我们在旁边读它收到的响应。不构造、不重放、不伪造请求——页面正常加载，
只是把响应也读一份。

为什么不解析 DOM：
  - 类名是 CSS Modules 哈希（FlightDetailFragment_..__Sm9Yr），携程发版即失效
  - 折叠卡片里没有分段数据，要逐张点开「航班详情」才有，那会对页面产生
    N 次额外交互，更容易触发风控
  - JSON 里字段齐全（含 operateAirlineCode、transferDuration、各段航站楼）

字段映射见 ctrip_parse.py 的 docstring。
"""

from datetime import datetime

from playwright.sync_api import Page, Response

from flight_assistant.fetchers.base import FetchResult, PlatformFetcher
from flight_assistant.fetchers.ctrip_parse import parse_batch_search

_LIST_URL = "https://flights.ctrip.com/online/list/oneway-{origin}-{dest}?depdate={date}"

# 航班结果接口。pull 的 URL 尾部带 session token，无法自行拼接——
# 只在响应回来时按路径匹配。两个接口的响应结构相同。
_RESULT_APIS = ("/search/api/search/batchSearch", "/search/api/search/pull/")

# 结果卡片。data-testid 无构建哈希，比 .domestic 这类类名稳定。
_CARD = '[data-testid^="flight-item-"]'


class CtripFetcher(PlatformFetcher):
    platform_name = "ctrip"

    def __init__(self) -> None:
        self._payloads: list[dict] = []

    def _on_response(self, response: Response) -> None:
        if not any(api in response.url for api in _RESULT_APIS):
            return
        try:
            payload = response.json()
        except Exception:
            return  # 不是 JSON 就跳过
        if payload.get("data", {}).get("flightItineraryList"):
            self._payloads.append(payload)

    def fetch_offers(
        self, origin: str, dest: str, date: str, page: Page
    ) -> list[FetchResult]:
        url = _LIST_URL.format(origin=origin, dest=dest, date=date)

        self._payloads.clear()
        page.on("response", self._on_response)
        try:
            page.goto(url, wait_until="domcontentloaded")
            # 等结果渲染出来，同时给 XHR 留出返回时间。风控拦截时这里会
            # 超时，异常向上抛，由 fetch_live.py 标记该平台缺失——不重试、
            # 不做任何绕过尝试。
            page.wait_for_selector(_CARD, timeout=30_000)
        finally:
            page.remove_listener("response", self._on_response)

        if not self._payloads:
            raise RuntimeError(
                "携程：页面渲染出了卡片但没截到 batchSearch/pull 响应。"
                "接口路径可能已变，检查 _RESULT_APIS。"
            )

        fetched_at = datetime.now()
        results: list[FetchResult] = []
        seen: set[str] = set()
        for payload in self._payloads:
            for itinerary, offer in parse_batch_search(payload, fetched_at):
                # batchSearch 和 pull 会返回重叠的行程，按 itineraryId 去重
                key = f"{itinerary.model_dump_json()}|{offer.price}"
                if key in seen:
                    continue
                seen.add(key)
                results.append((itinerary, offer))
        return results
