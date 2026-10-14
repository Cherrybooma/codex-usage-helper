"""Local project identities and daily facts; never contains conversational text."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
import sqlite3
import tempfile
import uuid

COUNTS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
          "output_tokens", "reasoning_output_tokens", "total_tokens",
          "request_records", "legacy_updates")
PROJECT_MANAGEMENT_FIELDS = ("project_goal", "weekly_goal", "next_step", "project_priority",
                             "project_health", "target_date", "last_reviewed", "project_summary",
                             "open_tasks", "blocked_tasks", "completed_tasks")


def project_progress(meta: dict) -> dict:
    value = meta.get("development_progress")
    if (meta.get("progress_source") == "manual" and isinstance(value, (int, float))
            and not isinstance(value, bool) and 0 <= value <= 100 and math.isfinite(value)):
        return {"development_progress": float(value), "progress_source": "manual"}
    opened, completed = meta.get("open_tasks"), meta.get("completed_tasks")
    if (all(isinstance(count, int) and not isinstance(count, bool) and count >= 0 for count in (opened, completed))
            and opened + completed > 0):
        return {"development_progress": completed * 100 / (opened + completed), "progress_source": "tasks"}
    return {"development_progress": None, "progress_source": None}


def management_metadata(meta: dict) -> dict:
    values = {}
    for field in PROJECT_MANAGEMENT_FIELDS:
        value = meta.get(field)
        if field.endswith("_tasks"):
            values[field] = value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
        else:
            values[field] = value if isinstance(value, str) and value else None
    return {**values, **project_progress(meta),
            "notion_icon_missing": meta.get("icon_missing"),
            "project_status": meta.get("status"),
            "icon_type": meta.get("icon_type"), "icon_value": meta.get("icon_value"),
            "notion_url": meta.get("url"), "notion_stale": bool(meta.get("stale", False))}


def management_projects(projects: list[dict], usage_rows: list[dict], bindings: dict, *, today_known=True) -> list[dict]:
    """Presentation only: include managed projects even without weekly usage.

    The caller must never add this array to token totals or send it to the daily
    outbox. Missing local attribution remains explicit, even though numeric
    fields keep the native TaskRow schema compatible.
    """
    by_page = {row["task_id"][7:]: row for row in usage_rows
               if row.get("task_id", "").startswith("notion:")}
    available = [meta for meta in projects if meta.get("available", True)
                 and meta.get("status") not in ("归档", "已归档")]
    if any(meta.get("status") in ("开发中", "计划中") for meta in available):
        available = [meta for meta in available if meta.get("status") != "暂停"]
    status_order = {"开发中": 0, "计划中": 0, "已发布": 1, "暂停": 2}
    priority_order = {"高": 0, "中": 1, "低": 2}
    result = []
    for meta in available:
        page_id = meta["page_id"]
        linked = sorted(task_id for task_id, page in bindings.items() if page == page_id)
        row = dict(by_page.get(page_id) or {
            "task_id": "notion:" + page_id, "name": meta.get("display_name") or "未命名项目",
            **{field: 0 for field in COUNTS}, "percent": 0,
            "sessions_count": 0 if linked else None, "task_ids": linked,
            "today_tokens": 0 if linked and today_known else None})
        row.update(management_metadata(meta))
        row["display_name"] = meta.get("display_name") or row["name"]
        row["usage_linked"] = bool(linked)
        if not linked:
            row["today_tokens"] = None
        result.append(row)
    return sorted(result, key=lambda row: (status_order.get(row.get("project_status"), 4),
                  priority_order.get(row.get("project_priority"), 3),
                  row.get("display_name") or row["name"], row["task_id"]))


def atomic_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=path.name + ".", delete=False) as stream:
        temp = Path(stream.name)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    try:
        temp.chmod(0o600)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def project_location(project: str, task_root: Path) -> tuple[str, str]:
    root = task_root.expanduser().resolve()
    if project == "unknown":
        return "unknown", "未知目录"
    if not Path(project).expanduser().is_absolute():
        # A foreign Windows path on macOS must not resolve relative to this app
        # and accidentally charge the current project.
        return "outside-root", "临时及其他目录"
    try:
        relative = Path(project).expanduser().resolve().relative_to(root)
    except (ValueError, OSError):
        return "outside-root", "临时及其他目录"
    if not relative.parts:
        return "folder:" + str(root), "文稿根目录"
    path = root / relative.parts[0]
    return "folder:" + str(path), relative.parts[0]


class ProjectRegistry:
    def __init__(self, data_dir: Path, codex_home: Path):
        self.path = data_dir / "project-registry.json"
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(self.data, dict) or self.data.get("version") != 1 or not all(
                    isinstance(self.data.get(key), dict) for key in ("datasets", "projects")):
                raise ValueError("项目身份配置版本不支持，请保留原文件。")
        else:
            self.data = {"version": 1, "datasets": {}, "projects": {}}
        source = str(codex_home.expanduser().resolve())
        key = hashlib.sha256(source.encode()).hexdigest()
        if key not in self.data["datasets"]:
            self.data["datasets"][key] = str(uuid.uuid4())
        self.dataset_id = self.data["datasets"][key]

    def identify(self, key: str, name: str) -> str:
        known = self.data["projects"].get(key)
        if known is None:
            known = {"task_id": str(uuid.uuid4()), "name": name}
            self.data["projects"][key] = known
        known["name"] = name
        return known["task_id"]

    def save(self) -> None:
        atomic_write(self.path, self.data)

    def session_key(self, thread_id: str) -> str:
        # This local-only key allows cross-folder/day distinct counts. It is
        # never a Notion property and does not expose a raw session identifier.
        return hashlib.sha256((self.dataset_id + ":" + thread_id).encode()).hexdigest()


def aggregate(report: dict, task_root: Path, registry: ProjectRegistry,
              metadata: dict[str, dict] | None = None) -> tuple[list[dict], list[dict], list[dict]]:
    metadata = metadata or {}
    cutoff = datetime.fromisoformat(report["range"]["until_exclusive"].replace("Z", "+00:00"))
    today = cutoff.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    today_known = isinstance(report.get("project_daily"), list)
    weekly: dict[str, dict] = {}
    identities: dict[str, tuple[str, str]] = {}
    sources: dict[str, dict] = {}

    def resolve(project: str) -> tuple[str, str]:
        if project not in identities:
            key, name = project_location(project, task_root)
            identities[project] = registry.identify(key, name), name
        return identities[project]

    def row_for(task_id: str, name: str) -> dict:
        meta = metadata.get(task_id, {})
        page_id = meta.get("page_id")
        display_key = "notion:" + page_id if page_id else task_id
        if display_key not in weekly:
            weekly[display_key] = {
                "task_id": display_key, "name": name,
                "display_name": meta.get("display_name") or name,
                "project_status": meta.get("status"),
                "icon_type": meta.get("icon_type"), "icon_value": meta.get("icon_value"),
                "notion_url": meta.get("url"), "notion_stale": bool(meta.get("stale", False)),
                **management_metadata(meta),
                **{field: 0 for field in COUNTS}, "_sessions": set(), "task_ids": [],
                "today_tokens": 0 if today_known else None,
            }
        row = weekly[display_key]
        row["usage_linked"] = True
        if task_id not in row["task_ids"]:
            row["task_ids"].append(task_id)
        sources[task_id] = {"task_id": task_id, "name": name,
                           "display_name": meta.get("display_name") or name,
                           "notion_page_id": page_id, "project_status": meta.get("status")}
        return row

    for project in report["projects"]:
        task_id, name = resolve(project["project"])
        row = row_for(task_id, name)
        for field in COUNTS:
            row[field] += project.get(field, 0)
        row["_sessions"].update(project.get("thread_ids", []))

    daily: dict[tuple[str, str], dict] = {}
    for project in report.get("project_daily", []):
        task_id, name = resolve(project["project"])
        if project["day"] == today:
            row_for(task_id, name)["today_tokens"] += project.get("total_tokens", 0)
        key = task_id, project["day"]
        row = daily.setdefault(key, {"task_id": task_id, "day": project["day"],
                                    "dataset_id": registry.dataset_id, "provider": "codex",
                                    **{field: 0 for field in COUNTS}, "_sessions": set()})
        for field in COUNTS:
            row[field] += project.get(field, 0)
        row["_sessions"].update(project.get("thread_ids", []))

    total = report["totals"]["total_tokens"]
    rows = sorted(weekly.values(), key=lambda row: (-row["total_tokens"], row["display_name"], row["task_id"]))
    for row in rows:
        row["sessions_count"] = len(row.pop("_sessions"))
        row["task_ids"].sort()
        row["percent"] = row["total_tokens"] * 100 / total if total else 0
    unreadable = any(report.get("coverage", {}).get(field, 0)
                     for field in ("files_skipped", "malformed_lines", "missing_timestamps")) or any(
        any(marker in warning for marker in ("; skipped", "usage omitted", "read failed",
            "could not be decoded", "No usable token", "missing/invalid timestamp",
            "original timestamp is unknown")) for warning in report.get("warnings", []))
    for row in daily.values():
        session_ids = row.pop("_sessions")
        row["sessions_count"] = len(session_ids)
        row["session_keys"] = sorted(registry.session_key(value) for value in session_ids)
        row["quality"] = (["partial_unreadable"] if unreadable else []) + [
            "legacy_estimated" if row["legacy_updates"] else "response_records_verified"]
        row["statistics_until"] = report["range"]["until_exclusive"]
    registry.save()
    return rows, sorted(daily.values(), key=lambda row: (row["day"], row["task_id"])), sorted(sources.values(), key=lambda row: row["name"])


def split_small(rows: list[dict], total: int) -> tuple[list[dict], list[dict]]:
    small = [row for row in rows if row["total_tokens"] * 100 < total * 5]
    major = [row for row in rows if row["total_tokens"] * 100 >= total * 5]
    if small:
        counts = {field: sum(row.get(field, 0) for row in small) for field in COUNTS}
        major.append({"name": "其他", **counts, "percent": counts["total_tokens"] * 100 / total if total else 0,
                      "today_tokens": sum(row["today_tokens"] for row in small) if all(row.get("today_tokens") is not None for row in small) else None,
                      "members_count": len(small), "is_other": True})
    return major, small


def remember_daily(path: Path, rows: list[dict], dataset_id: str) -> list[dict]:
    """Keep scanned summaries for sync; they are observed history, not raw logs.

    A supplied row replaces its previous observation, including corrections.
    Omitted historical days remain explicitly tagged observed_history.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS summaries
                   (dataset TEXT NOT NULL,task TEXT NOT NULL,day TEXT NOT NULL,
                    payload TEXT NOT NULL,PRIMARY KEY(dataset,task,day))""")
        for row in rows:
            db.execute("INSERT INTO summaries VALUES(?,?,?,?) ON CONFLICT(dataset,task,day) DO UPDATE SET payload=excluded.payload",
                       (dataset_id, row["task_id"], row["day"], json.dumps(row, ensure_ascii=False, sort_keys=True)))
        current = {(row["task_id"], row["day"]) for row in rows}
        stored = []
        for task, day, payload in db.execute("SELECT task,day,payload FROM summaries WHERE dataset=? ORDER BY day,task", (dataset_id,)):
            row = json.loads(payload)
            row["observed_history"] = (task, day) not in current
            stored.append(row)
    path.chmod(0o600)
    return stored
