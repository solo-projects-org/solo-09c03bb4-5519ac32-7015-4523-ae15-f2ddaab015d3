"""核心采集逻辑：沿“更正自”链接遍历，整次同版、冲突即失败。

关键保证：
- 整次采集的所有页面必须返回同一个站点版号，任一页面版号不同 -> 跨版失败；
- 循环链接按 URL 去重，不会重复采集；
- 同一通报号出现在多个 URL 时，正文哈希必须一致，否则“同号异文”失败；
- 任一链接失效 -> 整次失败。
失败时返回结构化结果，由服务层决定不发布。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlparse

from .fetcher import FetchedPage, FetchError
from .parser import ParsedPage, parse_html


@dataclass
class NoticeRecord:
    notice_number: str
    content_hash: str
    site_revision: int
    source_urls: list[str]
    titles: list[str | None]
    batches: list[tuple[str, str]]  # (批号, 来源 URL)
    correction_targets: list[tuple[str, str]]  # (目标 URL, 来源 URL)


@dataclass
class CrawlResult:
    ok: bool
    failure_reason: str | None = None
    conflict_detail: dict | None = None
    site_revision: int | None = None
    pages_fetched: int = 0
    notices: dict[str, NoticeRecord] = field(default_factory=dict)
    edges: list[dict] = field(default_factory=list)


def _normalize_url(base: str, href: str) -> str:
    absolute = urljoin(base, href)
    absolute, _ = urldefrag(absolute)
    return absolute


async def run_crawl(fetcher, seed_notice_number: str) -> CrawlResult:
    """执行一次采集。fetcher 需实现 async fetch(path) -> FetchedPage。"""
    result = CrawlResult(ok=False)

    def fail(reason: str, detail: dict | None = None) -> CrawlResult:
        result.failure_reason = reason
        result.conflict_detail = detail
        result.ok = False
        return result

    # 1) 抓索引页，建立 通报号 -> URL 列表 映射
    try:
        index_page = await fetcher.fetch("/index.html")
    except FetchError as exc:
        return fail(f"索引页抓取失败: {exc.message}")
    result.pages_fetched = 1
    revision = index_page.site_revision
    result.site_revision = revision

    index = parse_html(index_page.html)
    number_to_urls: dict[str, list[str]] = {}
    for number, href in index.index_entries:
        url = _normalize_url(index_page.url, href)
        number_to_urls.setdefault(number, [])
        if url not in number_to_urls[number]:
            number_to_urls[number].append(url)
    url_to_number = {u: n for n, urls in number_to_urls.items() for u in urls}

    seed_urls = number_to_urls.get(seed_notice_number)
    if not seed_urls:
        return fail(f"索引中找不到通报号: {seed_notice_number}",
                    {"notice_number": seed_notice_number})

    # 2) BFS：按 URL 去重，天然斩断循环。
    #    同号的所有 URL（如归档镜像）都纳入采集，用于合并与同号异文检测。
    queue: deque[str] = deque()
    queued: set[str] = set()
    for url in seed_urls:
        queue.append(url)
        queued.add(url)
    fetched: dict[str, tuple[FetchedPage, ParsedPage]] = {}

    while queue:
        url = queue.popleft()
        try:
            page = await fetcher.fetch(url)
        except FetchError as exc:
            return fail(f"链接失效，整次采集终止: {exc.message}",
                        {"url": exc.url, "status_code": exc.status_code})
        if page.site_revision != revision:
            return fail("检测到跨版采集，整次采集终止",
                        {"expected_revision": revision,
                         "actual_revision": page.site_revision,
                         "changed_url": page.url})
        parsed = parse_html(page.html)
        fetched[url] = (page, parsed)
        result.pages_fetched += 1

        for href in parsed.correction_hrefs:
            target = _normalize_url(page.url, href)
            if target not in queued:
                queued.add(target)
                queue.append(target)

        # 该通报号在索引下登记的其余 URL（如归档镜像）也要采集，
        # 用于按通报号合并来源并检测同号异文
        page_number = parsed.notice_number or url_to_number.get(url)
        if page_number:
            for extra in number_to_urls.get(page_number, []):
                if extra not in queued:
                    queued.add(extra)
                    queue.append(extra)

    # 3) 解析公告，按通报号合并不同 URL；同号异文即冲突
    notices: dict[str, NoticeRecord] = {}
    for url, (page, parsed) in fetched.items():
        number = parsed.notice_number or url_to_number.get(url)
        if not number:
            return fail("公告页缺少通报号，无法归档", {"url": url})
        existing = notices.get(number)
        if existing is None:
            notices[number] = NoticeRecord(
                notice_number=number,
                content_hash=parsed.content_hash,
                site_revision=revision,
                source_urls=[url],
                titles=[parsed.title],
                batches=[(b, url) for b in parsed.batches],
                correction_targets=[(_normalize_url(url, h), url)
                                    for h in parsed.correction_hrefs],
            )
            continue
        existing.source_urls.append(url)
        if parsed.title:
            existing.titles.append(parsed.title)
        existing.batches.extend((b, url) for b in parsed.batches)
        existing.correction_targets.extend(
            (_normalize_url(url, h), url) for h in parsed.correction_hrefs
        )
        if parsed.content_hash != existing.content_hash:
            return fail(
                "同一通报号在不同 URL 上正文不一致（同号异文），整次采集终止",
                {"notice_number": number,
                 "urls": existing.source_urls,
                 "content_hashes": [existing.content_hash, parsed.content_hash]},
            )

    # 4) 构造引用边：目标 URL 解析为通报号；无法解析则失败
    edges: list[dict] = []
    for number, record in notices.items():
        for target_url, source_url in record.correction_targets:
            target_number = url_to_number.get(target_url)
            if target_number is None:
                target_parsed = fetched.get(target_url)
                if target_parsed is not None:
                    target_number = target_parsed[1].notice_number
            if not target_number:
                return fail("更正自链接指向的页面无法确定通报号",
                            {"source_notice": number, "target_url": target_url})
            edges.append({"from_notice": number, "to_notice": target_number,
                          "source_url": source_url, "target_url": target_url})

    result.ok = True
    result.notices = notices
    result.edges = edges
    return result
