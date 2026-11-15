"""HTTP 抓取客户端。"""
from __future__ import annotations

from dataclasses import dataclass

import httpx


class FetchError(Exception):
    def __init__(self, url: str, status_code: int | None, message: str) -> None:
        super().__init__(message)
        self.url = url
        self.status_code = status_code
        self.message = message


@dataclass
class FetchedPage:
    url: str
    html: str
    site_revision: int
    status_code: int


class HttpFetcher:
    """真实 HTTP 抓取；测试中可用实现相同接口的假抓取器替换。"""

    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=timeout,
                                         follow_redirects=True)

    async def fetch(self, path: str) -> FetchedPage:
        url = path if path.startswith("http") else self._base_url + path
        try:
            resp = await self._client.get(url)
        except httpx.HTTPError as exc:
            raise FetchError(url, None, f"请求失败: {exc!r}") from exc
        revision_header = resp.headers.get("X-Site-Revision")
        if revision_header is None:
            raise FetchError(str(resp.url), resp.status_code,
                             "响应缺少 X-Site-Revision 版号头，无法保证整次采集同版")
        try:
            revision = int(revision_header)
        except ValueError as exc:
            raise FetchError(str(resp.url), resp.status_code,
                             f"X-Site-Revision 不是整数: {revision_header!r}") from exc
        if resp.status_code >= 400:
            raise FetchError(str(resp.url), resp.status_code,
                             f"链接失效: HTTP {resp.status_code}")
        return FetchedPage(url=str(resp.url), html=resp.text,
                           site_revision=revision, status_code=resp.status_code)

    async def aclose(self) -> None:
        await self._client.aclose()
