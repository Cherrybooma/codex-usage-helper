"""Local, allowlisted project manifest validation; no remote transport or credentials."""
from __future__ import annotations
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
from urllib.parse import urlsplit

MAX_MANIFEST_BYTES = 256 * 1024
STATUSES = {"计划中", "开发中", "待验收", "已发布", "暂停", "归档"}
ACTION_STATUSES = {"待处理", "进行中", "待确认", "阻塞", "已完成", "取消"}
PROJECT_FIELDS = {"name", "icon", "vision", "status", "health", "current_goal", "next_step",
                  "summary", "progress", "artifacts", "manual_actions", "history"}

class ManifestError(ValueError):
    def __init__(self, code="invalid_input"):
        self.code = code
        super().__init__("导入清单或成果入口无效，请核对字段。")
    def public(self):
        return {"code": self.code, "message": str(self)}

def _invalid():
    raise ManifestError()

def _safe_https(value):
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) <= 32 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password and (parsed.port is None or 1 <= parsed.port <= 65535):
            return value
    except ValueError:
        pass
    return None

def project_icon(value) -> dict:
    """Validate the project icon before any credential/network access or create.

    Creation must assign it atomically; binding never fixes an icon remotely.
    A single emoji may include variation selectors, skin tones or ZWJ parts.
    """
    if not isinstance(value, dict):
        raise ManifestError("icon_required")
    if value.get("type") == "external":
        external = value.get("external")
        url = _safe_https(external.get("url")) if isinstance(external, dict) else None
        if url:
            return {"type": "external", "external": {"url": url}}
    if value.get("type") == "emoji":
        emoji = value.get("emoji")
        base = r"[\u00a9\u00ae\u203c\u2049\u2122\u2139\u2194-\u21ff\u2300-\u23ff\u25a0-\u27ff\u2934\u2935\u2b00-\u2bff\u3030\u303d\u3297\u3299\U0001f000-\U0001faff]"
        part = base + r"\ufe0f?[\U0001f3fb-\U0001f3ff]?"
        pattern = r"(?:[\U0001f1e6-\U0001f1ff]{2}|[0-9#*]\ufe0f?\u20e3|" + part + r"(?:\u200d" + part + r")*)"
        if isinstance(emoji, str) and len(emoji) <= 32 and re.fullmatch(pattern, emoji):
            return {"type": "emoji", "emoji": emoji}
    raise ManifestError("icon_required")


def _shape(value, fields):
    if not isinstance(value, dict) or set(value) - set(fields):
        _invalid()


def _text(value, *, limit=1000, required=False):
    if value is None and not required:
        return ""
    if not isinstance(value, str) or len(value) > limit:
        _invalid()
    value = value.strip()
    # The manifest is an allow-list, not a way to send credentials or private paths.
    if (required and not value) or any(ord(c) < 32 and c not in "\n\t" for c in value):
        _invalid()
    if re.search(r"(?:sk-|ntn_|secret_)[A-Za-z0-9_-]{16,}", value):
        _invalid()
    if (re.search(r"(?:/Users/|/home/)", value)
            or re.search(r"(?:^|[\s\"'(])[A-Za-z]:[\\/]", value) or "```" in value):
        _invalid()
    return value


def _url(value):
    if value in (None, ""):
        return None
    value = _text(value, limit=2048)
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            _invalid()
        if parsed.hostname.lower() in ("localhost", "127.0.0.1", "::1"):
            _invalid()
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            _invalid()
    except ValueError:
        _invalid()
    return value


def _time(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            _invalid()
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, AttributeError, TypeError):
        _invalid()


def validate_manifest(value, folder: Path | None = None) -> dict:
    """Reject unexpected fields instead of forwarding arbitrary user JSON."""
    _shape(value, {"version", "project"})
    if value.get("version") != 1 or isinstance(value.get("version"), bool):
        _invalid()
    project = value.get("project")
    _shape(project, PROJECT_FIELDS)
    result = {key: _text(project.get(key), required=key in {"name", "vision", "current_goal", "next_step"},
                         limit=200 if key == "name" else 1000)
              for key in ("name", "vision", "current_goal", "next_step", "summary")}
    result["icon"] = project_icon(project.get("icon"))
    result["status"] = project.get("status", "开发中")
    result["health"] = project.get("health", "正常")
    if result["status"] not in STATUSES or result["health"] not in {"正常", "顺利", "有风险", "受阻", "阻塞"}:
        _invalid()
    progress = project.get("progress")
    if progress is None:
        result["progress"] = None
    else:
        _shape(progress, {"percent", "evidence", "reviewed_at", "milestones"})
        percent = progress.get("percent")
        if (not isinstance(percent, (int, float)) or isinstance(percent, bool)
                or not 0 <= percent <= 100 or not math.isfinite(percent)):
            _invalid()
        milestones = progress.get("milestones")
        if not isinstance(milestones, list) or not 1 <= len(milestones) <= 20:
            _invalid()
        normalized = []
        for item in milestones:
            _shape(item, {"name", "weight", "completion", "evidence"})
            weight, completion = item.get("weight"), item.get("completion")
            if (not isinstance(weight, (int, float)) or isinstance(weight, bool)
                    or not 0 < weight <= 1000 or not math.isfinite(weight)
                    or not isinstance(completion, (int, float)) or isinstance(completion, bool)
                    or not 0 <= completion <= 100 or not math.isfinite(completion)):
                _invalid()
            normalized.append({"name": _text(item.get("name"), limit=120, required=True),
                               "weight": weight, "completion": completion,
                               "evidence": _text(item.get("evidence"), required=True)})
        derived = sum(m["weight"] * m["completion"] for m in normalized) / sum(m["weight"] for m in normalized)
        if abs(percent - round(derived)) > .000001:
            _invalid()
        result["progress"] = {"percent": percent, "evidence": _text(progress.get("evidence"), required=True),
                              "reviewed_at": _time(progress.get("reviewed_at")), "milestones": normalized}
    result["artifacts"] = []
    artifacts = project.get("artifacts", [])
    if not isinstance(artifacts, list) or len(artifacts) > 12:
        _invalid()
    for item in artifacts:
        _shape(item, {"title", "url", "relative_path", "result"})
        path = item.get("relative_path")
        if path:
            path = _text(path, limit=512)
            relative = Path(path)
            if relative.is_absolute() or ".." in relative.parts or "\\" in path:
                _invalid()
            if folder is not None:
                target = (folder / relative).resolve()
                if not target.is_relative_to(folder.resolve()) or not target.is_file():
                    _invalid()
        url = _url(item.get("url"))
        if not path and not url:
            _invalid()
        result["artifacts"].append({"title": _text(item.get("title"), limit=160, required=True),
                                    "url": url, "relative_path": path or None,
                                    "result": _text(item.get("result"), required=True)})
    for field, allowed in (("manual_actions", {"id", "name", "status", "decision", "recommendation", "impact", "evidence_url"}),
                           ("history", {"id", "title", "occurred_at", "result", "evidence_url"})):
        items = project.get(field, [])
        if not isinstance(items, list) or len(items) > 30:
            _invalid()
        result[field], seen = [], set()
        for item in items:
            _shape(item, allowed)
            identity = _text(item.get("id"), limit=160, required=True)
            if identity in seen:
                _invalid()
            seen.add(identity)
            row = {key: _text(item.get(key), limit=1000 if key not in ("id", "name", "title") else 160,
                              required=key in ("id", "name", "title", "result"))
                   for key in allowed - {"status", "occurred_at", "evidence_url"}}
            row["evidence_url"] = _url(item.get("evidence_url"))
            if field == "manual_actions":
                row["status"] = item.get("status", "待确认")
                if row["status"] not in ACTION_STATUSES:
                    _invalid()
            else:
                row["occurred_at"] = _time(item["occurred_at"]) if item.get("occurred_at") else None
            result[field].append(row)
    return {"version": 1, "project": result}
