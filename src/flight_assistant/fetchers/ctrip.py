from playwright.sync_api import Page

from flight_assistant.fetchers.base import PlatformFetcher
from flight_assistant.models import PlatformOffer

# URL 模板来自需求文档，未做过人工核对的部分见下方 TODO。
_LIST_URL = "https://flights.ctrip.com/online/list/oneway-{origin}-{dest}?depdate={date}"


class CtripFetcher(PlatformFetcher):
    platform_name = "ctrip"

    def fetch_offers(
        self, origin: str, dest: str, date: str, page: Page
    ) -> list[PlatformOffer]:
        url = _LIST_URL.format(origin=origin, dest=dest, date=date)
        page.goto(url, wait_until="domcontentloaded")

        # TODO(人工核对真实页面后填入): 结果列表的稳定选择器。
        # 携程结果卡片通常异步加载，需要等待具体的航班卡片容器出现，
        # 而不是固定 sleep。示例（未核对，先占位）：
        #   page.wait_for_selector(".flight-item", timeout=15000)
        #   cards = page.query_selector_all(".flight-item")
        # 然后从每张卡片里解析 price / fare_conditions_raw / booking_url。
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
            "Ctrip 选择器待人工核对真实页面后填入 (see fetchers/ctrip.py TODO)"
        )
