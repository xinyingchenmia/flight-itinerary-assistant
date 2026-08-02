from playwright.sync_api import Page

from flight_assistant.fetchers.base import PlatformFetcher
from flight_assistant.models import PlatformOffer

# URL 模板来自需求文档，未做过人工核对的部分见下方 TODO。
_LIST_URL = (
    "https://flight.qunar.com/site/oneway_list.htm"
    "?searchDepartureAirport={origin}&searchArrivalAirport={dest}&searchDepartureTime={date}"
)


class QunarFetcher(PlatformFetcher):
    platform_name = "qunar"

    def fetch_offers(
        self, origin: str, dest: str, date: str, page: Page
    ) -> list[PlatformOffer]:
        url = _LIST_URL.format(origin=origin, dest=dest, date=date)
        page.goto(url, wait_until="domcontentloaded")

        # TODO(人工核对真实页面后填入): 结果列表的稳定选择器。
        # 去哪儿的查询参数名（searchDepartureAirport 等）也待核对，
        # 文档里只给了 URL 前缀，具体 query string 需要打开真实页面确认。
        # 核对完选择器后，参考此形状组装返回值：
        # return [
        #     PlatformOffer(
        #         platform=self.platform_name,
        #         price=Decimal("..."),
        #         currency="CNY",
        #         fetched_at=datetime.now(timezone.utc),
        #         booking_url=url,
        #         fare_conditions_raw="...",
        #         confidence="confirmed",
        #     )
        # ]
        raise NotImplementedError(
            "Qunar 选择器与查询参数待人工核对真实页面后填入 (see fetchers/qunar.py TODO)"
        )
