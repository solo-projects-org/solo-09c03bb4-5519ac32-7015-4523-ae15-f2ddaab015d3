"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CollectRequest(BaseModel):
    notice_number: str = Field(..., description="通报号，例如 2024-003", examples=["2024-003"])


class SourceView(BaseModel):
    url: str
    title: str | None = None
    fetched_at: str
    content_hash: str


class BatchView(BaseModel):
    batch_number: str
    source_url: str


class EdgeView(BaseModel):
    from_notice: str
    to_notice: str
    source_url: str
    target_url: str


class NoticeView(BaseModel):
    notice_number: str
    latest_snapshot_id: int
    site_revision: int
    content_hash: str
    sources: list[SourceView]
    batches: list[BatchView]
    corrects: list[str]
    corrected_by: list[str]


class AncestorView(BaseModel):
    notice_number: str
    depth: int
    via_url: str | None = None


class CollectResponse(BaseModel):
    run_id: int
    status: str
    snapshot_id: int | None = None
    site_revision: int | None = None
    seed_notice_number: str
    pages_fetched: int
    notices: int
    edges: int
    failure_reason: str | None = None
    conflict_detail: dict[str, Any] | None = None
