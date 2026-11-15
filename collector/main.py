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
from .models import AncestorView, CollectRequest, CollectResponse, NoticeView
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
