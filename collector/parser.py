"""公告 HTML 解析（仅使用标准库 html.parser）。

约定的页面结构：
- 索引页：<a class="index-entry" data-notice-number="通报号" href="...">
- 公告页：<article class="notice" data-notice-number="...">
  - <span class="notice-number">通报号</span>
  - <li class="batch">批号</li>
  - <p class="correction"><a href="...">更正自链接</a></p>
- 每页：<meta name="site-revision" content="...">
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser


@dataclass
class ParsedPage:
    revision_meta: str | None = None
    notice_number: str | None = None
    title: str | None = None
    batches: list[str] = field(default_factory=list)
    correction_hrefs: list[str] = field(default_factory=list)
    index_entries: list[tuple[str, str]] = field(default_factory=list)
    content_hash: str = ""


class _NoticeHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.result = ParsedPage()
        self._classes: list[set[str]] = []
        self._tag_stack: list[str] = []
        self._text_buf: list[str] = []
        self._title_buf: list[str] = []
        self._in_title = False
        self._pending_anchor: dict | None = None
        self._anchor_text: list[str] = []
        self._in_anchor = False
        self._in_notice = False
        self._meta_revision: str | None = None

    @staticmethod
    def _classes_of(attrs: list[tuple[str, str | None]]) -> set[str]:
        for name, value in attrs:
            if name == "class" and value:
                return set(value.split())
        return set()

    @staticmethod
    def _attr(attrs: list[tuple[str, str | None]], key: str) -> str | None:
        for name, value in attrs:
            if name == key:
                return value
        return None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = self._classes_of(attrs)
        self._tag_stack.append(tag)
        self._classes.append(classes)

        if tag == "title":
            self._in_title = True
        if tag == "meta" and self._attr(attrs, "name") == "site-revision":
            self._meta_revision = self._attr(attrs, "content")
        if tag == "a":
            href = self._attr(attrs, "href")
            if href:
                self._pending_anchor = {"href": href, "classes": classes,
                                        "data_notice": self._attr(attrs, "data-notice-number")}
                self._in_anchor = True
                self._anchor_text = []
        if tag == "article" and "notice" in classes:
            self._in_notice = True
            self.result.notice_number = self._attr(attrs, "data-notice-number")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
            if self._title_buf:
                self.result.title = "".join(self._title_buf).strip()
        if tag == "a" and self._in_anchor and self._pending_anchor is not None:
            info = self._pending_anchor
            text = "".join(self._anchor_text).strip()
            if "index-entry" in info["classes"] and info["data_notice"]:
                self.result.index_entries.append((info["data_notice"].strip(), info["href"]))
            # 更正自链接：位于 .correction 容器内，或锚文本含“更正自/通报”
            in_correction = any("correction" in c for c in self._classes[:-1])
            if in_correction or "更正自" in text:
                self.result.correction_hrefs.append(info["href"])
            self._pending_anchor = None
            self._in_anchor = False
        if tag == "article" and self._in_notice:
            self._in_notice = False
        if self._tag_stack:
            self._tag_stack.pop()
        if self._classes:
            self._classes.pop()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_buf.append(data)
        if self._in_anchor:
            self._anchor_text.append(data)
        if not self._in_notice:
            return
        self._text_buf.append(data)
        classes = self._classes[-1] if self._classes else set()
        tag = self._tag_stack[-1] if self._tag_stack else ""
        if tag == "span" and "notice-number" in classes:
            value = data.strip()
            if value and not self.result.notice_number:
                self.result.notice_number = value
        if tag == "li" and "batch" in classes:
            value = data.strip()
            if value:
                self.result.batches.append(value)

    def close(self) -> None:  # type: ignore[override]
        super().close()
        normalized = re.sub(r"\s+", " ", "".join(self._text_buf)).strip()
        self.result.content_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        self.result.revision_meta = self._meta_revision


def parse_html(html: str) -> ParsedPage:
    parser = _NoticeHTMLParser()
    parser.feed(html)
    parser.close()
    return parser.result
