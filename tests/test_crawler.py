"""采集核心逻辑测试：更正链、循环去重、跨版、同号异文、链接失效。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collector.crawler import run_crawl
from tests.fakes import FakeFetcher, FakePage
from tests.html_fixtures import index_html, notice_html

P = {
    "/index.html": index_html([
        ("N1", "/notice/n1.html"),
        ("N1", "/notice/n1_mirror.html"),
        ("N2", "/notice/n2.html"),
        ("N3", "/notice/n3.html"),
        ("CA", "/notice/ca.html"),
        ("CB", "/notice/cb.html"),
    ]),
    "/notice/n1.html": notice_html("N1", "公告1", ["B1", "B2"]),
    "/notice/n1_mirror.html": notice_html("N1", "公告1", ["B1", "B2"]),
    "/notice/n2.html": notice_html("N2", "公告2", ["B2"], "/notice/n1.html"),
    "/notice/n3.html": notice_html("N3", "公告3", ["B2", "B3"], "/notice/n2.html"),
    "/notice/ca.html": notice_html("CA", "循环A", ["BA"], "/notice/cb.html"),
    "/notice/cb.html": notice_html("CB", "循环B", ["BB"], "/notice/ca.html"),
}


def _fetcher(pages_by_rev: dict[int, dict]) -> FakeFetcher:
    return FakeFetcher({rev: {p: FakePage(h, rev) for p, h in pages.items()}
                        for rev, pages in pages_by_rev.items()}, start_revision=1)


def test_crawl_correction_chain_and_mirror_merge():
    fetcher = _fetcher({1: P})
    result = asyncio.run(run_crawl(fetcher, "N3"))
    assert result.ok
    assert result.site_revision == 1
    assert set(result.notices) == {"N3", "N2", "N1"}
    # N1 合并了两个 URL
    assert sorted(result.notices["N1"].source_urls) == [
        "http://fake.test/notice/n1.html",
        "http://fake.test/notice/n1_mirror.html",
    ]
    # 批号：N1 两个来源各两条，去重由发布层按 (批号,URL) 处理
    batches_n1 = result.notices["N1"].batches
    assert ("B1", "http://fake.test/notice/n1.html") in batches_n1
    # 引用边
    pairs = {(e["from_notice"], e["to_notice"]) for e in result.edges}
    assert pairs == {("N3", "N2"), ("N2", "N1")}
    # 索引 + N3/N2/N1/N1镜像 = 5 次抓取
    assert result.pages_fetched == 5


def test_cycle_links_not_fetched_twice():
    fetcher = _fetcher({1: P})
    result = asyncio.run(run_crawl(fetcher, "CA"))
    assert result.ok
    # 索引 + CA + CB，CB 指回 CA 时 CA 已在队列中，不重复抓
    assert result.pages_fetched == 3
    assert len(fetcher.calls) == len(set(fetcher.calls))
    pairs = {(e["from_notice"], e["to_notice"]) for e in result.edges}
    assert pairs == {("CA", "CB"), ("CB", "CA")}


def test_cross_revision_fails_and_publishes_nothing():
    pages = {1: dict(P), 2: dict(P)}
    fetcher = FakeFetcher({rev: {p: FakePage(h, rev) for p, h in ps.items()}
                           for rev, ps in pages.items()}, start_revision=1)

    original = fetcher.fetch

    async def flaky_fetch(path):
        from urllib.parse import urlparse
        page = await original(path)
        # 所有页面先按 revision=1 正常返回，最后抓 n1 时站点改版
        key = urlparse(path).path if path.startswith("http") else path
        if key == "/notice/n1.html":
            fetcher.set_revision(2)
        return page

    fetcher.fetch = flaky_fetch
    result = asyncio.run(run_crawl(fetcher, "N3"))
    assert not result.ok
    assert "跨版" in result.failure_reason
    assert result.conflict_detail["expected_revision"] == 1
    assert result.conflict_detail["actual_revision"] == 2


def test_same_number_different_text_fails():
    pages = dict(P)
    pages["/notice/n1_mirror.html"] = notice_html(
        "N1", "公告1-镜像异文", ["B1", "B2", "B9"], extra_body="不一样的正文XYZ")
    fetcher = _fetcher({1: pages})
    result = asyncio.run(run_crawl(fetcher, "N3"))
    assert not result.ok
    assert "同号异文" in result.failure_reason
    assert result.conflict_detail["notice_number"] == "N1"


def test_same_number_different_text_seed_mirror():
    # 直接从含镜像的通报号发起采集，也必须检出同号异文
    pages = dict(P)
    pages["/notice/n1_mirror.html"] = notice_html(
        "N1", "公告1-镜像异文", ["B9"], extra_body="镜像专属不同正文")
    fetcher = _fetcher({1: pages})
    result = asyncio.run(run_crawl(fetcher, "N1"))
    assert not result.ok
    assert "同号异文" in result.failure_reason


def test_broken_link_fails_whole_run():
    pages = dict(P)
    del pages["/notice/n1.html"]  # N2 的“更正自”指向已删除页面（v3 场景）
    fetcher = _fetcher({1: pages})
    result = asyncio.run(run_crawl(fetcher, "N3"))
    assert not result.ok
    assert "链接失效" in result.failure_reason
    assert result.conflict_detail["status_code"] == 404


def test_unknown_notice_number():
    fetcher = _fetcher({1: P})
    result = asyncio.run(run_crawl(fetcher, "NOPE"))
    assert not result.ok
    assert "找不到通报号" in result.failure_reason
