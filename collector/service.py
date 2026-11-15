"""采集编排服务：把爬取结果原子发布到数据库，并记录每次运行。"""
from __future__ import annotations

import json

from .crawler import run_crawl
from .db import Database, StaleRevisionError


class CollectorService:
    def __init__(self, db: Database, fetcher) -> None:
        self.db = db
        self.fetcher = fetcher

    async def collect(self, notice_number: str) -> dict:
        run_id = self.db.start_run(notice_number)
        crawl = await run_crawl(self.fetcher, notice_number)

        if not crawl.ok:
            self.db.finish_run(
                run_id, "failed",
                site_revision=crawl.site_revision, snapshot_id=None,
                pages_fetched=crawl.pages_fetched,
                failure_reason=crawl.failure_reason,
                conflict_detail=json.dumps(crawl.conflict_detail, ensure_ascii=False)
                if crawl.conflict_detail else None,
            )
            return {"run_id": run_id, "status": "failed",
                    "snapshot_id": None, "site_revision": crawl.site_revision,
                    "seed_notice_number": notice_number,
                    "pages_fetched": crawl.pages_fetched,
                    "notices": len(crawl.notices), "edges": len(crawl.edges),
                    "failure_reason": crawl.failure_reason,
                    "conflict_detail": crawl.conflict_detail,
                    "http_status": 409}

        try:
            pub = self.db.publish_snapshot(crawl, notice_number)
        except StaleRevisionError as exc:
            self.db.finish_run(
                run_id, "rejected_stale",
                site_revision=crawl.site_revision, snapshot_id=None,
                pages_fetched=crawl.pages_fetched,
                failure_reason=str(exc), conflict_detail=None,
            )
            return {"run_id": run_id, "status": "rejected_stale",
                    "snapshot_id": None, "site_revision": crawl.site_revision,
                    "seed_notice_number": notice_number,
                    "pages_fetched": crawl.pages_fetched,
                    "notices": len(crawl.notices), "edges": len(crawl.edges),
                    "failure_reason": str(exc), "conflict_detail": None,
                    "http_status": 409}

        self.db.finish_run(
            run_id, "published",
            site_revision=crawl.site_revision, snapshot_id=pub["snapshot_id"],
            pages_fetched=crawl.pages_fetched,
            failure_reason=None if pub["outcome"] == "published"
            else "内容与当前已发布图完全一致，幂等复用",
            conflict_detail=None,
        )
        return {"run_id": run_id,
                "status": "published" if pub["outcome"] == "published" else "noop",
                "snapshot_id": pub["snapshot_id"],
                "site_revision": crawl.site_revision,
                "seed_notice_number": notice_number,
                "pages_fetched": crawl.pages_fetched,
                "notices": len(crawl.notices), "edges": len(crawl.edges),
                "failure_reason": None, "conflict_detail": None,
                "http_status": 200}
