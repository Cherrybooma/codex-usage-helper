"""Opt-in Notion project metadata and absolute daily Codex summaries.

Only calculated, whitelisted aggregates leave the computer. Credentials come
from a selected Pages file or this app's dedicated Keychain item, never Codex
auth or environment vars.
The transport and credential provider are injectable for network-free tests.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import random
import re
import subprocess
import sys
import time
from typing import Callable
from urllib import error, parse, request
import uuid
from zoneinfo import ZoneInfo

if __package__:
    from .notion_credentials import configured_token
else:
    from notion_credentials import configured_token

API_VERSION = "2026-03-11"
API_BASE = "https://api.notion.com/v1"
KEYCHAIN_SERVICE = "local.codex.usage-widget.notion"
KEYCHAIN_ACCOUNT = "integration"
SHANGHAI = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc
COUNTERS = ("total_tokens", "input_tokens", "output_tokens", "cached_input_tokens", "cache_write_input_tokens",
            "reasoning_output_tokens", "request_records", "legacy_updates")
NUMBER_PROPERTIES = dict(zip(COUNTERS, (
    "Total Tokens", "Input Tokens", "Output Tokens", "Cached Input Tokens", "Cache Write Input Tokens",
    "Reasoning Output Tokens", "Response Records", "Legacy Updates")))
QUALITY = {"response_records_verified": "按响应记录", "legacy_estimated": "含旧累计估算",
           "partial_unreadable": "部分记录未读取", "unknown": "未核实"}
MESSAGES = {
    "not_connected": "尚未连接 Notion；本地统计继续可用。",
    "not_configured": "请先选择父页面并建立项目库和每日用量库。",
    "unauthorized": "Notion 凭据无效，请重新连接。",
    "restricted_resource": "Notion 页面未授权或已达到工作区限制。",
    "object_not_found": "Notion 页面不存在或未向连接共享。",
    "rate_limited": "Notion 请求受限，已保留待同步摘要。",
    "service_overload": "Notion 暂时繁忙，已保留待同步摘要。",
    "api_access_blocked": "Notion 已限制此连接，自动重试已停止。",
    "network_error": "Notion 网络不可用，已保留本地缓存与待同步摘要。",
    "write_uncertain": "写入结果尚未确认，先重新查询，避免创建副本。",
    "duplicate_summary": "Notion 已存在重复摘要，暂停写入以免覆盖错误记录。",
    "invalid_input": "输入字段或统计时间无效。",
    "schema_mismatch": "Notion 数据库结构与本版本不兼容。",
    "invalid_response": "Notion 返回的数据无法识别。",
    "api_error": "Notion 请求失败，已保留本地数据。",
}


class NotionError(Exception):
    """Safe to show; never exposes server messages, URLs or request bodies."""
    def __init__(self, code: str, *, status: int = 0, retry_after: int = 0,
                 uncertain: bool = False, committed_id: str | None = None):
        self.code = code if code in MESSAGES else "api_error"
        self.status = status
        self.retry_after = retry_after
        self.uncertain = uncertain
        self.committed_id = committed_id
        super().__init__(MESSAGES[self.code])

    def public(self) -> dict:
        return {"code": self.code, "message": MESSAGES[self.code],
                "http_status": self.status, "retry_after": self.retry_after}


def keychain_token() -> str | None:
    """Read exactly this application's integration item; suppress all output."""
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
             "-a", KEYCHAIN_ACCOUNT, "-w"], stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=30, check=False)
        if result.returncode == 0:
            value = result.stdout.decode("utf-8").strip()
            return value or None
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        pass
    return None


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def urllib_transport(method: str, url: str, headers: dict, payload: dict | None):
    """One request. Redirects cannot send Authorization to a different host."""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = request.Request(url, data=data, headers=headers, method=method)
    opener = request.build_opener(_NoRedirect())
    try:
        with opener.open(req, timeout=20) as response:
            body = response.read(4 * 1024 * 1024)
            return response.status, dict(response.headers), json.loads(body)
    except error.HTTPError as exc:
        try:
            body = json.loads(exc.read(1024 * 1024))
        except (ValueError, OSError):
            body = {}
        return exc.code, dict(exc.headers), body


def notion_id(value: str) -> str:
    """Accept a page UUID or a user-pasted Notion URL, not arbitrary URLs."""
    if not isinstance(value, str):
        raise NotionError("invalid_input")
    value = value.strip()
    if "://" in value:
        url = parse.urlparse(value)
        if url.scheme != "https" or url.hostname not in {
                "notion.so", "www.notion.so", "notion.com", "www.notion.com", "app.notion.com"}:
            raise NotionError("invalid_input")
        value = url.path.rstrip("/").split("/")[-1]
        match = re.search(r"([0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})$", value)
        if not match:
            raise NotionError("invalid_input")
        value = match.group(1)
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError):
        raise NotionError("invalid_input") from None


def _utc(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
        if result.tzinfo is None:
            raise ValueError()
        return result.astimezone(UTC)
    except (ValueError, AttributeError, TypeError):
        raise NotionError("invalid_input") from None


def _text(value: str) -> list[dict]:
    return [{"type": "text", "text": {"content": value}}]


def _plain(value) -> str:
    if not isinstance(value, list):
        return ""
    return "".join(item.get("plain_text", item.get("text", {}).get("content", ""))
                   for item in value if isinstance(item, dict))[:2000]


def _safe_https(value) -> str | None:
    if not isinstance(value, str) or len(value) > 4096:
        return None
    url = parse.urlparse(value)
    return value if url.scheme == "https" and url.hostname and not url.username and not url.password else None


class NotionWorkspace:
    """An opt-in, serialized local outbox. All writes replace absolute totals."""
    def __init__(self, data_dir: Path | str, token_provider: Callable | None = None,
                 transport: Callable | None = None, clock: Callable | None = None,
                 sleeper: Callable | None = None):
        self.data_dir = Path(data_dir)
        self.token_provider = token_provider if token_provider is not None else lambda: configured_token(self.data_dir)
        self.transport = transport or urllib_transport
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleeper = sleeper or time.sleep
        self.config_path = self.data_dir / "notion-config.json"
        self.state_path = self.data_dir / "notion-state.json"
        self.config: dict = {}
        self.state: dict = {}
        self._active_token: str | None = None
        if self.config_path.exists() and self.state_path.exists():
            self.config = self._load(self.config_path, {})
            self.state = self._load(self.state_path, {})
        else:
            with self._locked():
                pass

    def _now(self, value=None):
        return _utc(value if value is not None else self.clock())

    def _load(self, path: Path, fallback: dict):
        if not path.exists():
            return fallback
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(result, dict):
                raise ValueError()
            if path == self.config_path:
                if result.get("version") != 1 or not isinstance(result.get("bindings"), dict):
                    raise ValueError()
                notion_id(result.get("dataset_id"))
            if path == self.state_path:
                if result.get("version") != 1 or not all(isinstance(result.get(key), dict)
                        for key in ("projects", "pending", "synced", "uncertain_creates")):
                    raise ValueError()
                if not all(isinstance(meta, dict) for meta in result["projects"].values()):
                    raise ValueError()
            return result
        except (ValueError, UnicodeError):
            raise NotionError("invalid_response") from None

    def _save(self, path: Path, value: dict):
        temporary = path.with_name(path.name + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, sort_keys=True, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)

    @contextmanager
    def _locked(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with (self.data_dir / "notion.lock").open("a+") as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            loaded = False
            try:
                self.config = self._load(self.config_path, {
                    "version": 1, "dataset_id": str(uuid.uuid4()), "bindings": {}})
                self.state = self._load(self.state_path, {
                    "version": 1, "projects": {}, "pending": {}, "synced": {},
                    "uncertain_creates": {}})
                self._active_token = None
                loaded = True
                yield
            finally:
                if loaded and self.config and self.state:
                    self._save(self.config_path, self.config)
                    self._save(self.state_path, self.state)
                self._active_token = None
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _token(self):
        if self._active_token is not None:
            return self._active_token
        try:
            result = self.token_provider()
        except Exception:
            result = None
        if not isinstance(result, str) or not result.strip() or "\n" in result or "\r" in result:
            self.state["credential_available"] = False
            raise NotionError("not_connected")
        self.state["credential_available"] = True
        self._active_token = result.strip()
        return self._active_token

    def _remember_error(self, exc: NotionError):
        self.state["error"] = exc.public()
        if exc.code in ("not_connected", "unauthorized"):
            self.state["credential_available"] = False
        if exc.code == "api_access_blocked":
            self.state["automatic_sync_paused"] = True
        if exc.retry_after:
            self.state["next_retry"] = (self._now() + timedelta(seconds=exc.retry_after)).isoformat()

    def _ready(self):
        return bool(self.config.get("projects_data_source_id") and self.config.get("usage_data_source_id"))

    def _request(self, method: str, path: str, payload=None, *, idempotent=False):
        token = self._token()
        if self.state.get("next_retry") and self._now() < _utc(self.state["next_retry"]):
            remaining = max(1, int((_utc(self.state["next_retry"]) - self._now()).total_seconds()))
            raise NotionError("rate_limited", retry_after=remaining)
        for attempt in range(3):
            try:
                status, headers, body = self.transport(method, API_BASE + path, {
                    "Authorization": "Bearer " + token, "Notion-Version": API_VERSION,
                    "Content-Type": "application/json"}, payload)
                if not isinstance(body, dict):
                    raise ValueError()
            except Exception:
                if idempotent and attempt < 2:
                    self.sleeper(2 ** attempt)
                    continue
                raise NotionError("network_error", uncertain=not idempotent) from None
            if 200 <= status < 300:
                self.state.pop("next_retry", None)
                return body
            extra = body.get("additional_data") or {}
            reason = extra.get("rate_limit_reason") if isinstance(extra, dict) else None
            if status == 429 and reason == "public_api_request_blocked":
                raise NotionError("api_access_blocked", status=status)
            if status in (429, 529):
                raw_wait = next((v for k, v in headers.items() if k.lower() == "retry-after"),
                                extra.get("retry_after") if isinstance(extra, dict) else None)
                try:
                    delay = max(0, int(raw_wait)) if raw_wait is not None else 2 ** attempt
                except (ValueError, TypeError):
                    delay = 2 ** attempt
                delay = max(delay, 2 ** attempt if attempt else 0)
                code = "rate_limited" if status == 429 else "service_overload"
                if attempt < 2 and delay <= 30:
                    self.sleeper(delay + random.uniform(0, .1))
                    continue
                raise NotionError(code, status=status, retry_after=max(1, delay))
            if idempotent and status in (500, 502, 503, 504) and attempt < 2:
                self.sleeper(2 ** attempt)
                continue
            committed = None
            if isinstance(extra, dict) and extra.get("committed_resource_id"):
                try:
                    committed = notion_id(extra["committed_resource_id"])
                except NotionError:
                    pass
            code = {401: "unauthorized", 403: "restricted_resource", 404: "object_not_found"}.get(status, "api_error")
            raise NotionError(code, status=status,
                              uncertain=not idempotent and status in (500, 502, 503, 504),
                              committed_id=committed)

    def _query(self, data_source_id, *, filter=None):
        result, cursor, seen = [], None, set()
        for _ in range(10000):
            body = {"page_size": 100}
            if filter:
                body["filter"] = filter
            if cursor:
                body["start_cursor"] = cursor
            page = self._request("POST", f"/data_sources/{notion_id(data_source_id)}/query", body,
                                 idempotent=True)
            if not isinstance(page.get("results"), list):
                raise NotionError("invalid_response")
            result.extend(page["results"])
            if not page.get("has_more"):
                return result
            cursor = page.get("next_cursor")
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise NotionError("invalid_response")
            seen.add(cursor)
        raise NotionError("invalid_response")

    def _db_schema(self, kind):
        if kind == "projects":
            statuses = [("计划中", "gray", "To-do"), ("开发中", "blue", "In progress"),
                        ("已发布", "green", "Complete"), ("暂停", "yellow", "To-do"),
                        ("归档", "gray", "Complete")]
            return {"Name": {"title": {}}, "Status": {"status": {"options": [
                {"name": name, "color": color, "group": group} for name, color, group in statuses]}},
                "Weekly Goal": {"rich_text": {}}, "Next Step": {"rich_text": {}},
                "Outcome": {"url": {}}, "Binding Key": {"rich_text": {}}}
        result = {"Name": {"title": {}}, "Project": {"relation": {
            "data_source_id": self.config["projects_data_source_id"],
            "type": "single_property", "single_property": {}}},
            "Day": {"date": {}}, "Provider": {"select": {"options": [{"name": "Codex"}]}},
            "Summary Key": {"rich_text": {}}, "Dataset": {"rich_text": {}},
            "Quality": {"rich_text": {}}, "Statistics Until": {"date": {}},
            "Statistics Until ISO": {"rich_text": {}},
            "Data Scope": {"rich_text": {}},
            "Included": {"checkbox": {}}, "Counted Tokens": {"number": {}},
            "Synced At": {"date": {}}, "Schema Version": {"number": {}},
            "Sessions": {"number": {}}}
        result.update({name: {"number": {}} for name in NUMBER_PROPERTIES.values()})
        return result

    def _find_database(self, parent, marker):
        cursor, seen = None, set()
        while True:
            suffix = "?page_size=100" + ("&start_cursor=" + parse.quote(cursor) if cursor else "")
            listing = self._request("GET", f"/blocks/{parent}/children{suffix}", idempotent=True)
            for block in listing.get("results", []):
                if block.get("type") == "child_database":
                    database = self._request("GET", f"/databases/{notion_id(block['id'])}", idempotent=True)
                    if _plain(database.get("description")) == marker:
                        return database
            if not listing.get("has_more"):
                return None
            cursor = listing.get("next_cursor")
            if not cursor or cursor in seen:
                raise NotionError("invalid_response")
            seen.add(cursor)

    def _ensure_database(self, kind, parent):
        if self.config.get(kind + "_database_id"):
            database = self._request("GET", f"/databases/{notion_id(self.config[kind + '_database_id'])}",
                                     idempotent=True)
        else:
            marker = f"codex-usage-helper:{self.config['dataset_id']}:{kind}:v1"
            database = self._find_database(parent, marker)
            operation = "database:" + kind
            if not database and operation in self.state["uncertain_creates"]:
                raise NotionError("write_uncertain")
            if not database:
                self.state["uncertain_creates"][operation] = {"kind": kind}
                self._save(self.state_path, self.state)  # Outbox before the request.
                try:
                    database = self._request("POST", "/databases", {
                        "parent": {"type": "page_id", "page_id": parent},
                        "title": _text("项目 Projects" if kind == "projects" else "每日用量 Usage Daily"),
                        "description": _text(marker), "is_inline": True,
                        "initial_data_source": {"properties": self._db_schema(kind)}})
                except NotionError as exc:
                    if exc.committed_id:
                        database = self._request("GET", f"/databases/{exc.committed_id}", idempotent=True)
                    elif exc.uncertain:
                        database = self._find_database(parent, marker)
                        if not database:
                            raise NotionError("write_uncertain") from None
                    else:
                        self.state["uncertain_creates"].pop(operation, None)
                        raise
            self.state["uncertain_creates"].pop(operation, None)
        sources = database.get("data_sources")
        if not isinstance(sources, list) or len(sources) != 1:
            raise NotionError("schema_mismatch")
        self.config[kind + "_database_id"] = notion_id(database.get("id"))
        self.config[kind + "_data_source_id"] = notion_id(sources[0].get("id"))
        self.config[kind + "_url"] = _safe_https(database.get("url"))
        source = self._request("GET", f"/data_sources/{self.config[kind + '_data_source_id']}", idempotent=True)
        properties = source.get("properties", {})
        expected = self._db_schema(kind)
        for name, definition in expected.items():
            actual = properties.get(name, {})
            actual_type = actual.get("type") or next((key for key in definition if key in actual), None)
            required_type = next(iter(definition))
            if kind == "projects" and name == "Status" and actual_type == "select":
                self.config["status_property_type"] = "select"
            elif actual_type != required_type:
                raise NotionError("schema_mismatch")
            elif kind == "projects" and name == "Status":
                self.config["status_property_type"] = "status"
            if name == "Project" and actual.get("relation", {}).get("data_source_id") != self.config["projects_data_source_id"]:
                raise NotionError("schema_mismatch")
        self._save(self.config_path, self.config)

    def setup(self, parent_page: str, now=None) -> dict:
        with self._locked():
            try:
                parent = notion_id(parent_page)
                if self.config.get("parent_page_id") not in (None, parent):
                    raise NotionError("invalid_input")
                self._token()
                self._request("GET", f"/pages/{parent}", idempotent=True)
                self.config["parent_page_id"] = parent
                today = self._now(now).astimezone(SHANGHAI).date()
                self.config.setdefault("sync_since", (today - timedelta(days=today.weekday())).isoformat())
                self._ensure_database("projects", parent)
                self._ensure_database("usage", parent)
                self.state.pop("error", None)
                self.state.pop("automatic_sync_paused", None)
            except NotionError as exc:
                self._remember_error(exc)
                raise
            return self._status()

    def _project(self, page, *, stale=False):
        properties = page.get("properties") or {}
        title = next((prop for prop in properties.values() if prop.get("type") == "title" or "title" in prop), {})
        status = properties.get("Status") or next(
            (prop for prop in properties.values() if prop.get("type") == "status"), {})
        status_value = status.get("status") or status.get("select") or {}
        icon = page.get("icon") or {}
        icon_type, icon_value = icon.get("type"), None
        if icon_type == "emoji":
            icon_value = str(icon.get("emoji", ""))[:32]
        elif icon_type in ("external", "file"):
            icon_value = _safe_https((icon.get(icon_type) or {}).get("url"))
        elif icon_type == "custom_emoji":
            icon_value = _safe_https((icon.get("custom_emoji") or {}).get("url"))
        return {"page_id": notion_id(page.get("id")), "display_name": _plain(title.get("title")) or "未命名项目",
                "status": str(status_value.get("name", ""))[:100],
                "icon_type": icon_type if icon_value else None, "icon_value": icon_value,
                "url": _safe_https(page.get("url")), "updated_at": page.get("last_edited_time"),
                "fetched_at": self._now().isoformat(), "stale": stale,
                "available": not (page.get("archived") or page.get("in_trash"))}

    def _projects(self, force=False):
        cached = self.state["projects"]
        fetched = self.state.get("projects_fetched_at")
        due = force or not fetched or (self._now() - _utc(fetched)).total_seconds() >= 300
        if self._ready() and due and (force or not self.state.get("automatic_sync_paused")):
            try:
                pages = self._query(self.config["projects_data_source_id"])
                fresh = {meta["page_id"]: meta for page in pages if not (page.get("archived") or page.get("in_trash"))
                         for meta in [self._project(page)]}
                for old_id, old in cached.items():
                    if old_id not in fresh:
                        fresh[old_id] = {**old, "available": False, "stale": True}
                self.state["projects"] = cached = fresh
                self.state["projects_fetched_at"] = self._now().isoformat()
                self.state.pop("projects_error", None)
                self.state.pop("automatic_sync_paused", None)
            except NotionError as exc:
                self.state["projects_error"] = exc.public()
                self._remember_error(exc)
        fetched = self.state.get("projects_fetched_at")
        stale = not fetched or (self._now() - _utc(fetched)).total_seconds() >= 600
        return [{**meta, "stale": meta.get("stale", False) or stale or bool(self.state.get("projects_error"))}
                for meta in sorted(cached.values(), key=lambda item: (item["display_name"], item["page_id"]))]

    def projects(self, force=False) -> list[dict]:
        with self._locked():
            return self._projects(force)

    def _verify_project(self, page_id):
        page = self._request("GET", f"/pages/{notion_id(page_id)}", idempotent=True)
        if page.get("archived") or page.get("in_trash"):
            raise NotionError("object_not_found")
        if (page.get("parent") or {}).get("data_source_id") != self.config.get("projects_data_source_id"):
            raise NotionError("schema_mismatch")
        return self._project(page)

    def _task_id(self, task_id):
        if not isinstance(task_id, str) or not task_id or len(task_id) > 512:
            raise NotionError("invalid_input")
        return task_id

    def bind(self, task_id: str, page_id: str) -> dict:
        with self._locked():
            try:
                if not self._ready():
                    raise NotionError("not_configured")
                task_id = self._task_id(task_id)
                meta = self._verify_project(page_id)
                self.config["bindings"][task_id] = meta["page_id"]
                self.state["projects"][meta["page_id"]] = meta
                self.state.pop("error", None)
                return meta
            except NotionError as exc:
                self._remember_error(exc)
                raise

    def unbind(self, task_id: str):
        with self._locked():
            self.config["bindings"].pop(self._task_id(task_id), None)
            # The cloud history is not deleted. Pending writes for this local
            # task are removed by the next full sync's binding validation.
            return self._status()

    def create_project(self, task_id: str, name: str) -> dict:
        with self._locked():
            try:
                if not self._ready():
                    raise NotionError("not_configured")
                task_id = self._task_id(task_id)
                if not isinstance(name, str) or not name.strip() or len(name) > 200:
                    raise NotionError("invalid_input")
                if task_id in self.config["bindings"]:
                    return self._verify_project(self.config["bindings"][task_id])
                key = hashlib.sha256((self.config["dataset_id"] + ":" + task_id).encode()).hexdigest()
                matches = self._query(self.config["projects_data_source_id"], filter={
                    "property": "Binding Key", "rich_text": {"equals": key}})
                if len(matches) > 1:
                    raise NotionError("duplicate_summary")
                operation = "project:" + key
                if not matches and operation in self.state["uncertain_creates"]:
                    raise NotionError("write_uncertain")
                if matches:
                    page = matches[0]
                else:
                    self.state["uncertain_creates"][operation] = {"kind": "project"}
                    self._save(self.state_path, self.state)
                    try:
                        page = self._request("POST", "/pages", {
                            "parent": {"type": "data_source_id", "data_source_id": self.config["projects_data_source_id"]},
                            "properties": {"Name": {"title": _text(name.strip())},
                                           "Status": {"select" if self.config.get("status_property_type") == "select" else "status": {"name": "计划中"}},
                                           "Binding Key": {"rich_text": _text(key)}}})
                    except NotionError as exc:
                        if exc.committed_id:
                            page = self._request("GET", f"/pages/{exc.committed_id}", idempotent=True)
                        elif exc.uncertain:
                            found = self._query(self.config["projects_data_source_id"], filter={
                                "property": "Binding Key", "rich_text": {"equals": key}})
                            if len(found) != 1:
                                raise NotionError("write_uncertain") from None
                            page = found[0]
                        else:
                            self.state["uncertain_creates"].pop(operation, None)
                            raise
                meta = self._project(page)
                self.state["uncertain_creates"].pop(operation, None)
                self.state["projects"][meta["page_id"]] = meta
                self.config["bindings"][task_id] = meta["page_id"]
                self.state.pop("error", None)
                return meta
            except NotionError as exc:
                self._remember_error(exc)
                raise

    def _normalize_rows(self, rows, now):
        if not isinstance(rows, list):
            raise NotionError("invalid_input")
        result, seen = [], set()
        for raw in rows:
            if not isinstance(raw, dict):
                raise NotionError("invalid_input")
            task = self._task_id(raw.get("task_id"))
            if str(raw.get("provider", "codex")).lower() != "codex":
                raise NotionError("invalid_input")
            try:
                day = date.fromisoformat(raw.get("day", ""))
            except (ValueError, TypeError):
                raise NotionError("invalid_input") from None
            if (task, day) in seen:
                raise NotionError("invalid_input")
            seen.add((task, day))
            if day.isoformat() < self.config.get("sync_since", "9999-12-31"):
                continue
            if day > now.astimezone(SHANGHAI).date():
                raise NotionError("invalid_input")
            project = self.config["bindings"].get(task)
            if not project:
                continue
            dataset = notion_id(raw.get("dataset_id", self.config["dataset_id"]))
            row = {"project_id": project, "task_ids": [task], "day": day.isoformat(),
                   "provider": "Codex", "dataset_id": dataset,
                   "observed_history": bool(raw.get("observed_history", False))}
            for counter in (*COUNTERS, "sessions_count"):
                value = raw.get(counter, 0)
                if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2 ** 53:
                    raise NotionError("invalid_input")
                row[counter] = value
            until = self._now(raw.get("statistics_until") or now)
            day_start = datetime.combine(day, datetime.min.time(), tzinfo=SHANGHAI).astimezone(UTC)
            if until > now + timedelta(seconds=1) or until < day_start:
                raise NotionError("invalid_input")
            row["statistics_until"] = until.isoformat()
            quality = raw.get("quality", "unknown")
            quality = quality if isinstance(quality, list) else [quality]
            row["quality"] = sorted({q if isinstance(q, str) and q in QUALITY else "unknown" for q in quality}) or ["unknown"]
            # Session keys are optional transient evidence used only for local
            # unions, discarded before persistence and never sent to Notion.
            session_keys = raw.get("session_keys")
            row["_sessions"] = set(session_keys) if isinstance(session_keys, list) and all(
                isinstance(value, str) for value in session_keys) else None
            result.append(row)
        groups = defaultdict(list)
        for row in result:
            groups[(row["dataset_id"], row["project_id"], row["day"])].append(row)
        merged = []
        for items in groups.values():
            untils = {row["statistics_until"] for row in items}
            if len(untils) != 1 and not any(item["observed_history"] for item in items):
                raise NotionError("invalid_input")
            row = {"project_id": items[0]["project_id"], "day": items[0]["day"], "provider": "Codex",
                   "dataset_id": items[0]["dataset_id"],
                   "observed_history": any(item["observed_history"] for item in items),
                   "task_ids": sorted({task for item in items for task in item["task_ids"]}),
                   "statistics_until": max(untils, key=_utc),
                   "quality": sorted({q for item in items for q in item["quality"]})}
            for counter in COUNTERS:
                row[counter] = sum(item[counter] for item in items)
                if row[counter] > 2 ** 53:
                    raise NotionError("invalid_input")
            if all(item["_sessions"] is not None for item in items):
                row["sessions_count"] = len(set().union(*(item["_sessions"] for item in items)))
            elif len(items) == 1:
                row["sessions_count"] = items[0]["sessions_count"]
            else:
                row["sessions_count"] = None
                row["quality"].append("sessions_union_unverified")
            merged.append(row)
        return merged

    def _summary_key(self, row):
        values = [row["dataset_id"], row["project_id"], row["day"], row["provider"]]
        return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()

    def _fingerprint(self, row):
        # A new scan cutoff alone does not need a cloud write when counts and
        # quality have not changed. Notion retains the last transmitted cutoff.
        fields = {k: v for k, v in row.items() if k not in ("task_ids", "statistics_until")}
        return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()

    def _summary_properties(self, row, key, now):
        quality = "；".join(QUALITY.get(q, "会话数未跨任务去重") for q in row["quality"])
        properties = {
            "Name": {"title": _text(row["day"] + " · Codex")},
            "Project": {"relation": [{"id": row["project_id"]}]},
            "Day": {"date": {"start": row["day"]}}, "Provider": {"select": {"name": "Codex"}},
            "Summary Key": {"rich_text": _text(key)}, "Dataset": {"rich_text": _text(row["dataset_id"])},
            "Quality": {"rich_text": _text(quality)}, "Statistics Until": {"date": {"start": row["statistics_until"]}},
            "Statistics Until ISO": {"rich_text": _text(row["statistics_until"])},
            "Data Scope": {"rich_text": _text(
                "包含此前已观测摘要；部分历史本次未重新复算，不是当前保留记录的全量统计" if row["observed_history"]
                else "本机当前保留记录；不代表完整账户用量或最终账单")},
            "Synced At": {"date": {"start": now.isoformat()}}, "Schema Version": {"number": 1},
            "Included": {"checkbox": True}, "Counted Tokens": {"number": row["total_tokens"]},
            "Sessions": {"number": row["sessions_count"]}}
        properties.update({name: {"number": row[counter]} for counter, name in NUMBER_PROPERTIES.items()})
        return properties

    def _upsert(self, key, row, now):
        found = self._query(self.config["usage_data_source_id"], filter={
            "property": "Summary Key", "rich_text": {"equals": key}})
        if len(found) > 1:
            raise NotionError("duplicate_summary")
        properties = self._summary_properties(row, key, now)
        if found:
            page_id = notion_id(found[0].get("id"))
            # Absolute replacement is safe even if an update response is lost.
            self._request("PATCH", f"/pages/{page_id}", {"properties": properties}, idempotent=True)
        else:
            if key in self.state["uncertain_creates"]:
                raise NotionError("write_uncertain")
            self.state["uncertain_creates"][key] = {"kind": "summary"}
            self._save(self.state_path, self.state)
            try:
                page = self._request("POST", "/pages", {
                    "parent": {"type": "data_source_id", "data_source_id": self.config["usage_data_source_id"]},
                    "properties": properties})
                page_id = notion_id(page.get("id"))
            except NotionError as exc:
                if exc.committed_id:
                    page = self._request("GET", f"/pages/{exc.committed_id}", idempotent=True)
                    # A committed ID must still point to this exact summary.
                    if _plain((page.get("properties", {}).get("Summary Key") or {}).get("rich_text")) != key:
                        raise NotionError("write_uncertain") from None
                    page_id = notion_id(page.get("id"))
                elif exc.uncertain:
                    found = self._query(self.config["usage_data_source_id"], filter={
                        "property": "Summary Key", "rich_text": {"equals": key}})
                    if len(found) != 1:
                        raise NotionError("write_uncertain") from None
                    page_id = notion_id(found[0].get("id"))
                else:
                    self.state["uncertain_creates"].pop(key, None)
                    raise
        self.state["uncertain_creates"].pop(key, None)
        self.state["synced"][key] = {"page_id": page_id, "fingerprint": self._fingerprint(row),
                                     "statistics_until": row["statistics_until"], "day": row["day"],
                                     "dataset_id": row["dataset_id"]}
        self.state["pending"].pop(key, None)
        self._save(self.state_path, self.state)

    def sync(self, rows: list[dict], now=None, force=False, complete_dataset_ids=None, complete_until=None) -> dict:
        with self._locked():
            now = self._now(now)
            changed = 0
            try:
                normalized = self._normalize_rows(rows, now)
                if complete_dataset_ids is not None and not isinstance(complete_dataset_ids, list):
                    raise NotionError("invalid_input")
                complete = {notion_id(value) for value in (complete_dataset_ids or [])}
                cutoff = self._now(complete_until) if complete_until is not None else max(
                    (self._now(row.get("statistics_until") or now) for row in rows), default=now)
                if cutoff > now:
                    raise NotionError("invalid_input")
                clocks = self.state.setdefault("complete_cutoffs", {})
                stale = {dataset for dataset in complete if clocks.get(dataset) and cutoff < _utc(clocks[dataset])}
                complete -= stale
                normalized = [row for row in normalized if row["dataset_id"] not in stale]
                active = {self._summary_key(row) for row in normalized}
                for row in normalized:
                    key = self._summary_key(row)
                    prior = self.state["pending"].get(key) or self.state["synced"].get(key)
                    if prior and _utc(prior["statistics_until"]) > _utc(row["statistics_until"]):
                        continue  # A delayed older scan cannot roll back a newer snapshot.
                    if self.state["synced"].get(key, {}).get("fingerprint") != self._fingerprint(row) or self.state["synced"].get(key, {}).get("retired"):
                        self.state["pending"][key] = row
                    else:
                        self.state["pending"].pop(key, None)
                # A binding removed or changed after a failed sync invalidates
                # its pending cloud write, but never changes local statistics.
                for key, row in list(self.state["pending"].items()):
                    if any(self.config["bindings"].get(task) != row["project_id"] for task in row["task_ids"]):
                        self.state["pending"].pop(key)
                self._save(self.state_path, self.state)
                if not self._ready():
                    raise NotionError("not_configured")
                if not force and self.state.get("automatic_sync_paused"):
                    return {**self._status(), "changed": 0}
                self._token()
                attempt = self.state.get("last_attempt")
                if not force and attempt and (now - _utc(attempt)).total_seconds() < 300:
                    return {**self._status(), "changed": 0}
                self.state["last_attempt"] = now.isoformat()
                self._projects(force=force)
                if self.state.get("automatic_sync_paused"):
                    raise NotionError("api_access_blocked")
                remote_retire = {}
                remote_seen = set()
                # Reconcile complete snapshots with server metadata as well as
                # local state. A lost local sync cache or plugin bootstrap must
                # not leave an earlier allocation active after rebinding.
                for dataset in complete:
                    pages = self._query(self.config["usage_data_source_id"], filter={
                        "property": "Dataset", "rich_text": {"equals": dataset}})
                    for page in pages:
                        props = page.get("properties", {})
                        if (props.get("Provider", {}).get("select") or {}).get("name") != "Codex" or not props.get("Included", {}).get("checkbox"):
                            continue
                        key = _plain(props.get("Summary Key", {}).get("rich_text"))
                        if key in remote_seen:
                            raise NotionError("duplicate_summary")
                        remote_seen.add(key)
                        if key in active:
                            continue
                        relation = props.get("Project", {}).get("relation", [])
                        day = (props.get("Day", {}).get("date") or {}).get("start")
                        if len(relation) != 1 or not isinstance(day, str):
                            raise NotionError("schema_mismatch")
                        expected = self._summary_key({"dataset_id": dataset, "project_id": notion_id(relation[0].get("id")),
                            "day": day, "provider": "Codex"})
                        if expected != key:
                            raise NotionError("schema_mismatch")
                        if key in remote_retire:
                            raise NotionError("duplicate_summary")
                        remote_retire[key] = notion_id(page.get("id"))
                # A complete dataset snapshot can move a task to another
                # project. Retire its previous allocation before creating the
                # new one; keep original counters as an audit, not a second use.
                for key, prior in self.state["synced"].items():
                    if prior.get("dataset_id") in complete and key not in active and not prior.get("retired"):
                        remote_retire.setdefault(key, notion_id(prior["page_id"]))
                for key, page_id in remote_retire.items():
                    self._request("PATCH", f"/pages/{page_id}", {
                        "properties": {"Included": {"checkbox": False}, "Counted Tokens": {"number": 0},
                                       "Synced At": {"date": {"start": now.isoformat()}}}}, idempotent=True)
                    if key in self.state["synced"]:
                        self.state["synced"][key]["retired"] = True
                    changed += 1
                    self._save(self.state_path, self.state)
                for key, row in list(self.state["pending"].items()):
                    meta = self.state["projects"].get(row["project_id"])
                    if meta and not meta.get("available", True):
                        raise NotionError("object_not_found")
                    # Check access before each cloud write. The returned status
                    # is metadata only and never changes the Token denominator.
                    meta = self._verify_project(row["project_id"])
                    self.state["projects"][meta["page_id"]] = meta
                    self._upsert(key, row, now)
                    changed += 1
                self.state["last_sync"] = now.isoformat()
                for dataset in complete:
                    clocks[dataset] = cutoff.isoformat()
                self.state.pop("error", None)
                self.state.pop("automatic_sync_paused", None)
            except NotionError as exc:
                self._remember_error(exc)
            return {**self._status(), "changed": changed}

    def _status(self):
        # Last known connection state only. Rendering must never query Keychain
        # or trigger network/credential prompts. setup/sync verify credentials.
        credential = bool(self.state.get("credential_available", False))
        current_error = self.state.get("error")
        current_state = "disconnected" if not credential or not self._ready() else "error" if current_error else "ready"
        return {"connected": credential and self._ready(), "credential_available": credential,
                "state": current_state, "message": current_error.get("message") if current_error else
                    "Notion 已连接。" if current_state == "ready" else MESSAGES["not_connected"],
                "projects_url": self.config.get("projects_url"), "usage_url": self.config.get("usage_url"),
                "last_synced_at": self.state.get("last_sync"),
                "automatic_sync_paused": bool(self.state.get("automatic_sync_paused")),
                "ready": self._ready(), "api_version": API_VERSION,
                "dataset_id": self.config["dataset_id"], "sync_since": self.config.get("sync_since"),
                "binding_count": len(self.config["bindings"]), "bindings": dict(self.config["bindings"]),
                "pending_count": len(self.state["pending"]),
                "uncertain_count": len(self.state["uncertain_creates"]),
                "last_sync": self.state.get("last_sync"), "next_retry": self.state.get("next_retry"),
                "error": self.state.get("error"), "projects_error": self.state.get("projects_error"),
                "projects": list(self.state["projects"].values())}

    def status(self) -> dict:
        return self.cached_status()

    def project_for_task(self, task_id: str) -> dict | None:
        # Atomic replacement makes these reads safe without waiting on the
        # worker's network lock. Metadata may lag one write; counters do not.
        config = self._load(self.config_path, {"bindings": {}})
        state = self._load(self.state_path, {"projects": {}})
        page = config["bindings"].get(task_id)
        meta = state["projects"].get(page)
        if not meta:
            return None
        fetched = meta.get("fetched_at")
        stale = not fetched or (self._now() - _utc(fetched)).total_seconds() >= 600
        return {**meta, "stale": meta.get("stale", False) or stale or bool(state.get("projects_error"))}

    def cached_status(self) -> dict:
        """Non-network snapshot for the 60-second scanner/native renderer."""
        self.config = self._load(self.config_path, {"dataset_id": "", "bindings": {}})
        self.state = self._load(self.state_path, {"projects": {}, "pending": {}, "uncertain_creates": {}})
        return self._status()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Notion 项目与每日摘要同步；JSON 命令由 stdin 输入，凭据从明确选择的 Pages 文件或应用专用钥匙串读取。")
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parent / "data")
    args = parser.parse_args(argv)
    workspace = None
    try:
        command = json.load(sys.stdin)
        if not isinstance(command, dict) or any(key in command for key in ("token", "api_key", "authorization")):
            raise NotionError("invalid_input")
        workspace = NotionWorkspace(args.data_dir)
        name = command.get("command")
        if name == "status":
            result = workspace.status()
        elif name == "projects":
            result = workspace.projects(force=bool(command.get("force", True)))
        elif name == "setup":
            result = workspace.setup(command.get("parent_page"))
        elif name == "bind":
            result = workspace.bind(command.get("task_id"), command.get("page_id"))
        elif name == "unbind":
            result = workspace.unbind(command.get("task_id"))
        elif name == "create_project":
            result = workspace.create_project(command.get("task_id"), command.get("name"))
        elif name == "sync":
            result = workspace.sync(command.get("rows", []), force=bool(command.get("force", True)),
                                    complete_dataset_ids=command.get("complete_dataset_ids"), complete_until=command.get("complete_until"))
        else:
            raise NotionError("invalid_input")
        status = workspace.status()
        failed = name in ("projects", "sync") and bool(status.get("projects_error") if name == "projects" else status.get("error"))
        output = {"ok": not failed, "result": result, "status": status}
        if failed:
            output["error"] = status.get("projects_error") if name == "projects" else status.get("error")
        print(json.dumps(output, ensure_ascii=False))
        return 1 if failed else 0
    except NotionError as exc:
        print(json.dumps({"ok": False, "error": exc.public(),
                          "status": workspace.status() if workspace else None}, ensure_ascii=False))
        return 1
    except Exception:
        # Do not emit a traceback: even injected failures can contain credentials.
        print(json.dumps({"ok": False, "error": NotionError("invalid_input").public()}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
