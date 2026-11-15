"""SQLite 持久层。

发布语义：
- 一次采集的整张子图（snapshot + notices + sources + batches + edges）
  在单个 IMMEDIATE 事务内写入，外部读者要么看到完整新图，要么看到旧图，
  永远读不到半张图；进程崩溃后重启会清理 staging 残留。
- CAS：仅当新图 site_revision 严格大于当前最新已发布版号，或内容哈希
  完全相同（幂等 no-op）时才允许发布；并发下晚完成的旧版发布被拒绝。
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class StaleRevisionError(Exception):
    """CAS 失败：试图用较旧或同版异文的图覆盖最新图。"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._init_schema()
        self.cleanup_staging()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def _init_schema(self) -> None:
        # executescript 会自行提交，不能包在手动事务里
        self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    site_revision INTEGER NOT NULL,
                    root_notice_number TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('staging','published')),
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    seed_notice_number TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('published','failed','rejected_stale')),
                    site_revision INTEGER,
                    snapshot_id INTEGER,
                    pages_fetched INTEGER NOT NULL DEFAULT 0,
                    failure_reason TEXT,
                    conflict_detail TEXT,
                    started_at TEXT NOT NULL DEFAULT (datetime('now')),
                    finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS notices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
                    notice_number TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    site_revision INTEGER NOT NULL,
                    UNIQUE(snapshot_id, notice_number)
                );
                CREATE TABLE IF NOT EXISTS notice_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    notice_id INTEGER NOT NULL REFERENCES notices(id),
                    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
                    url TEXT NOT NULL,
                    title TEXT,
                    content_hash TEXT NOT NULL,
                    fetched_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notice_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    notice_id INTEGER NOT NULL REFERENCES notices(id),
                    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
                    batch_number TEXT NOT NULL,
                    source_url TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notice_edges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
                    from_notice TEXT NOT NULL,
                    to_notice TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    target_url TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_snapshots_lookup
                    ON snapshots(status, root_notice_number);
                -- 每个通报号当前指向的最新已发布快照；
                -- 新图发布时只更新图内通报号的指针，不影响其他通报号的既有图
                CREATE TABLE IF NOT EXISTS notice_latest (
                    notice_number TEXT PRIMARY KEY,
                    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
                    site_revision INTEGER NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS idx_notice_latest_snap
                    ON notice_latest(snapshot_id);
                CREATE INDEX IF NOT EXISTS idx_notices_number
                    ON notices(snapshot_id, notice_number);
                CREATE INDEX IF NOT EXISTS idx_edges_from ON notice_edges(snapshot_id, from_notice);
                CREATE INDEX IF NOT EXISTS idx_edges_to ON notice_edges(snapshot_id, to_notice);
                """
            )

    def cleanup_staging(self) -> int:
        """删除未提交事务遗留的 staging 图（崩溃恢复）。"""
        with self._tx() as c:
            rows = c.execute(
                "SELECT id FROM snapshots WHERE status='staging'"
            ).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                placeholders = ",".join("?" * len(ids))
                for table in ("notice_edges", "notice_batches",
                              "notice_sources", "notices", "snapshots"):
                    col = "id" if table == "snapshots" else "snapshot_id"
                    c.execute(f"DELETE FROM {table} WHERE {col} IN ({placeholders})", ids)
        return len(ids)

    # ---- 运行记录 -------------------------------------------------
    def start_run(self, seed: str) -> int:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO runs(seed_notice_number, status) VALUES (?, 'failed')",
                (seed,),
            )
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, *, site_revision: int | None,
                   snapshot_id: int | None, pages_fetched: int,
                   failure_reason: str | None, conflict_detail: str | None) -> None:
        with self._tx() as c:
            c.execute(
                """UPDATE runs SET status=?, site_revision=?, snapshot_id=?,
                   pages_fetched=?, failure_reason=?, conflict_detail=?,
                   finished_at=datetime('now') WHERE id=?""",
                (status, site_revision, snapshot_id, pages_fetched,
                 failure_reason, conflict_detail, run_id),
            )

    # ---- 原子发布 + CAS ------------------------------------------
    def publish_snapshot(self, crawl_result, root_notice_number: str) -> dict[str, Any]:
        """在单事务内写整张子图并做 CAS 检查。返回 {snapshot_id, outcome}。"""
        import json

        notices = crawl_result.notices
        edges = crawl_result.edges
        revision = crawl_result.site_revision
        content_hash = _graph_hash(notices, edges)
        now = time.strftime("%Y-%m-%d %H:%M:%S")

        with self._tx() as c:
            # CAS 以“同一通报号种子”的最新已发布版号为准：
            # 不同种子的图互不影响，晚完成的旧版不能覆盖同种子的新版
            row = c.execute(
                """SELECT s.id, s.site_revision, s.content_hash FROM snapshots s
                   WHERE s.status='published' AND s.root_notice_number=?
                   ORDER BY s.site_revision DESC, s.id DESC LIMIT 1""",
                (root_notice_number,),
            ).fetchone()
            latest_revision = row["site_revision"] if row else 0
            if row is not None and revision < latest_revision:
                raise StaleRevisionError(
                    f"拒绝发布：本次采集版号 {revision} 早于通报 {root_notice_number} "
                    f"的最新已发布版号 {latest_revision}"
                )

            # 幂等：同种子、同版号、同内容才复用；
            # 仅内容相同但站点版号已推进，必须产生新快照以保留版号谱系
            if (row is not None and row["site_revision"] == revision
                    and row["content_hash"] == content_hash):
                return {"snapshot_id": row["id"], "outcome": "noop"}

            # 已发布快照不可变；新图独立成行，仅更新本图内通报号的最新指针
            cur = c.execute(
                """INSERT INTO snapshots(site_revision, root_notice_number,
                   content_hash, status) VALUES (?, ?, ?, 'staging')""",
                (revision, root_notice_number, content_hash),
            )
            snapshot_id = int(cur.lastrowid)

            for number, record in notices.items():
                ncur = c.execute(
                    """INSERT INTO notices(snapshot_id, notice_number, content_hash,
                       site_revision) VALUES (?, ?, ?, ?)""",
                    (snapshot_id, number, record.content_hash, revision),
                )
                notice_id = int(ncur.lastrowid)
                seen_urls: set[str] = set()
                for url in record.source_urls:
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    c.execute(
                        """INSERT INTO notice_sources(notice_id, snapshot_id, url,
                           title, content_hash, fetched_at) VALUES (?, ?, ?, ?, ?, ?)""",
                        (notice_id, snapshot_id, url,
                         record.titles[0] if record.titles else None,
                         record.content_hash, now),
                    )
                seen_batches: set[tuple[str, str]] = set()
                for batch_number, source_url in record.batches:
                    key = (batch_number, source_url)
                    if key in seen_batches:
                        continue
                    seen_batches.add(key)
                    c.execute(
                        """INSERT INTO notice_batches(notice_id, snapshot_id,
                           batch_number, source_url) VALUES (?, ?, ?, ?)""",
                        (notice_id, snapshot_id, batch_number, source_url),
                    )
            for edge in edges:
                c.execute(
                    """INSERT INTO notice_edges(snapshot_id, from_notice, to_notice,
                       source_url, target_url) VALUES (?, ?, ?, ?, ?)""",
                    (snapshot_id, edge["from_notice"], edge["to_notice"],
                     edge["source_url"], edge["target_url"]),
                )

            c.execute("UPDATE snapshots SET status='published' WHERE id=?",
                      (snapshot_id,))
            # 仅推进本图内通报号的最新指针
            for number in notices:
                c.execute(
                    """INSERT INTO notice_latest(notice_number, snapshot_id, site_revision, updated_at)
                       VALUES (?, ?, ?, datetime('now'))
                       ON CONFLICT(notice_number) DO UPDATE SET
                         snapshot_id=excluded.snapshot_id,
                         site_revision=excluded.site_revision,
                         updated_at=datetime('now')""",
                    (number, snapshot_id, revision),
                )
            return {"snapshot_id": snapshot_id, "outcome": "published",
                    "conflict_detail": None}

    # ---- 查询 -----------------------------------------------------
    def latest_published_snapshot(self, notice_number: str | None = None) -> sqlite3.Row | None:
        if notice_number is None:
            return self._conn.execute(
                "SELECT * FROM snapshots WHERE status='published' "
                "ORDER BY site_revision DESC, id DESC LIMIT 1"
            ).fetchone()
        return self._conn.execute(
            """SELECT s.* FROM snapshots s
               JOIN notice_latest nl ON nl.snapshot_id=s.id
               WHERE nl.notice_number=?""",
            (notice_number,),
        ).fetchone()

    def get_notice(self, notice_number: str) -> dict | None:
        snap = self.latest_published_snapshot(notice_number)
        if snap is None:
            return None
        sid = snap["id"]
        notice = self._conn.execute(
            "SELECT * FROM notices WHERE snapshot_id=? AND notice_number=?",
            (sid, notice_number),
        ).fetchone()
        if notice is None:
            return None
        sources = [dict(r) for r in self._conn.execute(
            "SELECT url, title, fetched_at, content_hash FROM notice_sources "
            "WHERE snapshot_id=? AND notice_id=? ORDER BY url",
            (sid, notice["id"])).fetchall()]
        batches = [dict(r) for r in self._conn.execute(
            "SELECT batch_number AS batch_number, source_url FROM notice_batches "
            "WHERE snapshot_id=? AND notice_id=? ORDER BY batch_number",
            (sid, notice["id"])).fetchall()]
        corrects = [r["to_notice"] for r in self._conn.execute(
            "SELECT DISTINCT to_notice FROM notice_edges WHERE snapshot_id=? AND from_notice=? "
            "ORDER BY to_notice", (sid, notice_number)).fetchall()]
        corrected_by = [r["from_notice"] for r in self._conn.execute(
            "SELECT DISTINCT from_notice FROM notice_edges WHERE snapshot_id=? AND to_notice=? "
            "ORDER BY from_notice", (sid, notice_number)).fetchall()]
        return {
            "notice_number": notice_number,
            "latest_snapshot_id": sid,
            "site_revision": snap["site_revision"],
            "content_hash": notice["content_hash"],
            "sources": sources,
            "batches": batches,
            "corrects": corrects,
            "corrected_by": corrected_by,
        }

    def get_ancestors(self, notice_number: str) -> list[dict] | None:
        snap = self.latest_published_snapshot(notice_number)
        if snap is None:
            return None
        sid = snap["id"]
        edges = self._conn.execute(
            "SELECT from_notice, to_notice, source_url FROM notice_edges WHERE snapshot_id=?",
            (sid,)).fetchall()
        # from -> to 表示 from 更正自 to；祖先即沿 to 方向遍历
        adjacency: dict[str, list[tuple[str, str]]] = {}
        for e in edges:
            adjacency.setdefault(e["from_notice"], []).append(
                (e["to_notice"], e["source_url"]))
        result: list[dict] = []
        seen = {notice_number}
        queue = [(notice_number, 0, None)]
        while queue:
            current, depth, via = queue.pop(0)
            if depth > 0:
                result.append({"notice_number": current, "depth": depth, "via_url": via})
            for nxt, via_url in adjacency.get(current, []):
                if nxt in seen:  # 循环图不重复展开
                    continue
                seen.add(nxt)
                queue.append((nxt, depth + 1, via_url))
        return result

    def get_graph(self) -> dict | None:
        snap = self.latest_published_snapshot()
        if snap is None:
            return None
        sid = snap["id"]
        notices = [dict(r) for r in self._conn.execute(
            "SELECT notice_number, content_hash, site_revision FROM notices "
            "WHERE snapshot_id=? ORDER BY notice_number", (sid,)).fetchall()]
        edges = [dict(r) for r in self._conn.execute(
            "SELECT from_notice, to_notice, source_url, target_url FROM notice_edges "
            "WHERE snapshot_id=? ORDER BY id", (sid,)).fetchall()]
        return {"snapshot_id": sid, "site_revision": snap["site_revision"],
                "root_notice_number": snap["root_notice_number"],
                "notices": notices, "edges": edges}

    def list_runs(self, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d.get("conflict_detail"):
                try:
                    import json
                    d["conflict_detail"] = json.loads(d["conflict_detail"])
                except (ValueError, TypeError):
                    pass
            out.append(d)
        return out

    def list_snapshots(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, site_revision, root_notice_number, content_hash, status, created_at "
            "FROM snapshots ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self._conn.close()


def _graph_hash(notices, edges) -> str:
    import hashlib
    import json
    payload = json.dumps(
        {"notices": {n: r.content_hash for n, r in sorted(notices.items())},
         "edges": sorted((e["from_notice"], e["to_notice"], e["target_url"])
                         for e in edges)},
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
