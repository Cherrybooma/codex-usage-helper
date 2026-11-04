"""Read-only, loopback observability for the Markdown project management vault.

The API reads the same provider as the desktop widget. It never scans Codex logs,
refreshes account limits, mutates notes, or serves arbitrary files.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from markdown_store import DEFAULT_DATA, DEFAULT_VAULT, MarkdownError, management_snapshot

API_VERSION = "1"


def project_id(row):
    return row.get("project_id") or row.get("id")


def overview_data(snapshot):
    tasks = snapshot.get("tasks", [])
    phases = {project_id(p): p.get("phase") for p in snapshot.get("projects", [])}
    current = [t for t in tasks if t.get("status") not in ("已完成", "取消")
               and t.get("phase") and t.get("phase") == phases.get(t.get("project_id"))]
    ordered = ["确认首版目标与范围", "验证关键可行性", "完成核心使用流程", "整体验收", "发布试用并收集反馈"]
    def is_future(task):
        phase, current_phase = task.get("phase"), phases.get(task.get("project_id"))
        return phase in ordered and current_phase in ordered and ordered.index(phase) > ordered.index(current_phase)
    return {"projects": snapshot.get("projects", []),
            "needs_decision": [t for t in tasks if t.get("kind") == "决策" and t.get("managed_by") == "人类确认" and t.get("status") == "待确认"],
            "needs_review": [t for t in tasks if t.get("kind") == "验收" and t.get("managed_by") == "人类确认" and t.get("status") == "待确认"],
            "current_actions": current,
            "future_tasks": [t for t in tasks if t.get("status") not in ("已完成", "取消") and is_future(t)],
            "blocked": [t for t in tasks if t.get("status") == "阻塞"],
            "stale": bool(snapshot.get("stale", False))}


def filter_rows(rows, query):
    wanted = query.get("project_id", [None])[0]
    status = query.get("status", [None])[0]
    kind = query.get("kind", [None])[0]
    result = [row for row in rows if (not wanted or row.get("project_id") == wanted)
              and (not status or row.get("status") == status)
              and (not kind or row.get("kind") == kind)]
    try:
        offset = max(0, int(query.get("offset", [0])[0]))
        limit = min(1000, max(1, int(query.get("limit", [200])[0])))
    except (TypeError, ValueError):
        raise ValueError("limit 和 offset 必须为整数") from None
    return {"items": result[offset:offset + limit], "total": len(result),
            "offset": offset, "limit": limit}


def select_route(snapshot, path, query):
    """Pure routing function, shared by the HTTP server and isolated tests."""
    if path in ("/health", "/api/v1/health"):
        return 200, {"status": "ok", "provider": "markdown",
                     "project_count": len(snapshot.get("projects", [])),
                     "stale": bool(snapshot.get("stale", False)),
                     "source_state": snapshot.get("source_state", "ready"),
                     "data_updated_at": max((p.get("updated") or "" for p in snapshot.get("projects", [])), default="") or None}
    if path == "/api/v1/snapshot":
        return 200, snapshot
    if path == "/api/v1/overview":
        return 200, overview_data(snapshot)
    collections = {"projects": snapshot.get("projects", []),
                   "tasks": snapshot.get("tasks", []),
                   "events": snapshot.get("events", []),
                   "feedback": snapshot.get("feedback", []),
                   "artifacts": snapshot.get("artifacts", [])}
    if path.startswith("/api/v1/"):
        parts = path.removeprefix("/api/v1/").strip("/").split("/")
        name = parts[0]
        if name in collections and len(parts) == 1:
            return 200, filter_rows(collections[name], query)
        if name == "projects" and len(parts) == 2:
            wanted = unquote(parts[1])
            found = [row for row in collections["projects"] if project_id(row) == wanted]
            if found:
                return 200, {"project": found[0],
                             "tasks": [row for row in collections["tasks"] if row.get("project_id") == wanted],
                             "events": [row for row in collections["events"] if row.get("project_id") == wanted],
                             "artifacts": [row for row in collections["artifacts"] if row.get("project_id") == wanted]}
        if name == "usage" and len(parts) == 1:
            return 200, snapshot.get("usage", {})
        if name == "usage" and len(parts) == 2 and parts[1] in ("daily", "periods", "windows"):
            usage = snapshot.get("usage", {})
            return 200, filter_rows(usage.get(parts[1], []), query)
    return 404, {"error": {"code": "not_found", "message": "没有这个只读接口或项目"}}


def make_handler(vault, data_dir):
    class Handler(BaseHTTPRequestHandler):
        server_version = "MarkdownProjectAPI/1"

        def _send(self, status, payload, *, etag=None):
            raw = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Project-API-Version", API_VERSION)
            if etag:
                self.send_header("ETag", etag)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(raw)

        def do_GET(self):
            if len(self.path) > 4096:
                self._send(414, {"error": {"code": "uri_too_long"}})
                return
            host = self.headers.get("Host", "").split(":", 1)[0].lower()
            if host not in ("127.0.0.1", "localhost"):
                self._send(403, {"error": {"code": "host_rejected"}})
                return
            # This listener is for local programs. No wildcard CORS is added.
            # Prevent arbitrary web origins from reading the private local API.
            origin = self.headers.get("Origin")
            if origin:
                try:
                    origin_parts = urlsplit(origin)
                except ValueError:
                    self._send(403, {"error": {"code": "origin_rejected"}})
                    return
                if origin_parts.hostname not in ("127.0.0.1", "localhost", "::1"):
                    self._send(403, {"error": {"code": "origin_rejected"}})
                    return
            try:
                snapshot = management_snapshot(vault, data_dir)
                parsed = urlsplit(self.path)
                status, data = select_route(snapshot, parsed.path.rstrip("/") or "/", parse_qs(parsed.query))
                envelope = {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
                            "provider": "markdown", "vault_id": hashlib.sha256(str(Path(vault).resolve()).encode()).hexdigest()[:16],
                            "data": data}
                stable_data = {key: value for key, value in data.items() if key != "generated_at"} if isinstance(data, dict) else data
                stable = json.dumps(stable_data, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
                etag = '"' + hashlib.sha256(stable).hexdigest() + '"'
                if status == 200 and self.headers.get("If-None-Match") == etag:
                    self.send_response(304)
                    self.send_header("ETag", etag)
                    self.end_headers()
                    return
                self._send(status, envelope, etag=etag)
            except MarkdownError as error:
                self._send(503, {"schema_version": 1, "error": error.public()})
            except ValueError as error:
                self._send(400, {"schema_version": 1, "error": {"code": "invalid_query", "message": str(error)}})
            except Exception as error:
                public = error.public() if hasattr(error, "public") else {"code": "vault_unavailable", "message": "项目资料读取失败，请运行系统检查"}
                self._send(503, {"schema_version": 1, "error": public})

        def do_HEAD(self):
            self.do_GET()

        def _reject_write(self):
            self._send(405, {"error": {"code": "read_only", "message": "本接口只读；请通过管理命令或 Obsidian 维护资料"}})

        do_POST = do_PUT = do_PATCH = do_DELETE = _reject_write

        def log_message(self, format, *args):
            # Keep project names, query contents, and private note text out of logs.
            return

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port 必须在 1–65535 之间")
    management_snapshot(args.vault, args.data_dir)  # Fail clearly before listening.
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(args.vault, args.data_dir))
    print(json.dumps({"ready": True, "url": f"http://127.0.0.1:{args.port}/api/v1/projects", "read_only": True}, ensure_ascii=False), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
