"""并发采集测试：两个采集任务针对同一站点不同版号，晚完成的旧版不得覆盖新版。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collector.db import Database
from collector.service import CollectorService
from tests.fakes import FakeFetcher, FakePage
from tests.html_fixtures import index_html, notice_html


def _pages(revision: int, extra: str) -> dict:
    p = {
        "/index.html": index_html([("N1", "/notice/n1.html")]),
        "/notice/n1.html": notice_html("N1", f"公告 r{revision}", [f"B{revision}"],
                                       extra_body=extra),
    }
    return {path: FakePage(html, revision) for path, html in p.items()}


class ControllableFetcher:
    """每个实例抓取前先在 barrier 上同步，再由调用方决定放行顺序。"""

    def __init__(self, pages, revision, gate: asyncio.Event):
        self.pages = pages
        self.revision = revision
        self.gate = gate

    async def fetch(self, path: str) -> FetchedPageLike:
        from urllib.parse import urlparse
        key = urlparse(path).path if path.startswith("http") else path
        if key == "/notice/n1.html":
            await self.gate.wait()  # 两个任务都抓到索引后在此排队
        page = self.pages[key]
        return FetchedPageLike(f"http://x{key}", page.html, self.revision, 200)


class FetchedPageLike:
    def __init__(self, url, html, revision, status):
        self.url, self.html, self.site_revision, self.status_code = (
            url, html, revision, status)


def test_concurrent_old_revision_cannot_overwrite_new(tmp_path):
    db = Database(str(tmp_path / "conc.db"))
    new_gate, old_gate = asyncio.Event(), asyncio.Event()
    new_fetcher = ControllableFetcher(_pages(2, "new-content"), 2, new_gate)
    old_fetcher = ControllableFetcher(_pages(1, "old-content"), 1, old_gate)
    new_service = CollectorService(db, new_fetcher)
    old_service = CollectorService(db, old_fetcher)

    async def scenario():
        old_task = asyncio.create_task(old_service.collect("N1"))
        new_task = asyncio.create_task(new_service.collect("N1"))
        await asyncio.sleep(0.05)
        # 先放行新版（revision=2），再放行旧版（revision=1）
        new_gate.set()
        new_result = await new_task
        old_gate.set()
        old_result = await old_task
        return new_result, old_result

    new_result, old_result = asyncio.run(scenario())
    assert new_result["status"] == "published"
    assert new_result["site_revision"] == 2
    assert old_result["status"] == "rejected_stale"
    assert old_result["site_revision"] == 1

    latest = db.latest_published_snapshot()
    assert latest["site_revision"] == 2
    notice = db.get_notice("N1")
    assert notice["site_revision"] == 2
    assert notice["batches"][0]["batch_number"] == "B2"

    runs = db.list_runs()
    assert {r["status"] for r in runs} == {"published", "rejected_stale"}
    db.close()
