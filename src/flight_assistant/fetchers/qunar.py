from playwright.sync_api import Page

from flight_assistant.fetchers.base import FetchResult, PlatformFetcher

# URL 模板来自需求文档，未做过人工核对的部分见下方 TODO。
_LIST_URL = (
    "https://flight.qunar.com/site/oneway_list.htm"
    "?searchDepartureAirport={origin}&searchArrivalAirport={dest}&searchDepartureTime={date}"
)


class QunarFetcher(PlatformFetcher):
    platform_name = "qunar"

    def fetch_offers(
        self, origin: str, dest: str, date: str, page: Page
    ) -> list[FetchResult]:
        url = _LIST_URL.format(origin=origin, dest=dest, date=date)
        page.goto(url, wait_until="domcontentloaded")

        # TODO(待核对真实页面): 两件事都要核对——
        #   1. 查询参数名（searchDepartureAirport 等只是文档里的猜测，
        #      打开真实页面看地址栏被改写成什么）
        #   2. 用 scripts/capture_pull.js 找它的航班数据 JSON 接口，
        #      仿照 ctrip_parse.py 写 qunar_parse.py
        raise NotImplementedError(
            "Qunar 取数待核对：先确认 URL 参数名，再用 capture_pull.js 找数据接口"
        )
