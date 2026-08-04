from playwright.sync_api import Page

from flight_assistant.fetchers.base import FetchResult, PlatformFetcher

# URL 模板来自需求文档，未做过人工核对的部分见下方 TODO。
_LIST_URL = (
    "https://sfs.fliggy.com/flight/list.htm"
    "?depCity={origin}&arrCity={dest}&depDate={date}"
)


class FeizhuFetcher(PlatformFetcher):
    platform_name = "feizhu"

    def fetch_offers(
        self, origin: str, dest: str, date: str, page: Page
    ) -> list[FetchResult]:
        url = _LIST_URL.format(origin=origin, dest=dest, date=date)
        page.goto(url, wait_until="domcontentloaded")

        # TODO(待核对真实页面): 照携程的做法来——先用 scripts/capture_pull.js
        # 装拦截器，找出飞猪自己请求航班数据的那个 JSON 接口，读它的响应，
        # 而不是解析 DOM（类名带构建哈希、折叠卡片缺分段数据）。
        # 找到接口后仿照 ctrip_parse.py 写一个 feizhu_parse.py。
        raise NotImplementedError(
            "Feizhu 取数待核对：用 scripts/capture_pull.js 找它的航班数据接口"
        )
