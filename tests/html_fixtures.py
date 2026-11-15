"""HTML 构造小工具，生成与真实站点同构的页面。"""
from __future__ import annotations


def index_html(entries: list[tuple[str, str]]) -> str:
    lis = "\n".join(
        f'    <li><a class="index-entry" data-notice-number="{n}" href="{u}">{n}</a></li>'
        for n, u in entries
    )
    return f"""<!doctype html><html><head>
    <meta name="site-revision" content="__R__"></head><body>
    <ul class="notice-index">
{lis}
    </ul></body></html>"""


def notice_html(number: str, title: str, batches: list[str],
                correction: str | None = None,
                extra_body: str = "") -> str:
    batch_lis = "".join(f'<li class="batch">{b}</li>' for b in batches)
    correction_p = (f'<p class="correction">更正自：<a href="{correction}">link</a></p>'
                    if correction else "")
    return f"""<!doctype html><html><head>
    <meta name="site-revision" content="__R__"><title>{title}</title></head><body>
    <article class="notice" data-notice-number="{number}">
      <h1>{title}</h1>
      <p>通报号：<span class="notice-number">{number}</span></p>
      <ul class="batches">{batch_lis}</ul>
      {correction_p}
      <div class="content">正文内容 {number} {extra_body}</div>
    </article></body></html>"""
