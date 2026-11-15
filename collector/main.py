"""FastAPI 入口：采集、关系/批号查询、运行与快照审计。

Swagger UI：启动后访问 /docs，可直接发起采集与查询。
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from .db import Database
from .fetcher import HttpFetcher
from .models import (AncestorView, CollectRequest, CollectResponse,
                     HistoricalCorrectionChainView, NoticeView)
from .service import CollectorService

DB_PATH = os.environ.get("COLLECTOR_DB", "/data/collector/collector.db")
SITE_BASE_URL = os.environ.get("SITE_BASE_URL", "http://site:8080")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = Database(DB_PATH)
    app.state.fetcher = HttpFetcher(SITE_BASE_URL)
    app.state.service = CollectorService(app.state.db, app.state.fetcher)
    try:
        yield
    finally:
        await app.state.fetcher.aclose()
        app.state.db.close()


app = FastAPI(
    title="药品召回更正公告采集 API",
    version="1.0.0",
    description=(
        "提交通报号后，沿公告中的“更正自”链接递归抓取旧公告，"
        "按通报号合并不同 URL，保存来源 URL、批号与引用边。\n\n"
        "关键语义：\n"
        "- 整次采集必须落在同一站点版号（每页回传 X-Site-Revision）；"
        "跨版、同号异文、链接失效均整次不发布，查询继续返回旧图；\n"
        "- 循环链接按 URL 去重，不重复采集；\n"
        "- 并发采集时晚完成的旧版发布被 CAS 拒绝，不能覆盖新版；\n"
        "- 整张子图单事务原子写入，重启不会读到半张图。\n\n"
        "内置样例：2024-003（更正链 003→002→001）、2024-001（同号镜像合并）、"
        "CYCLE-A/CYCLE-B（循环引用）；站点 v2 制造同号异文冲突，v3 制造链接失效。"
    ),
    lifespan=lifespan,
    openapi_tags=[
        {"name": "采集", "description": "发起整次采集，失败一律不发布"},
        {"name": "查询", "description": "读取最新已发布图中的通报、批号与引用关系"},
        {"name": "历史查询", "description": "按已发布快照号还原该次发布时点的历史更正链视图"},
        {"name": "审计", "description": "运行历史与快照版本历史"},
        {"name": "运维", "description": "健康检查"},
    ],
)


@app.post("/collect", response_model=CollectResponse, tags=["采集"],
          summary="按通报号发起一次采集",
          responses={
              409: {"description": "跨版 / 同号异文 / 链接失效 / 旧版覆盖被拒绝，"
                                    "整次不发布，旧图保留"},
          })
async def collect(req: CollectRequest) -> JSONResponse:
    result = await app.state.service.collect(req.notice_number)
    status = result.pop("http_status")
    return JSONResponse(status_code=status, content=result)


@app.get("/notices/{notice_number}", response_model=NoticeView, tags=["查询"],
         summary="按通报号查询来源、批号与引用关系（读取最新已发布图）")
async def get_notice(notice_number: str) -> dict:
    data = app.state.db.get_notice(notice_number)
    if data is None:
        raise HTTPException(status_code=404,
                            detail=f"已发布图中不存在通报号 {notice_number}，"
                                   "若采集曾失败，请用 GET /runs 查看原因")
    return data


@app.get("/notices/{notice_number}/ancestors",
         response_model=list[AncestorView], tags=["查询"],
         summary="查询某通报号沿“更正自”链接可追溯到的全部旧公告")
async def get_ancestors(notice_number: str) -> list[dict]:
    data = app.state.db.get_ancestors(notice_number)
    if data is None:
        raise HTTPException(status_code=404, detail=f"通报号 {notice_number} 尚未发布")
    return data


@app.get(
    "/snapshots/{snapshot_id}/notices/{notice_number}/correction-chain",
    response_model=HistoricalCorrectionChainView, tags=["历史查询"],
    summary="按已发布快照号和通报号查询历史更正链（还原该次发布时点的公告视图）",
    responses={
        404: {"description": "快照号不存在/未发布，或被查通报在该时点尚未发布"},
    },
    description=(
        "还原指定已发布快照发布后、各入口采集共同形成的公告视图，沿“更正自”关系"
        "返回相关通报的批号、来源及所属快照。\n\n"
        "时点语义：以路径中的快照号为时点，每个通报取该时点之前最近一次发布且"
        "包含它的快照节点——不同入口的图共享通报时取最近发布的节点，未被新图"
        "覆盖的通报沿用原值。\n\n"
        "断链语义：链接所指 URL 不在目标通报当时的来源中，或目标在该时点尚未"
        "发布，均标记 `broken=true` 并给出 `broken_reason`，不展开该链接，也不"
        "借用后来的来源或批号；循环引用中的通报只列出一次。\n\n"
        "历史结果只读取不可变的已发布快照，与新采集交错及服务重启后保持一致；"
        "最新查询（/notices、/graph 等）行为不变。可用 GET /snapshots 取得快照号。"
    ))
async def get_historical_correction_chain(snapshot_id: int,
                                           notice_number: str) -> dict:
    if not app.state.db.snapshot_exists(snapshot_id):
        raise HTTPException(
            status_code=404,
            detail=f"快照号 {snapshot_id} 不存在或未发布，可通过 GET /snapshots 查看")
    data = app.state.db.get_historical_correction_chain(snapshot_id, notice_number)
    if data is None:
        raise HTTPException(
            status_code=404,
            detail=f"快照 {snapshot_id} 发布时点，通报号 {notice_number} 尚未发布，"
                   "无法还原其历史更正链（不得借用后来的来源或批号）")
    return data


@app.get("/graph", tags=["查询"], summary="查看最新已发布的整张子图（通报号节点+引用边）")
async def get_graph() -> dict:
    data = app.state.db.get_graph()
    if data is None:
        raise HTTPException(status_code=404, detail="尚无已发布快照")
    return data


@app.get("/runs", tags=["审计"], summary="采集运行历史（含失败原因与冲突明细）")
async def list_runs(limit: int = 50) -> list[dict]:
    return app.state.db.list_runs(limit)


@app.get("/snapshots", tags=["审计"], summary="快照版本历史（已发布快照不可变；staging 残留会在启动时清理）")
async def list_snapshots() -> list[dict]:
    return app.state.db.list_snapshots()


@app.get("/healthz", tags=["运维"], summary="健康检查")
async def healthz() -> dict:
    return {"status": "ok"}
