"""历史更正链测试：时点还原、跨入口共享取新图、未覆盖沿用旧值、断链、循环、一致性。

关键语义（对应需求）：
- 以已发布快照号为时点，每个通报取该时点最近一次发布且包含它的快照节点；
- 不同入口的图共享通报时取最近发布节点，未被新图覆盖的通报沿用原值；
- 链接 URL 不在目标当时来源中、或目标尚未发布 -> 断链，不借用后来的来源/批号；
- 循环引用每个通报只列一次；
- 历史结果只依赖不可变快照，新采集交错与重启后保持一致。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collector.db import Database


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


def rec(number, revision, *, urls=None, batches=None, corrections=None):
    urls = urls or [f"http://x/{number.lower()}-r{revision}.html"]
    batches = batches or [(f"{number}-BATCH-r{revision}", urls[0])]
    corrections = corrections or []
    return _MiniRecord(
        number, f"hash-{number}-r{revision}", urls, batches, corrections)


def crawl(revision, notices, edges):
    return _MiniCrawl(revision, notices, edges)


def edge(src, src_url, dst, dst_url):
    return {"from_notice": src, "to_notice": dst,
            "source_url": src_url, "target_url": dst_url}


def _by_number(chain, number):
    return next(n for n in chain["nodes"] if n["notice_number"] == number)


def _link(chain, src, dst):
    return next(l for l in chain["links"]
                if l["from_notice"] == src and l["to_notice"] == dst)


def test_basic_chain_reconstructs_view_at_snapshot(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    n1 = rec("N1", 1)
    n2 = rec("N2", 1, corrections=[("http://x/n1-r1.html", "http://x/n2-r1.html")])
    n3 = rec("N3", 1, corrections=[("http://x/n2-r1.html", "http://x/n3-r1.html")])
    pub = db.publish_snapshot(
        crawl(1, {"N1": n1, "N2": n2, "N3": n3},
              [edge("N3", "http://x/n3-r1.html", "N2", "http://x/n2-r1.html"),
               edge("N2", "http://x/n2-r1.html", "N1", "http://x/n1-r1.html")]),
        "N3")
    sid = pub["snapshot_id"]

    chain = db.get_historical_correction_chain(sid, "N3")
    assert chain is not None
    assert chain["snapshot_id"] == sid
    assert chain["broken_link_count"] == 0
    assert [n["notice_number"] for n in chain["nodes"]] == ["N3", "N2", "N1"]
    assert [n["depth"] for n in chain["nodes"]] == [0, 1, 2]
    # 批号、来源、所属快照都来自该时点的快照
    assert _by_number(chain, "N1")["snapshot_id"] == sid
    assert _by_number(chain, "N1")["batches"][0]["batch_number"] == "N1-BATCH-r1"
    assert _by_number(chain, "N1")["sources"][0]["url"] == "http://x/n1-r1.html"
    assert all(l["broken"] is False for l in chain["links"])
    assert _link(chain, "N2", "N1")["target_snapshot_id"] == sid
    db.close()


def test_shared_notice_takes_latest_published_node_at_that_point(tmp_path):
    """两个入口（不同种子）的图共享 N1：后发布的新图节点在旧锚点时点生效。"""
    db = Database(str(tmp_path / "t.db"))

    # 入口 A：N2 更正自 N1，N1 来源为旧 URL、旧批号
    n1_old = rec("N1", 1, urls=["http://x/n1-old.html"],
                 batches=[("OLD-BATCH", "http://x/n1-old.html")])
    n2 = rec("N2", 1, urls=["http://x/n2.html"], batches=[("B2", "http://x/n2.html")],
             corrections=[("http://x/n1-old.html", "http://x/n2.html")])
    s1 = db.publish_snapshot(
        crawl(1, {"N1": n1_old, "N2": n2},
              [edge("N2", "http://x/n2.html", "N1", "http://x/n1-old.html")]),
        "N2")["snapshot_id"]

    # 入口 B：另一棵子图重新发布 N1（新来源 URL、新批号），边仍指向 N1
    n1_new = rec("N1", 2, urls=["http://x/n1-new.html"],
                 batches=[("NEW-BATCH", "http://x/n1-new.html")])
    n5 = rec("N5", 2, urls=["http://x/n5.html"], batches=[("B5", "http://x/n5.html")],
             corrections=[("http://x/n1-new.html", "http://x/n5.html")])
    s2 = db.publish_snapshot(
        crawl(2, {"N1": n1_new, "N5": n5},
              [edge("N5", "http://x/n5.html", "N1", "http://x/n1-new.html")]),
        "N5")["snapshot_id"]
    assert s2 > s1

    # 以 s2 为时点查 N2 的链：N2 仍在 s1（未被新图覆盖，沿用原值），
    # 但共享通报 N1 取最近发布的 s2 节点
    chain = db.get_historical_correction_chain(s2, "N2")
    node_n2 = _by_number(chain, "N2")
    node_n1 = _by_number(chain, "N1")
    assert node_n2["snapshot_id"] == s1
    assert node_n2["batches"][0]["batch_number"] == "B2"
    assert node_n1["snapshot_id"] == s2
    assert node_n1["batches"][0]["batch_number"] == "NEW-BATCH"
    assert node_n1["sources"][0]["url"] == "http://x/n1-new.html"

    # 沿 N2 所在快照（s1）内的旧边走到 N1 时，旧边 URL 不在 N1 新节点来源中 -> 断链
    link = _link(chain, "N2", "N1")
    assert link["broken"] is True
    assert link["broken_reason"] == "target_url_not_in_sources"
    assert link["target_snapshot_id"] == s2
    assert chain["broken_link_count"] == 1
    db.close()


def test_uncovered_notice_keeps_original_value(tmp_path):
    """新图不覆盖的通报，在更新的锚点时点仍沿用旧快照的原值。"""
    db = Database(str(tmp_path / "t.db"))
    n1 = rec("N1", 1)
    n2 = rec("N2", 1, corrections=[("http://x/n1-r1.html", "http://x/n2-r1.html")])
    s1 = db.publish_snapshot(
        crawl(1, {"N1": n1, "N2": n2},
              [edge("N2", "http://x/n2-r1.html", "N1", "http://x/n1-r1.html")]),
        "N2")["snapshot_id"]

    # 后续发布一张完全不相干的图，不包含 N1/N2
    n8 = rec("N8", 3, urls=["http://x/n8.html"], batches=[("B8", "http://x/n8.html")])
    s2 = db.publish_snapshot(crawl(3, {"N8": n8}, []), "N8")["snapshot_id"]

    chain = db.get_historical_correction_chain(s2, "N2")
    assert _by_number(chain, "N2")["snapshot_id"] == s1
    assert _by_number(chain, "N1")["snapshot_id"] == s1
    assert _by_number(chain, "N1")["batches"][0]["batch_number"] == "N1-BATCH-r1"
    assert chain["broken_link_count"] == 0
    db.close()


def test_history_immutable_when_newer_snapshots_published_afterwards(tmp_path):
    """查询之后再发布新快照，同一（旧快照号, 通报号）的历史结果必须保持不变。"""
    db = Database(str(tmp_path / "t.db"))
    n1 = rec("N1", 1)
    n2 = rec("N2", 1, corrections=[("http://x/n1-r1.html", "http://x/n2-r1.html")])
    s1 = db.publish_snapshot(
        crawl(1, {"N1": n1, "N2": n2},
              [edge("N2", "http://x/n2-r1.html", "N1", "http://x/n1-r1.html")]),
        "N2")["snapshot_id"]

    before = db.get_historical_correction_chain(s1, "N2")

    # 交错的新采集：N1 出新版（后来的批号不得污染旧时点视图）
    n1_v2 = rec("N1", 2, urls=["http://x/n1-r2.html"],
                batches=[("LATER-BATCH", "http://x/n1-r2.html")])
    n2_v2 = rec("N2", 2, corrections=[("http://x/n1-r2.html", "http://x/n2-r2.html")])
    db.publish_snapshot(
        crawl(2, {"N1": n1_v2, "N2": n2_v2},
              [edge("N2", "http://x/n2-r2.html", "N1", "http://x/n1-r2.html")]),
        "N2")
    db.publish_snapshot(crawl(4, {"N9": rec("N9", 4)}, []), "N9")

    after = db.get_historical_correction_chain(s1, "N2")
    assert after == before
    assert _by_number(after, "N1")["snapshot_id"] == s1
    assert _by_number(after, "N1")["batches"][0]["batch_number"] == "N1-BATCH-r1"
    db.close()


def test_broken_link_when_target_not_published_at_that_point(tmp_path):
    """目标通报在锚点时点尚未发布：断链，target_snapshot_id 为 null，不展开。"""
    db = Database(str(tmp_path / "t.db"))
    # 先发布的图只含 N2，边指向 N1，但 N1 当时不存在
    n2 = rec("N2", 1, corrections=[("http://x/n1-r2.html", "http://x/n2-r1.html")])
    s1 = db.publish_snapshot(
        crawl(1, {"N2": n2},
              [edge("N2", "http://x/n2-r1.html", "N1", "http://x/n1-r2.html")]),
        "N2")["snapshot_id"]
    # 之后 N1 才发布（后来的批号不得借用）
    n1_later = rec("N1", 2)
    db.publish_snapshot(crawl(2, {"N1": n1_later}, []), "N1")

    chain = db.get_historical_correction_chain(s1, "N2")
    assert [n["notice_number"] for n in chain["nodes"]] == ["N2"]
    assert len(chain["links"]) == 1
    link = chain["links"][0]
    assert link["broken"] is True
    assert link["broken_reason"] == "target_not_published"
    assert link["target_snapshot_id"] is None
    assert chain["broken_link_count"] == 1
    db.close()


def test_broken_link_when_target_url_not_in_target_sources(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    # 两张图各自独立、边的目标 URL 与目标节点来源不一致
    n1 = rec("N1", 1, urls=["http://x/n1-canonical.html"],
             batches=[("B1", "http://x/n1-canonical.html")])
    s_n1 = db.publish_snapshot(crawl(1, {"N1": n1}, []), "N1")["snapshot_id"]
    n2 = rec("N2", 2, urls=["http://x/n2.html"],
             corrections=[("http://x/n1-other.html", "http://x/n2.html")])
    s_n2 = db.publish_snapshot(
        crawl(2, {"N2": n2},
              [edge("N2", "http://x/n2.html", "N1", "http://x/n1-other.html")]),
        "N2")["snapshot_id"]

    chain = db.get_historical_correction_chain(s_n2, "N2")
    link = _link(chain, "N2", "N1")
    assert link["broken"] is True
    assert link["broken_reason"] == "target_url_not_in_sources"
    # 目标已发布：给出其当时生效快照与时点内批号/来源，但不沿断链继续展开
    assert link["target_snapshot_id"] == s_n1
    node_n1 = _by_number(chain, "N1")
    assert node_n1["snapshot_id"] == s_n1
    assert node_n1["batches"][0]["batch_number"] == "B1"
    assert node_n1["sources"][0]["url"] == "http://x/n1-canonical.html"
    assert chain["broken_link_count"] == 1
    db.close()


def test_valid_link_resolves_target_node_and_batches(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    n1 = rec("N1", 1, urls=["http://x/n1.html"], batches=[("B1", "http://x/n1.html")])
    s_n1 = db.publish_snapshot(crawl(1, {"N1": n1}, []), "N1")["snapshot_id"]
    n2 = rec("N2", 2, urls=["http://x/n2.html"],
             corrections=[("http://x/n1.html", "http://x/n2.html")])
    s_n2 = db.publish_snapshot(
        crawl(2, {"N2": n2},
              [edge("N2", "http://x/n2.html", "N1", "http://x/n1.html")]),
        "N2")["snapshot_id"]

    chain = db.get_historical_correction_chain(s_n2, "N2")
    assert chain["broken_link_count"] == 0
    node_n1 = _by_number(chain, "N1")
    assert node_n1["snapshot_id"] == s_n1
    assert node_n1["depth"] == 1
    assert node_n1["batches"][0]["batch_number"] == "B1"
    db.close()


def test_cycle_listed_once(tmp_path):
    """循环引用：环上通报只列出一次，边完整保留。"""
    db = Database(str(tmp_path / "t.db"))
    ca = rec("CA", 1, urls=["http://x/ca.html"], batches=[("CA-1", "http://x/ca.html")],
             corrections=[("http://x/cb.html", "http://x/ca.html")])
    cb = rec("CB", 1, urls=["http://x/cb.html"], batches=[("CB-1", "http://x/cb.html")],
             corrections=[("http://x/ca.html", "http://x/cb.html")])
    sid = db.publish_snapshot(
        crawl(1, {"CA": ca, "CB": cb},
              [edge("CA", "http://x/ca.html", "CB", "http://x/cb.html"),
               edge("CB", "http://x/cb.html", "CA", "http://x/ca.html")]),
        "CA")["snapshot_id"]

    chain = db.get_historical_correction_chain(sid, "CA")
    numbers = [n["notice_number"] for n in chain["nodes"]]
    assert sorted(numbers) == ["CA", "CB"]
    assert len(numbers) == len(set(numbers))
    assert {(l["from_notice"], l["to_notice"]) for l in chain["links"]} == {
        ("CA", "CB"), ("CB", "CA")}
    assert chain["broken_link_count"] == 0
    db.close()


def test_unknown_snapshot_and_unpublished_notice_return_none(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    n1 = rec("N1", 1)
    sid = db.publish_snapshot(crawl(1, {"N1": n1}, []), "N1")["snapshot_id"]
    assert db.snapshot_exists(sid) is True
    assert db.snapshot_exists(99999) is False
    assert db.get_historical_correction_chain(99999, "N1") is None
    # 快照存在，但被查通报在该时点尚未发布
    assert db.get_historical_correction_chain(sid, "NOT-THERE") is None
    db.close()


def test_history_survives_restart(tmp_path):
    """重启（重新打开同一库）后历史结果一致。"""
    path = str(tmp_path / "t.db")
    db = Database(path)
    n1 = rec("N1", 1)
    n2 = rec("N2", 1, corrections=[("http://x/n1-r1.html", "http://x/n2-r1.html")])
    sid = db.publish_snapshot(
        crawl(1, {"N1": n1, "N2": n2},
              [edge("N2", "http://x/n2-r1.html", "N1", "http://x/n1-r1.html")]),
        "N2")["snapshot_id"]
    before = db.get_historical_correction_chain(sid, "N2")
    db.close()

    db2 = Database(path)
    after = db2.get_historical_correction_chain(sid, "N2")
    assert after == before
    db2.close()


def test_anchor_snapshot_excludes_later_data_even_with_same_notice(tmp_path):
    """锚点之后发布的同通报新版快照号更大，必须被 snapshot_id<=锚点 排除。"""
    db = Database(str(tmp_path / "t.db"))
    n1_v1 = rec("N1", 1, urls=["http://x/n1.html"],
                batches=[("V1-BATCH", "http://x/n1.html")])
    s1 = db.publish_snapshot(crawl(1, {"N1": n1_v1}, []), "N1")["snapshot_id"]
    n1_v2 = rec("N1", 2, urls=["http://x/n1.html"],
                batches=[("V2-BATCH", "http://x/n1.html")])
    s2 = db.publish_snapshot(crawl(2, {"N1": n1_v2}, []), "N1")["snapshot_id"]

    at_s1 = db.get_historical_correction_chain(s1, "N1")
    at_s2 = db.get_historical_correction_chain(s2, "N1")
    assert _by_number(at_s1, "N1")["snapshot_id"] == s1
    assert _by_number(at_s1, "N1")["batches"][0]["batch_number"] == "V1-BATCH"
    assert _by_number(at_s2, "N1")["snapshot_id"] == s2
    assert _by_number(at_s2, "N1")["batches"][0]["batch_number"] == "V2-BATCH"
    db.close()
