"""持久层测试：原子发布、CAS 旧版拒绝、幂等、崩溃恢复、查询旧图。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collector.db import Database, StaleRevisionError
from tests.fakes import FakeFetcher, FakePage
from tests.html_fixtures import index_html, notice_html


class _MiniRecord:
    def __init__(self, number, hash_, urls, batches, correction_targets):
        self.notice_number = number
        self.content_hash = hash_
        self.source_urls = urls
        self.titles = [f"title-{number}"]
        self.batches = batches
        self.correction_targets = correction_targets


class _MiniCrawl:
    def __init__(self, revision, notices, edges):
        self.site_revision = revision
        self.notices = notices
        self.edges = edges


def _crawl(revision: int, suffix: str = "") -> _MiniCrawl:
    notices = {
        "N1": _MiniRecord("N1", f"hash-n1-{revision}{suffix}",
                          ["http://x/n1"], [("B1", "http://x/n1")], []),
        "N2": _MiniRecord("N2", f"hash-n2-{revision}{suffix}",
                          ["http://x/n2"], [("B2", "http://x/n2")],
                          [("http://x/n1", "http://x/n2")]),
    }
    edges = [{"from_notice": "N2", "to_notice": "N1",
              "source_url": "http://x/n2", "target_url": "http://x/n1"}]
    return _MiniCrawl(revision, notices, edges)


def test_publish_and_query(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    out = db.publish_snapshot(_crawl(1), "N2")
    assert out["outcome"] == "published"
    data = db.get_notice("N2")
    assert data["site_revision"] == 1
    assert data["corrects"] == ["N1"]
    assert data["batches"][0]["batch_number"] == "B2"
    ancestors = db.get_ancestors("N2")
    # via_url 是“发现这条边的公告页”URL，即 N2 自身页面
    assert ancestors == [{"notice_number": "N1", "depth": 1, "via_url": "http://x/n2"}]
    db.close()


def test_cas_rejects_late_older_revision(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.publish_snapshot(_crawl(2), "N2")  # 先完成的新版
    with pytest.raises(StaleRevisionError):
        db.publish_snapshot(_crawl(1), "N2")  # 晚完成的旧版
    # 最新图仍是 revision 2
    assert db.latest_published_snapshot()["site_revision"] == 2
    assert db.get_notice("N1")["site_revision"] == 2
    db.close()


def test_same_revision_different_graphs_coexist(tmp_path):
    # 同版号下采集不同种子得到不同子图：各自独立成图，互不覆盖
    db = Database(str(tmp_path / "t.db"))
    first = db.publish_snapshot(_crawl(1), "N2")
    second = db.publish_snapshot(_crawl(1, suffix="-other-seed"), "N9")
    assert first["outcome"] == "published"
    assert second["outcome"] == "published"
    assert second["snapshot_id"] != first["snapshot_id"]
    assert len(db.list_snapshots()) == 2
    db.close()


def test_idempotent_same_graph_noop(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    first = db.publish_snapshot(_crawl(1), "N2")
    again = db.publish_snapshot(_crawl(1), "N2")
    assert again["outcome"] == "noop"
    assert again["snapshot_id"] == first["snapshot_id"]
    assert len(db.list_snapshots()) == 1
    db.close()


def test_multiple_snapshots_coexist_with_latest_pointer(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    first = db.publish_snapshot(_crawl(1), "N2")
    second = db.publish_snapshot(_crawl(2), "N2")
    # 两张快照都永久保留为 published，不互相覆盖
    snapshots = db.list_snapshots()
    assert all(s["status"] == "published" for s in snapshots)
    assert {s["id"] for s in snapshots} == {first["snapshot_id"], second["snapshot_id"]}
    # 查询经 notice_latest 指针解析到新版
    assert db.get_notice("N2")["site_revision"] == 2
    db.close()


def test_unrelated_seed_graph_not_affected_by_new_publish(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.publish_snapshot(_crawl(1), "N2")

    # 另一棵完全不相干的子图（不同通报号），发布后不得影响 N1/N2 的指针
    other_notices = {
        "N8": _MiniRecord("N8", "hash-n8", ["http://x/n8"], [("B8", "http://x/n8")], []),
        "N9": _MiniRecord("N9", "hash-n9", ["http://x/n9"], [("B9", "http://x/n9")],
                          [("http://x/n8", "http://x/n9")]),
    }
    other_edges = [{"from_notice": "N9", "to_notice": "N8",
                    "source_url": "http://x/n9", "target_url": "http://x/n8"}]
    db.publish_snapshot(_MiniCrawl(3, other_notices, other_edges), "N9")

    assert db.get_notice("N2")["site_revision"] == 1
    assert db.get_notice("N1")["site_revision"] == 1
    assert db.get_notice("N9")["site_revision"] == 3
    db.close()


def test_restart_cleans_orphan_staging(tmp_path):
    path = str(tmp_path / "t.db")
    db = Database(path)
    # 手工制造一张“半张图”：staging 快照 + 部分 notice，模拟进程在事务中崩溃
    with db._tx() as c:
        cur = c.execute(
            "INSERT INTO snapshots(site_revision, root_notice_number, content_hash, status)"
            " VALUES (9, 'N2', 'partial', 'staging')")
        sid = cur.lastrowid
        c.execute(
            "INSERT INTO notices(snapshot_id, notice_number, content_hash, site_revision)"
            " VALUES (?, 'N2', 'partial', 9)", (sid,))
        c.execute(
            "INSERT INTO notice_batches(notice_id, snapshot_id, batch_number, source_url)"
            " VALUES (1, ?, 'BX', 'http://x')", (sid,))
    db.close()

    db2 = Database(path)  # 重新打开即清理
    rows = db2.list_snapshots()
    assert all(s["status"] != "staging" for s in rows)
    assert db2._conn.execute("SELECT COUNT(*) FROM notices").fetchone()[0] == 0
    assert db2._conn.execute("SELECT COUNT(*) FROM notice_batches").fetchone()[0] == 0
    assert db2._conn.execute("SELECT COUNT(*) FROM notice_latest").fetchone()[0] == 0
    # 正常发布不受影响
    db2.publish_snapshot(_crawl(1), "N2")
    assert db2.get_notice("N2") is not None
    db2.close()
