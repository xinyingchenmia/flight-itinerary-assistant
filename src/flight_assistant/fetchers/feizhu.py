"""飞猪取数。

flight_search_result.htm 加载后页面自己会轮询 flight_search_result_poller.do
拿航班数据（JSONP，不是纯 JSON）。我们在旁边读它收到的响应，不构造签名、
不重放、不伪造请求——页面正常加载，只是把响应也读一份。这个接口本身不需要
mtop 的 sign 签名（和飞猪其他接口不同），登录态靠 cookie，走真实浏览器
persistent context 导航就会自动带上。

轮询逻辑：响应体是 `jsonpN({...})` 包了一层，需要剥掉再当 JSON 解析。
`data.delayForNextPoll` 是页面自己用来判断"要不要再调一次"的字段——
0 表示这批已经出全，非 0 表示还有结果在路上。我们跟着页面节奏被动收集
（多次响应可能有重叠数据，按行程+价格去重），不主动重发请求、不做自己的
重试逻辑，直到看到 delayForNextPoll == 0 或超时（超时向上抛，不重试、
不做绕过尝试，交给 fetch_live.py 标记该平台缺失）。

城市码：飞猪的 searchJourney 参数用城市码（如 SHA/CHI），不是机场码。
映射表见 _AIRPORT_TO_CITY，只收录已通过 DevTools 抓包核实的条目——
查不到的机场码直接抛错，不猜：同城多机场（浦东/虹桥、首尔仁川/金浦等）
猜错城市码会查错整座城市的报价，比报错更危险。
"""

import json
import re
from datetime import datetime
from urllib.parse import quote

from playwright.sync_api import Page, Response

from flight_assistant.fetchers.base import FetchResult, PlatformFetcher
from flight_assistant.fetchers.feizhu_parse import parse_poller_response

_SEARCH_URL = "https://sijipiao.fliggy.com/ie/flight_search_result.htm"
_POLLER_PATH = "flight_search_result_poller.do"
_JSONP_RE = re.compile(r"^\s*\w+\((.*)\)\s*;?\s*$", re.DOTALL)

# 机场码 → 飞猪城市码。已核实：SHA/CHI（2026-08-05 抓包，PVG→ORD 请求原文）。
# 其余多数城市城市码=机场码本身（单机场城市），同城多机场的才需要显式映射。
_AIRPORT_TO_CITY = {
    "PVG": "SHA",
    "SHA": "SHA",  # 上海（浦东/虹桥）
    "ORD": "CHI",
    "MDW": "CHI",  # 芝加哥
}


def _city_code(airport: str) -> str:
    code = _AIRPORT_TO_CITY.get(airport.upper())
    if code is None:
        raise RuntimeError(
            f"飞猪：机场码 {airport} 没有已核实的城市码映射，"
            f"补充 _AIRPORT_TO_CITY 前不要猜（同城多机场猜错会查错整座城市）"
        )
    return code


class FeizhuFetcher(PlatformFetcher):
    platform_name = "feizhu"

    def __init__(self) -> None:
        self._payloads: list[dict] = []
        self._done = False

    def _on_response(self, response: Response) -> None:
        if _POLLER_PATH not in response.url:
            return
        try:
            body = response.text()
        except Exception:
            return
        m = _JSONP_RE.match(body)
        if not m:
            return
        try:
            payload = json.loads(m.group(1))
        except Exception:
            return
        self._payloads.append(payload)
        if int(payload.get("data", {}).get("delayForNextPoll", 0)) == 0:
            self._done = True

    def fetch_offers(
        self, origin: str, dest: str, date: str, page: Page
    ) -> list[FetchResult]:
        search_journey = json.dumps(
            [
                {
                    "depCityCode": _city_code(origin),
                    "arrCityCode": _city_code(dest),
                    "depCityName": "",
                    "arrCityName": "",
                    "depDate": date,
                    "selectedFlights": [],
                }
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        url = (
            f"{_SEARCH_URL}?searchBy=&b2g=0&formNo=-1&agentId=-1"
            f"&needMemberPrice=true&searchJourney={quote(search_journey)}"
            f"&childPassengerNum=0&infantPassengerNum=0&tripType=0&cardId="
        )

        self._payloads.clear()
        self._done = False
        page.on("response", self._on_response)
        try:
            page.goto(url, wait_until="domcontentloaded")
            # 轮询由页面自己驱动，我们只是被动等 delayForNextPoll 归零。
            # 30s 内不收敛就当接口变了或被拦，向上抛，不重试。
            deadline_ms = 30_000
            waited = 0
            step = 500
            while not self._done and waited < deadline_ms:
                page.wait_for_timeout(step)
                waited += step
        finally:
            page.remove_listener("response", self._on_response)

        if not self._payloads:
            raise RuntimeError(
                "飞猪：没截到 flight_search_result_poller.do 响应。"
                "接口路径或 JSONP 包裹格式可能已变，检查 _POLLER_PATH。"
            )

        fetched_at = datetime.now()
        results: list[FetchResult] = []
        seen: set[str] = set()
        for payload in self._payloads:
            items, _delay = parse_poller_response(payload, fetched_at)
            for itinerary, offer in items:
                key = f"{itinerary.model_dump_json()}|{offer.price}"
                if key in seen:
                    continue
                seen.add(key)
                results.append((itinerary, offer))
        return results
