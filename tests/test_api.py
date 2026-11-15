"""API 层测试：失败不发布、旧图保留、Swagger 路由可用。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import collector.main as main_mod
from collector.db import Database
from collector.service import CollectorService
from tests.fakes import FakeFetcher, FakePage
from tests.html_fixtures import index_html, notice_html

GOOD = {
    "/index.html": index_html([
        ("N1", "/notice/n1.html"),
        ("N1", "/notice/n1_mirror.html"),
        ("N2", "/notice/n2.html")]),
    "/notice/n1.html": notice_html("N1", "公告1", ["B1"]),
    "/notice/n1_mirror.html": notice_html("N1", "公告1", ["B1"]),
    "/notice/n2.html": notice_html("N2", "公告2", ["B2"], "/notice/n1.html"),
}
BROKEN = dict(GOOD)
del BROKEN["/notice/n1.html"]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLECTOR_DB", str(tmp_path / "api.db"))
    monkeypatch.setenv("SITE_BASE_URL", "http://testlocal")
    import importlib
    importlib.reload(main_mod)
    good = FakeFetcher({1: {p: FakePage(h, 1) for p, h in GOOD.items()}}, 1)
    with TestClient(main_mod.app) as c:
        # lifespan 已建好真实组件，这里替换为内存假抓取器与临时库
        db = Database(str(tmp_path / "api.db"))
        good.pages = {1: {p: FakePage(h, 1) for p, h in GOOD.items()}}
        good.revision = 1
        main_mod.app.state.db = db
        main_mod.app.state.fetcher = good
        main_mod.app.state.service = CollectorService(db, good)
        yield c, good, db
        db.close()


def test_full_publish_flow(client):
    c, _, _ = client
    resp = c.post("/collect", json={"notice_number": "N2"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "published"
    assert body["notices"] == 2

    notice = c.get("/notices/N2").json()
    assert notice["batches"][0]["batch_number"] == "B2"
    assert notice["corrects"] == ["N1"]
    assert c.get("/notices/N2/ancestors").json() == [
        {"notice_number": "N1", "depth": 1,
         "via_url": "http://fake.test/notice/n2.html"}]
    graph = c.get("/graph").json()
    assert {n["notice_number"] for n in graph["notices"]} == {"N1", "N2"}
    assert c.get("/runs").json()[0]["status"] == "published"


def test_failed_collect_keeps_old_graph(client):
    c, fetcher, db = client
    # 第一次成功发布
    assert c.post("/collect", json={"notice_number": "N2"}).status_code == 200
    old_snapshot = c.get("/graph").json()["snapshot_id"]

    # 切换到“链接失效”的站点视图
    fetcher.pages = {1: {p: FakePage(h, 1) for p, h in BROKEN.items()}}
    resp = c.post("/collect", json={"notice_number": "N2"})
    assert resp.status_code == 409
    assert "链接失效" in resp.json()["failure_reason"]
    # 查询仍返回旧图
    assert c.get("/graph").json()["snapshot_id"] == old_snapshot
    run = c.get("/runs").json()[0]
    assert run["status"] == "failed"
    assert run["snapshot_id"] is None


def test_conflict_returns_409_and_detail(client):
    c, _, db = client
    conflict_pages = dict(GOOD)
    conflict_pages["/notice/n1_mirror.html"] = notice_html(
        "N1", "镜像异文公告", ["B1", "B9"], extra_body="完全不同的正文内容！@#")
    conflicting = FakeFetcher(
        {1: {p: FakePage(h, 1) for p, h in conflict_pages.items()}}, 1)
    main_mod.app.state.fetcher = conflicting
    main_mod.app.state.service = CollectorService(db, conflicting)
    resp = c.post("/collect", json={"notice_number": "N2"})
    assert resp.status_code == 409
    assert resp.json()["conflict_detail"]["notice_number"] == "N1"
    assert c.get("/graph").status_code == 404  # 从未成功发布过


def test_unknown_notice_404_on_query(client):
    c, _, _ = client
    assert c.get("/notices/UNKNOWN").status_code == 404


def test_historical_correction_chain_endpoint(client):
    c, _, _ = client
    # GOOD 夹具：N2 更正自 N1，N1 有两个来源 URL（主公告 + 镜像）
    resp = c.post("/collect", json={"notice_number": "N2"})
    assert resp.status_code == 200
    sid = resp.json()["snapshot_id"]

    chain = c.get(f"/snapshots/{sid}/notices/N2/correction-chain")
    assert chain.status_code == 200
    body = chain.json()
    assert body["snapshot_id"] == sid
    assert body["notice_number"] == "N2"
    numbers = [n["notice_number"] for n in body["nodes"]]
    assert numbers == ["N2", "N1"]
    depths = {n["notice_number"]: n["depth"] for n in body["nodes"]}
    assert depths == {"N2": 0, "N1": 1}
    # N1 当时节点带批号、来源与所属快照
    n1 = next(n for n in body["nodes"] if n["notice_number"] == "N1")
    assert n1["snapshot_id"] == sid
    assert n1["batches"][0]["batch_number"] == "B1"
    assert {s["url"] for s in n1["sources"]} == {
        "http://fake.test/notice/n1.html",
        "http://fake.test/notice/n1_mirror.html"}
    assert body["broken_link_count"] == 0
    link = body["links"][0]
    assert (link["from_notice"], link["to_notice"], link["broken"]) == ("N2", "N1", False)
    assert link["target_snapshot_id"] == sid


def test_historical_chain_404_for_bad_snapshot_and_unpublished_notice(client):
    c, _, _ = client
    assert c.post("/collect", json={"notice_number": "N2"}).status_code == 200
    # 不存在的快照号
    resp = c.get("/snapshots/99999/notices/N2/correction-chain")
    assert resp.status_code == 404
    assert "快照" in resp.json()["detail"]
    # 快照存在但被查通报当时尚未发布
    sid = c.get("/snapshots").json()[0]["id"]
    resp = c.get(f"/snapshots/{sid}/notices/NOPE/correction-chain")
    assert resp.status_code == 404
    assert "尚未发布" in resp.json()["detail"]


def test_historical_chain_stable_after_later_collect(client):
    """历史查询与新采集交错：旧快照号的结果不被后来的发布改变。"""
    c, fetcher, db = client
    first = c.post("/collect", json={"notice_number": "N2"})
    sid = first.json()["snapshot_id"]
    before = c.get(f"/snapshots/{sid}/notices/N2/correction-chain").json()

    # 再来一次采集（幂等 noop），并追加一个不相干种子的发布
    c.post("/collect", json={"notice_number": "N2"})
    extra = {
        "/index.html": index_html([("N8", "/notice/n8.html")]),
        "/notice/n8.html": notice_html("N8", "公告8", ["B8"]),
    }
    other = FakeFetcher({1: {p: FakePage(h, 1) for p, h in extra.items()}}, 1)
    main_mod.app.state.fetcher = other
    main_mod.app.state.service = CollectorService(db, other)
    assert c.post("/collect", json={"notice_number": "N8"}).status_code == 200

    after = c.get(f"/snapshots/{sid}/notices/N2/correction-chain").json()
    assert after == before
    # 现有最新查询照常
    assert c.get("/notices/N2").status_code == 200
    assert c.get("/graph").status_code == 200


def test_openapi_contains_examples_and_tags(client):
    c, _, _ = client
    spec = c.get("/openapi.json").json()
    assert "/collect" in spec["paths"]
    schema = spec["components"]["schemas"]["CollectRequest"]
    assert "2024-003" in str(schema)
    declared_tags = {t["name"] for t in spec["tags"]}
    assert {"采集", "查询", "历史查询", "审计", "运维"} <= declared_tags
    assert "/snapshots/{snapshot_id}/notices/{notice_number}/correction-chain" \
        in spec["paths"]
