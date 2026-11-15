"""测试用假抓取器：内存中模拟版本化公告站。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from collector.fetcher import FetchedPage, FetchError


@dataclass
class FakePage:
    html: str
    revision: int


class FakeFetcher:
    """按 (revision, path) 组织页面；可在采集过程中切换 revision 模拟跨版。"""

    def __init__(self, pages: dict[int, dict[str, FakePage]], start_revision: int) -> None:
        self.pages = pages
        self.revision = start_revision
        self.calls: list[str] = []
        self._lock = asyncio.Lock()

    def set_revision(self, revision: int) -> None:
        self.revision = revision

    async def fetch(self, path: str) -> FetchedPage:
        async with self._lock:
            self.calls.append(path)
            rev = self.revision
            bucket = self.pages.get(rev, {})
            from urllib.parse import urlparse
            key = urlparse(path).path if path.startswith("http") else path
            if key not in bucket:
                raise FetchError(f"http://fake.test{key}", 404,
                                 f"链接失效: HTTP 404 (revision={rev})")
            page = bucket[key]
            return FetchedPage(url=f"http://fake.test{key}", html=page.html,
                               site_revision=rev, status_code=200)

    async def aclose(self) -> None:
        return None
