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


class HistoricalNodeView(BaseModel):
    notice_number: str
    depth: int = Field(..., description="相对被查通报的更正链深度，被查通报为 0")
    snapshot_id: int = Field(..., description="该时点该通报实际生效的已发布快照号（所属快照）")
    site_revision: int
    content_hash: str
    sources: list[SourceView]
    batches: list[BatchView]


class CorrectionLinkView(BaseModel):
    from_notice: str
    to_notice: str
    source_url: str
    target_url: str
    target_snapshot_id: int | None = Field(
        None, description="目标通报在该时点的生效快照号；断链且目标尚未发布时为 null")
    broken: bool = Field(False, description="是否为断链")
    broken_reason: str | None = Field(
        None, description="断链原因：target_not_published（目标尚未发布）或 "
                          "target_url_not_in_sources（链接 URL 不在目标当时的来源中）")


class HistoricalCorrectionChainView(BaseModel):
    snapshot_id: int = Field(..., description="查询锚点快照号（时点）")
    site_revision: int
    root_notice_number: str
    notice_number: str = Field(..., description="被查通报号")
    nodes: list[HistoricalNodeView]
    links: list[CorrectionLinkView]
    broken_link_count: int


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
