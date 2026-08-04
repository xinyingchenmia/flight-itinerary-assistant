from abc import ABC, abstractmethod

from playwright.sync_api import Page

from flight_assistant.models import Itinerary, PlatformOffer

# 一条取数结果 = 行程 + 该平台给出的报价。
#
# 需求文档里 fetch_offers 的返回类型写的是 list[PlatformOffer]，但那样会丢掉
# 行程本身：matching.group_and_compare 需要按 itinerary_key（承运方+航段+时刻）
# 分组，光有报价算不出 key。所以这里改成返回配对。
FetchResult = tuple[Itinerary, PlatformOffer]


class PlatformFetcher(ABC):
    platform_name: str

    @abstractmethod
    def fetch_offers(
        self, origin: str, dest: str, date: str, page: Page
    ) -> list[FetchResult]:
        """origin/dest 为三字机场码或城市码，date 为 YYYY-MM-DD。

        单次调用，不做重试到限流；失败直接抛异常，由调用方
        （scripts/fetch_live.py）捕获并把该平台标记为缺失，
        不在这一层做降级判断。
        """
        ...
