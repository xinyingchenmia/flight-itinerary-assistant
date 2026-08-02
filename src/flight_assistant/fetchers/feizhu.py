from playwright.sync_api import Page

from flight_assistant.fetchers.base import PlatformFetcher
from flight_assistant.models import PlatformOffer

# URL 模板来自需求文档，未做过人工核对的部分见下方 TODO。
_LIST_URL = (
    "https://sfs.fliggy.com/flight/list.htm"
    "?depCity={origin}&arrCity={dest}&depDate={date}"
)


class FeizhuFetcher(PlatformFetcher):
    platform_name = "feizhu"

    def fetch_offers(
        self, origin: str, dest: str, date: str, page: Page
    ) -> list[PlatformOffer]:
        url = _LIST_URL.format(origin=origin, dest=dest, date=date)
        page.goto(url, wait_until="domcontentloaded")

        # TODO(人工核对真实页面后填入): 结果列表的稳定选择器。
        # 飞猪页面结构和携程不同，先跑通 Ctrip 再复制这个模式过来，
        # 不要两个平台同时开工核对选择器。
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
            "Feizhu 选择器待人工核对真实页面后填入 (see fetchers/feizhu.py TODO)"
        )
