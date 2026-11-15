"""本地药品召回公告站（仅依赖 Python 标准库）。

特点：
- 内容按版本目录（v1/v2/v3 ...）组织，目录可热插拔；
- 每次切换版本 / reload 都让全站版号（revision）单调递增；
- 每个响应（含 404）都通过 X-Site-Revision 回传当前版号；
- HTML 中的 __REVISION__ 占位符会被替换为当前版号；
- 版号状态持久化到磁盘，重启后继续递增，不回退。
"""
from __future__ import annotations

import json
import os
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

CONTENT_ROOT = Path(os.environ.get("SITE_CONTENT_ROOT", "/srv/site"))
STATE_DIR = Path(os.environ.get("SITE_STATE_DIR", "/data/site"))
STATE_FILE = STATE_DIR / "state.json"
HOST = os.environ.get("SITE_HOST", "0.0.0.0")
PORT = int(os.environ.get("SITE_PORT", "8080"))

_MEDIA_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


class SiteState:
    """线程安全的站点版号状态，持久化到 JSON 文件。"""

    def __init__(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        # RLock：set_version/next_version/reload 持锁时会再调用 snapshot()
        self._lock = threading.RLock()
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            self.revision = int(data["revision"])
            self.version = str(data["version"])
        else:
            self.revision = 0
            self.version = "v1"
            self._bump_locked()

    def _bump_locked(self) -> None:
        self.revision += 1
        self._persist_locked()

    def _persist_locked(self) -> None:
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"revision": self.revision, "version": self.version}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, STATE_FILE)

    def snapshot(self) -> dict:
        with self._lock:
            return {"revision": self.revision, "version": self.version,
                    "available_versions": self.available_versions()}

    def available_versions(self) -> list[str]:
        if not CONTENT_ROOT.exists():
            return []
        return sorted(p.name for p in CONTENT_ROOT.iterdir() if p.is_dir() and p.name.startswith("v"))

    def set_version(self, version: str) -> dict:
        with self._lock:
            target = CONTENT_ROOT / version
            if not target.is_dir():
                raise KeyError(version)
            self.version = version
            self._bump_locked()
            return self.snapshot()

    def next_version(self) -> dict:
        with self._lock:
            versions = self.available_versions()
            if not versions:
                raise RuntimeError("no version directories found")
            try:
                idx = versions.index(self.version)
                nxt = versions[(idx + 1) % len(versions)]
            except ValueError:
                nxt = versions[0]
            self.version = nxt
            self._bump_locked()
            return self.snapshot()

    def reload(self) -> dict:
        """内容目录被外部改动后调用：不切版本，仅递增全站版号。"""
        with self._lock:
            self._bump_locked()
            return self.snapshot()


STATE = SiteState()


class Handler(BaseHTTPRequestHandler):
    server_version = "RecallNoticeSite/1.0"

    # ---- 基础工具 -------------------------------------------------
    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Site-Revision", str(STATE.revision))
        self.end_headers()
        if not getattr(self, "_head_only", False):
            self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # 安静一点
        print("[site] " + fmt % args)

    # ---- 管理 API -------------------------------------------------
    def do_HEAD(self) -> None:  # noqa: N802
        # HEAD 与 GET 走同一套版号/路径逻辑，仅不回响应体
        self._head_only = True
        try:
            self.do_GET()
        finally:
            self._head_only = False

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/admin/state":
            self._send_json(HTTPStatus.OK, STATE.snapshot())
            return
        if path == "/healthz":
            self._send_json(HTTPStatus.OK, {"status": "ok"})
            return
        self._serve_static(path)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid json body"})
            return

        if path == "/admin/version":
            version = str(payload.get("version", "")).strip()
            if not version:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "missing 'version'"})
                return
            try:
                self._send_json(HTTPStatus.OK, STATE.set_version(version))
            except KeyError:
                self._send_json(HTTPStatus.NOT_FOUND,
                                {"error": f"version directory not found: {version}"})
            return
        if path == "/admin/next":
            try:
                self._send_json(HTTPStatus.OK, STATE.next_version())
            except RuntimeError as exc:
                self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
            return
        if path == "/admin/reload":
            self._send_json(HTTPStatus.OK, STATE.reload())
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": f"unknown admin path: {path}"})

    # ---- 静态内容 -------------------------------------------------
    def _serve_static(self, path: str) -> None:
        revision = STATE.revision
        if path == "/":
            path = "/index.html"
        # 防目录穿越：resolve 后必须仍在版本目录内
        base = (CONTENT_ROOT / STATE.version).resolve()
        target = (base / path.lstrip("/")).resolve()
        if base not in target.parents and target != base:
            self._send_text(HTTPStatus.FORBIDDEN, "forbidden", revision)
            return
        if not target.is_file():
            self._send_text(
                HTTPStatus.NOT_FOUND,
                f"页面不存在：{path}（站点版号 {revision}，版本目录 {STATE.version}）",
                revision,
            )
            return
        media = _MEDIA_TYPES.get(target.suffix, "application/octet-stream")
        data = target.read_bytes()
        if target.suffix in (".html", ".htm"):
            data = data.replace(b"__REVISION__", str(revision).encode("ascii"))
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", media)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Site-Revision", str(revision))
        self.end_headers()
        if not getattr(self, "_head_only", False):
            self.wfile.write(data)

    def _send_text(self, status: int, text: str, revision: int) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Site-Revision", str(revision))
        self.end_headers()
        if not getattr(self, "_head_only", False):
            self.wfile.write(body)


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[site] serving {CONTENT_ROOT} versions={STATE.available_versions()} "
          f"current={STATE.version} revision={STATE.revision} on {HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
