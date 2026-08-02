from abc import ABC, abstractmethod

from playwright.sync_api import Page

from flight_assistant.models import PlatformOffer


class PlatformFetcher(ABC):
    platform_name: str

    @abstractmethod
    def fetch_offers(
        self, origin: str, dest: str, date: str, page: Page
    ) -> list[PlatformOffer]:
        """origin/dest 为三字机场码，date 为 YYYY-MM-DD。

        单次调用，不做重试到限流；失败直接抛异常，由调用方
        （scripts/fetch_live.py）捕获并把该平台标记为缺失，
        不在这一层做降级判断。
        """
        ...
