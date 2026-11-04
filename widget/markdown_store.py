"""Markdown project provider. All management reads are local and side-effect free.

The flat YAML properties subset is deliberately the same shape as Obsidian
Properties: scalar values and lists. Long text belongs in the Markdown body.
No remote transport, credential lookup, log scan or quota fetch occurs here.
"""
from __future__ import annotations

import argparse
import ast
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from urllib.parse import urlencode
import uuid
from zoneinfo import ZoneInfo

UTC = timezone.utc
SHANGHAI = ZoneInfo("Asia/Shanghai")
# The source checkout moved back into the original product. Management data and
# rules stay in their existing central locations; never derive a new vault from
# the checkout's nesting depth.
DEFAULT_VAULT = Path(os.environ.get("PROJECT_PM_VAULT") or Path.home() / "Documents" / "项目管理obsidian").expanduser().resolve()
DEFAULT_RULES = Path(os.environ.get("PROJECT_PM_RULES_ROOT") or Path.home() / "Documents" / "项目管理系统" / "规则").expanduser().resolve()
DEFAULT_DATA = Path(__file__).resolve().parent / "data"
COUNTERS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
            "output_tokens", "reasoning_output_tokens", "total_tokens",
            "request_records", "legacy_updates")
PROJECT_STATES = {"计划中", "开发中", "进行中", "待验收", "已发布", "暂停", "归档", "已归档"}
TASK_STATES = {"待处理", "进行中", "待确认", "阻塞", "已完成", "取消"}
PHASES = {"确认目标与范围", "确认首版目标与范围", "验证关键可行性", "核心流程开发", "完成核心使用流程", "整体验收", "发布与反馈", "发布试用并收集反馈"}
KINDS = {"project", "task", "event", "usage_daily", "usage_period", "quota_window", "feedback", "artifact"}
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")


class MarkdownError(ValueError):
    def __init__(self, code="invalid_input", message="Markdown 管理资料无效，请检查字段。"):
        self.code, self.message = code, message
        super().__init__(message)

    def public(self):
        return {"code": self.code, "message": self.message}


def now_iso():
    return datetime.now(UTC).isoformat()


def _time(value):
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise MarkdownError("invalid_time", "时间必须为带时区的 ISO 格式。") from None
    else:
        raise MarkdownError("invalid_time", "缺少有效时间。")
    if result.tzinfo is None:
        raise MarkdownError("invalid_time", "时间必须包含时区。")
    return result.astimezone(UTC)


def _identity(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise MarkdownError("invalid_id", "记录 ID 只能使用字母、数字、点、下划线和连字符。")
    return value


def _scalar(raw):
    raw = raw.strip()
    if not raw or raw in ("null", "Null", "NULL", "~"):
        return None
    if raw.startswith(('"', "'")):
        try:
            value = json.loads(raw) if raw.startswith('"') else raw[1:-1].replace("''", "'")
            if raw.startswith("'") and not raw.endswith("'"):
                raise ValueError()
            return value
        except (ValueError, TypeError):
            raise MarkdownError("invalid_yaml", "属性中的引号未正确闭合。") from None
    if raw.startswith("["):
        try:
            value = json.loads(raw)
        except ValueError:
            try:
                value = ast.literal_eval(raw)
            except (ValueError, SyntaxError):
                raise MarkdownError("invalid_yaml", "列表请使用带引号的值，或 Obsidian 的列表属性。") from None
        if not isinstance(value, list) or any(isinstance(x, (dict, list)) for x in value):
            raise MarkdownError("invalid_yaml", "属性仅支持一层列表。")
        return value
    if raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    if re.fullmatch(r"[-+]?\d+", raw):
        return int(raw)
    if re.fullmatch(r"[-+]?(?:\d+\.\d*|\d*\.\d+)(?:[eE][-+]?\d+)?", raw):
        value = float(raw)
        if not math.isfinite(value):
            raise MarkdownError("invalid_yaml", "属性数字必须有限。")
        return value
    if raw in ("|", ">", "|-", ">-") or raw.startswith(("{", "&", "*", "!")):
        raise MarkdownError("invalid_yaml", "长文请写在正文；属性只使用标量和一层列表。")
    # A comment starts only after whitespace, matching ordinary YAML scalars.
    return re.split(r"\s+#", raw, maxsplit=1)[0].rstrip()


def read_note(path: Path):
    path = Path(path)
    if path.stat().st_size > 4 * 1024 * 1024:
        raise MarkdownError("note_too_large", "管理笔记超过读取上限。")
    text = path.read_text(encoding="utf-8-sig")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise MarkdownError("invalid_yaml", "笔记属性缺少结束分隔线：" + path.name)
    props, pending = {}, None
    for raw in lines[1:end]:
        line = raw.rstrip("\r\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith((" ", "\t")) or line.startswith("- "):
            match = re.fullmatch(r"\s+-\s+(.*)", line)
            if not match or pending is None or props[pending] not in (None, []) and not isinstance(props[pending], list):
                raise MarkdownError("invalid_yaml", "不支持嵌套属性：" + path.name)
            if props[pending] is None:
                props[pending] = []
            props[pending].append(_scalar(match.group(1)))
            continue
        match = re.fullmatch(r"([^:#][^:]*):(?:\s+(.*)|\s*)", line)
        if not match:
            raise MarkdownError("invalid_yaml", "属性格式无效：" + path.name)
        key = match.group(1).strip()
        if key in props:
            raise MarkdownError("duplicate_property", "属性重复：" + key)
        props[key] = _scalar(match.group(2) or "")
        pending = key
    return props, "".join(lines[end + 1:]).lstrip("\n")


def note_text(props, body):
    lines = ["---"]
    for key, value in props.items():
        if not isinstance(key, str) or ":" in key or "\n" in key:
            raise MarkdownError("invalid_yaml", "属性名无效。")
        if isinstance(value, (dict, tuple)) or isinstance(value, list) and any(isinstance(x, (dict, list)) for x in value):
            raise MarkdownError("invalid_yaml", "属性只支持标量和一层列表。")
        lines.append(key + ": " + json.dumps(value, ensure_ascii=False, allow_nan=False))
    return "\n".join(lines) + "\n---\n\n" + body.rstrip() + "\n"


@contextmanager
def _file_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as lock:
        path.chmod(0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _publication_owner(vault, data_dir):
    """Read the optional central publisher assignment without claiming ownership.

    The assignment is intentionally separate from per-checkout run state. A
    malformed assignment cannot silently turn publishing back on. An explicit
    handover must replace this file while holding the vault publication lock.
    """
    marker = Path(vault) / ".ledger-publisher.json"
    try:
        descriptor = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return
    except OSError:
        raise MarkdownError("invalid_publisher", "账本发布配置不可读；为保护已有投入，未写入账本。") from None
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024:
                raise ValueError()
            assignment = json.load(stream)
        owner = assignment.get("owner_data_dir") if isinstance(assignment, dict) else None
        if not isinstance(owner, str) or not owner or owner != owner.strip():
            raise ValueError()
        owner_path = Path(owner)
        if not owner_path.is_absolute() or owner_path != owner_path.resolve(strict=True) or not owner_path.is_dir():
            raise ValueError()
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        raise MarkdownError("invalid_publisher", "账本发布配置无效；为保护已有投入，未写入账本。") from None
    if owner_path != Path(data_dir).expanduser().resolve():
        raise MarkdownError("publisher_not_owner", "此管理库已有指定的账本发布程序；当前副本仅可读取，未写入投入。")


@contextmanager
def _ledger_publication(vault, data_dir):
    """Serialize ledger publishers across checkout/data directories.

    Only ledger writes enter here. Reading notes, binding projects and ordinary
    management updates never create this lock or a publisher assignment.
    """
    vault = Path(vault)
    if not vault.is_dir():
        raise MarkdownError("vault_missing", "项目管理库不存在，未发布账本。")
    try:
        descriptor = os.open(vault / ".ledger-publication.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise MarkdownError("publication_lock_failed", "无法锁定中央账本，未发布投入。") from None
    with os.fdopen(descriptor, "a+") as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
            raise MarkdownError("publication_lock_failed", "中央账本锁无效，未发布投入。")
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            _publication_owner(vault, data_dir)
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".pm-", delete=False) as stream:
            temp = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temp.chmod(0o600)
        temp.replace(path)
    finally:
        if temp:
            temp.unlink(missing_ok=True)


def note_digest(path: Path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_note(path: Path, props: dict, body: str, expected_revision=None, expected_digest=None):
    """Atomic, revision-checked single-note write; caller keeps human sections."""
    path = Path(path)
    lock = path.parent / ".pm-write.lock"
    with _file_lock(lock):
        if path.exists():
            old, _ = read_note(path)
            if expected_digest is not None and note_digest(path) != expected_digest:
                raise MarkdownError("revision_conflict", "笔记内容已被修改，请重新读取后重试。")
            if expected_revision is not None and old.get("revision") != expected_revision:
                raise MarkdownError("revision_conflict", "笔记已被修改，请重新读取后重试。")
        elif expected_revision is not None or expected_digest is not None:
            raise MarkdownError("revision_conflict", "原笔记已移动或删除，请重新读取。")
        _atomic_text(path, note_text(props, body))


def _json(path, default):
    path = Path(path)
    if not path.is_file():
        return default
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, type(default)):
        raise MarkdownError("invalid_config", "本机管理配置格式无效。")
    return value


def _save_json(path, value):
    _atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def section(body, heading, first=False):
    match = re.search(r"(?m)^##\s+" + re.escape(heading) + r"\s*\n(.*?)(?=^##\s|\Z)", body, re.S | re.M)
    if not match:
        return ""
    text = match.group(1).strip()
    if first:
        paragraphs = re.split(r"\n\s*\n", text)
        text = next((p.strip() for p in paragraphs if p.strip() and not p.startswith(("![[", "```", "###"))), "")
    return text


def replace_section(body, heading, content):
    pattern = r"(?m)^##\s+" + re.escape(heading) + r"\s*\n.*?(?=^##\s|\Z)"
    replacement = "## " + heading + "\n\n" + content.strip() + "\n\n"
    if re.search(pattern, body, re.S | re.M):
        return re.sub(pattern, lambda _: replacement, body, count=1, flags=re.S | re.M)
    return body.rstrip() + "\n\n" + replacement


def _note_url(vault, path):
    return "obsidian://open?" + urlencode({"vault": Path(vault).name, "file": str(Path(path).relative_to(vault)).replace(os.sep, "/")})


def _records(vault):
    vault = Path(vault).expanduser().resolve()
    if not vault.is_dir():
        raise MarkdownError("vault_missing", "项目管理库不存在，请先选择正确目录。")
    records, seen = [], {}
    for folder in ("项目", "任务", "事件", "反馈", "成果", "账本"):
        base = vault / folder
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.md")):
            if path.is_symlink() or not path.resolve().is_relative_to(vault):
                raise MarkdownError("unsafe_path", "管理记录不能通过符号链接离开资料库。")
            digest = note_digest(path)
            props, body = read_note(path)
            if note_digest(path) != digest:
                raise MarkdownError("read_conflict", "管理资料正在修改，请稍后重试。")
            kind = props.get("type")
            if kind not in KINDS:
                continue
            identity = _identity(props.get("id"))
            if identity in seen:
                raise MarkdownError("duplicate_id", "发现重复记录 ID：" + identity)
            seen[identity] = path
            if kind == "project":
                if props.get("status") not in PROJECT_STATES:
                    raise MarkdownError("invalid_status", "项目状态无效：" + path.name)
                progress = props.get("progress")
                if progress is not None and (isinstance(progress, bool) or not isinstance(progress, (int, float)) or not math.isfinite(progress) or not 0 <= progress <= 100):
                    raise MarkdownError("invalid_progress", "项目进度必须为空或 0 到 100。")
                if progress is not None and props.get("progress_source") not in ("ai", "manual"):
                    raise MarkdownError("invalid_progress", "进度必须保留 AI 或人工评估来源。")
                if props.get("phase") and props["phase"] not in PHASES:
                    raise MarkdownError("invalid_phase", "项目阶段不在约定的五阶段中。")
            if kind == "task":
                if props.get("status") not in TASK_STATES:
                    raise MarkdownError("invalid_status", "任务状态无效：" + path.name)
                if props.get("managed_by") not in ("AI", "人类确认"):
                    raise MarkdownError("invalid_owner", "任务需要明确 AI 或人类确认负责人。")
                if props.get("kind") not in ("执行", "决策", "验收", "外部行动"):
                    raise MarkdownError("invalid_kind", "任务类型无效。")
            if kind in ("usage_daily", "usage_period"):
                for counter in (*COUNTERS, "counted_tokens"):
                    value = props.get(counter)
                    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                        raise MarkdownError("invalid_counter", "账本计数必须为非负整数或未知。")
            records.append({**props, "path": str(path.relative_to(vault)), "url": _note_url(vault, path), "body": body, "digest": digest})
    projects = {r["id"] for r in records if r["type"] == "project"}
    for row in records:
        if row.get("project_id") and row["project_id"] not in projects:
            raise MarkdownError("unknown_project", "记录引用了不存在的项目：" + str(row["project_id"]))
    return records


def _project_metadata(record, tasks, artifacts=()):
    body, props = record["body"], record
    identity = record["id"]
    related = [t for t in tasks if t.get("project_id") == identity and not t.get("archived", False)]
    human = [t for t in related if t.get("managed_by") == "人类确认"]
    def counts(items):
        return {"open": sum(t["status"] in ("待处理", "进行中", "待确认", "阻塞") for t in items),
                "blocked": sum(t["status"] == "阻塞" for t in items),
                "completed": sum(t["status"] == "已完成" for t in items)}
    all_counts, human_counts = counts(related), counts(human)
    icon = props.get("icon")
    icon = icon if isinstance(icon, str) and icon.strip() else None
    icon_type = "external" if icon and icon.startswith("https://") else "emoji" if icon else None
    current = section(body, "目前要做的事", first=True) or props.get("current_goal")
    goal = section(body, "总目标", first=True) or props.get("goal")
    # Existing project cards need not repeat their registered artifact fields.
    # Derive the missing display entry from that project's formal references;
    # this is a read-only view, not an edit or a new completion/acceptance claim.
    def artifact_order(artifact):
        occurred = None
        for field in ("updated", "created_at", "created"):
            if not artifact.get(field):
                continue
            try:
                occurred = _time(artifact[field])
                break
            except MarkdownError:
                continue
        return (occurred is not None, occurred or datetime.min.replace(tzinfo=UTC), artifact["id"])
    related_artifacts = [a for a in artifacts if a.get("project_id") == identity and not a.get("archived", False)]
    latest_artifact = max(related_artifacts, key=artifact_order) if related_artifacts else {}
    latest_title = props.get("latest_output_title") or latest_artifact.get("name")
    if not latest_title and latest_artifact.get("path"):
        latest_title = Path(latest_artifact["path"]).stem
    latest_url = props.get("latest_output_url") or latest_artifact.get("url")
    return {"project_id": identity, "id": identity,
            "provider": "markdown", "name": props.get("name") or Path(props["path"]).parent.name,
            "display_name": props.get("name") or Path(props["path"]).parent.name,
            "url": props["url"], "path": props["path"], "status": props["status"],
            "body": body, "digest": props.get("digest"),
            "available": True, "stale": False, "icon_type": icon_type, "icon_value": icon,
            "icon_missing": icon is None, "project_goal": goal, "current_goal": current,
            "weekly_goal": props.get("weekly_goal") or current, "next_step": props.get("next_step"),
            "project_priority": props.get("priority"), "project_health": props.get("health"),
            "target_date": props.get("target_date"), "last_reviewed": props.get("reviewed") or props.get("progress_reviewed"),
            "project_summary": props.get("summary") or current,
            "latest_progress_summary": props.get("summary") or current,
            "development_progress": props.get("progress"), "progress_source": props.get("progress_source"),
            "progress_evidence": props.get("progress_evidence"), "progress_reviewed": props.get("progress_reviewed"),
            "latest_output_title": latest_title, "latest_output_url": latest_url,
            "phase": props.get("phase"), "work_state": props.get("work_state"),
            "revision": props.get("revision"), "updated": props.get("updated"),
            **{f"{prefix}_{kind}_tasks" if prefix else f"{kind}_tasks": value
               for prefix, values in (("", all_counts), ("human", human_counts)) for kind, value in values.items()}}


def management_snapshot(vault: Path, data_dir: Path | None = None):
    """Public read model: file reads only, with no scans, network or writes."""
    vault = Path(vault).expanduser().resolve()
    if data_dir is not None:
        state = _json(Path(data_dir) / "markdown-state.json", {})
        if state.get("publication_in_progress"):
            cached = _json(Path(data_dir) / "markdown-last-complete.json", {})
            if not cached:
                raise MarkdownError("publication_in_progress", "计量摘要正在发布或上次发布未完成，请重试发布。")
            return {**cached, "stale": True, "source_state": "publication_in_progress",
                    "projects": [{**p, "stale": True} for p in cached.get("projects", [])]}
    records = _records(vault)
    tasks = [r for r in records if r["type"] == "task"]
    artifacts = [r for r in records if r["type"] == "artifact"]
    projects = [_project_metadata(r, tasks, artifacts) for r in records if r["type"] == "project"]
    return {"schema_version": 1, "provider": "markdown", "generated_at": now_iso(),
            "projects": projects, "tasks": tasks,
            "events": [r for r in records if r["type"] == "event"],
            "feedback": [r for r in records if r["type"] == "feedback"],
            "artifacts": artifacts,
            "usage": {"daily": [r for r in records if r["type"] == "usage_daily"],
                      "periods": [r for r in records if r["type"] == "usage_period"],
                      "windows": [r for r in records if r["type"] == "quota_window"]}}


def _accounting_rows(report):
    """Allowlisted rendering of the local accounting engine."""
    dataset = report["dataset_id"]
    if not isinstance(dataset, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,199}", dataset):
        raise MarkdownError("invalid_dataset", "计量来源身份无效。")
    ledger, windows = [], []
    for field, kind in (("weekly", "natural_week"), ("monthly", "billing_month")):
        config = report.get(field) or {}
        for period in config.get("periods", []):
            totals = period.get("totals") or {}
            row = {"dataset_id": dataset, "kind": kind, "start": period["start"], "end": period["end"],
                   "statistics_until": period.get("observed_until") or report["generated_at"],
                   "scan_cutoff": report["generated_at"], "state": period.get("state"),
                   "sessions_count": period.get("sessions_count"), "projects": period.get("projects", []),
                   "models": period.get("models", []), "coverage": period.get("coverage", {}),
                   "quality": period.get("quality") or period.get("coverage", {}).get("quality") or ["unknown"],
                   "boundary_basis": period.get("boundary_basis") or ("natural_week" if kind == "natural_week" else "confirmed_anchor_date" if config.get("anchor_precision") == "date" else "confirmed_anchor_datetime")}
            row.update({counter: totals.get(counter, 0) for counter in COUNTERS})
            if kind == "billing_month":
                row["schedule_id"] = config.get("schedule_id")
            ledger.append(row)
    for window in (report.get("quota") or {}).get("windows", []):
        kind = {"5小时": "five_hour", "周额度": "weekly_quota"}.get(window.get("kind"), window.get("kind"))
        windows.append({"dataset_id": dataset, "kind": kind, "start": window["start"], "end": window["end"],
                        "statistics_until": window.get("observed_until") or report["generated_at"],
                        "scan_cutoff": (report.get("quota") or {}).get("statistics_until") or report["generated_at"],
                        "observed_at": window.get("last_observed_at") or window.get("observed_at"),
                        "duration_minutes": window.get("duration_mins", window.get("duration_minutes")),
                        "observations": window.get("observations"), "used_percent": window.get("used_percent"),
                        "remaining_percent": window.get("remaining_percent"), "first_used_percent": window.get("first_used_percent"),
                        "peak_used_percent": window.get("peak_used_percent"), "observed_increase_percent": window.get("observed_increase_percent"),
                        "reset_kind": window.get("reset_kind", "未确认"), "state": window.get("state"),
                        "notes": window.get("notes", []), "quality": window.get("status", "unknown"),
                        "boundary_basis": "duration_before_reset"})
    return ledger, windows


def _slug(value):
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "-", str(value)).strip(" .-") or "未命名"


def _ledger_label(props, project_names=None):
    if props["type"] == "usage_daily":
        project = (project_names or {}).get(props.get("project_id"), props.get("project_id") or "项目")
        return "每日投入 · " + str(props["day"]) + " · " + project
    local = _time(props["start"]).astimezone(SHANGHAI)
    label = {"natural_week": "自然周投入", "billing_month": "会员月投入",
             "five_hour": "5小时额度", "weekly_quota": "周额度"}[props["kind"]]
    return label + " · " + local.strftime("%Y-%m-%d" + (" %H:%M" if props["type"] == "quota_window" else ""))


def _ledger_destination(vault, props, project_names=None):
    label = _ledger_label(props, project_names)
    stem = _slug(label.replace(" · ", "-"))
    family = "每日" if props["type"] == "usage_daily" else "额度窗口" if props["type"] == "quota_window" else "周期"
    target = Path(vault) / "账本" / family / (stem + ".md")
    if target.is_file():
        existing, _ = read_note(target)
        if existing.get("id") != props.get("id"):
            target = target.with_name(stem + "-来源" + hashlib.sha256(str(props.get("dataset_id", "")).encode()).hexdigest()[:8] + ".md")
            if target.is_file() and read_note(target)[0].get("id") != props.get("id"):
                raise MarkdownError("ledger_name_conflict", "账本名称冲突，请保留文件并核对来源。")
    return target


def _normalise_ledger_names(vault):
    """Presentation migration only: retain identity and all accounting values."""
    rows = _records(vault)
    projects = {r["id"]: r.get("name") or Path(r["path"]).parent.name for r in rows if r["type"] == "project"}
    renamed = rewritten = 0
    for row in rows:
        if row["type"] not in ("usage_daily", "usage_period", "quota_window"):
            continue
        old_path = Path(vault) / row["path"]
        target = _ledger_destination(vault, row, projects)
        props, body = read_note(old_path)
        label = _ledger_label(props, projects)
        new_body = re.sub(r"^# [^\n]*", "# " + label, body, count=1)
        if props["type"] != "usage_daily":
            start = _time(props["start"]).astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M")
            end = _time(props["end"]).astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M")
            new_body = re.sub(r"开始：[^\n]*", "开始：" + start + "；结束：" + end + "（上海时间）。开始包含，结束不包含。", new_body, count=1)
        if props.get("name") != label or new_body != body:
            props.update(name=label, revision=props.get("revision", 0) + 1)
            write_note(old_path, props, new_body, expected_revision=row.get("revision"), expected_digest=row["digest"])
            rewritten += 1
        if old_path != target:
            target.parent.mkdir(parents=True, exist_ok=True)
            with _file_lock(old_path.parent / ".pm-write.lock"):
                if target.exists():
                    raise MarkdownError("ledger_name_conflict", "目标账本名称已存在，请重新读取后核对。")
                old_path.replace(target)
            renamed += 1
    return {"renamed": renamed, "rewritten": rewritten}


class MarkdownWorkspace:
    def __init__(self, data_dir=DEFAULT_DATA, vault=None, **kwargs):
        self.data_dir = Path(data_dir)
        self.config_path = self.data_dir / "markdown-config.json"
        self.state_path = self.data_dir / "markdown-state.json"
        self.config = _json(self.config_path, {})
        self.config.setdefault("bindings", {})
        settings = _json(self.data_dir / "app-settings.json", {})
        self.vault = Path(vault or self.config.get("vault_path") or settings.get("vault_path") or DEFAULT_VAULT).expanduser().resolve()
        self.state = _json(self.state_path, {})
        self._last_error = None

    def _begin_publication(self, state, family):
        if not state.get("publication_in_progress"):
            _save_json(self.data_dir / "markdown-last-complete.json", management_snapshot(self.vault))
        state["publication_in_progress"] = {"family": family, "started_at": now_iso()}
        _save_json(self.state_path, state)

    def _finish_publication(self, state):
        complete = management_snapshot(self.vault)
        state.pop("publication_in_progress", None)
        _save_json(self.data_dir / "markdown-last-complete.json", complete)
        _save_json(self.state_path, state)

    def rename_ledgers(self):
        with _ledger_publication(self.vault, self.data_dir), _file_lock(self.data_dir / "markdown.lock"):
            state = _json(self.state_path, {})
            self._begin_publication(state, "readable_names")
            result = _normalise_ledger_names(self.vault)
            self._finish_publication(state)
            return result

    def projects(self, force=False):
        return management_snapshot(self.vault, self.data_dir)["projects"]

    def cached_projects(self):
        return self.projects()

    def project_for_task(self, task_id):
        if _json(self.data_dir / "app-settings.json", {}).get("management_enabled", True) is not True:
            return None
        identity = self.config["bindings"].get(task_id)
        if not identity:
            return None
        return next((p for p in self.projects() if p["project_id"] == identity), None)

    def status(self):
        enabled = _json(self.data_dir / "app-settings.json", {}).get("management_enabled", True) is True
        try:
            projects = self.projects() if enabled else []
            error = None
        except (MarkdownError, OSError, ValueError) as exc:
            projects = []
            error = exc.public() if isinstance(exc, MarkdownError) else {"code": "read_failed", "message": "管理资料暂不可读。"}
        ready = self.vault.is_dir()
        return {"provider": "markdown", "ready": ready, "connected": ready and enabled and not error,
                "state": "error" if error else "connected" if ready and enabled else "disabled" if not enabled else "disconnected",
                "message": error["message"] if error else "Markdown 管理库已连接" if enabled and ready else "项目管理已关闭" if not enabled else "请选择项目管理库",
                "vault_path": str(self.vault), "bindings": dict(self.config["bindings"]),
                "binding_count": len(self.config["bindings"]), "project_count": len(projects),
                "projects_url": "obsidian://open?" + urlencode({"vault": self.vault.name, "file": "首页"}),
                "usage_url": "obsidian://open?" + urlencode({"vault": self.vault.name, "file": "总览/用量.base"}),
                "tasks_url": "obsidian://open?" + urlencode({"vault": self.vault.name, "file": "总览/人工事项.base"}),
                "last_synced_at": self.state.get("last_synced_at"), "error": error}

    def setup(self, vault_path=None, task_root=None):
        vault = Path(vault_path or self.vault).expanduser().resolve()
        if not vault.is_dir():
            raise MarkdownError("vault_missing", "请先创建或选择现有 Obsidian 管理库。")
        root = Path(task_root or Path.home() / "Documents").expanduser().resolve()
        if not root.is_dir():
            raise MarkdownError("root_missing", "项目根目录不存在。")
        with _file_lock(self.data_dir / "markdown.lock"):
            config = _json(self.config_path, {})
            if config.get("bindings") and config.get("vault_path") and Path(config["vault_path"]).resolve() != vault:
                raise MarkdownError("bound_vault", "已有绑定不能静默更换资料库。")
            config.update(version=1, vault_path=str(vault), task_root=str(root))
            config.setdefault("bindings", {})
            _save_json(self.config_path, config)
            settings = _json(self.data_dir / "app-settings.json", {})
            settings.pop("notion_sync_enabled", None)
            settings.update(vault_path=str(vault), task_root=str(root), management_provider="markdown", management_enabled=True)
            _save_json(self.data_dir / "app-settings.json", settings)
            self.config, self.vault = config, vault
        return self.status()

    def _task_id(self, command):
        task_id = command.get("task_id")
        if task_id:
            return _identity(task_id)
        folder = command.get("folder")
        if not folder:
            raise MarkdownError("missing_folder", "请选择本机项目文件夹。")
        root = Path(command.get("task_root") or self.config.get("task_root") or Path.home() / "Documents").expanduser().resolve()
        path = Path(folder).expanduser().resolve()
        if not path.is_dir() or path.parent != root:
            raise MarkdownError("invalid_folder", "项目工程必须是文稿根目录下的直接子文件夹。")
        from project_store import ProjectRegistry
        home = Path(command.get("codex_home") or os.environ.get("CODEX_HOME") or "~/.codex").expanduser()
        registry = ProjectRegistry(self.data_dir, home)
        task_id = registry.identify("folder:" + str(path), path.name)
        registry.save()
        return task_id

    def bind(self, command):
        project_id = _identity(command.get("project_id"))
        if not any(p["project_id"] == project_id for p in self.projects()):
            raise MarkdownError("unknown_project", "管理库中找不到此项目。")
        task_id = self._task_id(command)
        with _file_lock(self.data_dir / "markdown.lock"):
            config = _json(self.config_path, self.config)
            config.setdefault("bindings", {})[task_id] = project_id
            config.setdefault("vault_path", str(self.vault))
            _save_json(self.config_path, config)
            self.config = config
        return {"task_id": task_id, "project_id": project_id}

    def unbind(self, command):
        task_id = self._task_id(command)
        with _file_lock(self.data_dir / "markdown.lock"):
            config = _json(self.config_path, self.config)
            config.setdefault("bindings", {}).pop(task_id, None)
            _save_json(self.config_path, config)
            self.config = config
        return {"task_id": task_id, "unbound": True}

    def create(self, command):
        name = command.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise MarkdownError("invalid_name", "请输入简短项目名称。")
        identity = _identity(command.get("project_id") or "p-" + uuid.uuid4().hex)
        if any(p["project_id"] == identity for p in self.projects()):
            project = next(p for p in self.projects() if p["project_id"] == identity)
        else:
            safe_name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "-", name.strip())
            path = self.vault / "项目" / (safe_name + "-" + identity[-8:]) / "项目.md"
            created = now_iso()
            icon = command.get("icon") or "📁"
            if isinstance(icon, dict):
                icon = icon.get("emoji") or (icon.get("external") or {}).get("url")
            if not isinstance(icon, str) or not icon.strip():
                raise MarkdownError("invalid_icon", "项目图标需要 emoji 或 HTTPS 图片。")
            props = {"type": "project", "id": identity, "name": name.strip(), "status": "计划中",
                     "phase": "确认首版目标与范围", "priority": "中", "health": "正常", "icon": icon,
                     "progress": None, "progress_source": None, "next_step": "和 AI 确认首版要做什么，以及怎样判断完成。",
                     "revision": 1, "created": created, "updated": created, "cssclasses": ["pm-project"]}
            body = ("# " + name.strip() + "\n\n## 总目标\n\n" + (command.get("goal") or "待与用户确认。")
                    + "\n\n## 需要你决策\n\n![[总览/项目事项.base#待决策]]\n\n## 需要你验收\n\n![[总览/项目事项.base#待验收]]"
                    + "\n\n## 目前要做的事\n\n和 AI 确认首版要做哪些功能，完成后怎样检查。\n\n![[总览/项目状态.base#当前阶段与下一步]]\n\n### 总体路线\n\n确认首版目标与范围 → 验证关键可行性 → 完成核心使用流程 → 整体验收 → 发布试用并收集反馈\n")
            body += "\n### 当前行动\n\n![[总览/项目事项.base#当前行动]]\n\n> [!note]- 完整计划（含后续阶段）\n> ![[总览/项目事项.base#完整计划]]\n\n### 近期关键历史\n\n![[总览/项目历史.base#当前项目近期历史]]\n"
            write_note(path, props, body)
            project = next(p for p in self.projects() if p["project_id"] == identity)
        if command.get("task_id") or command.get("folder"):
            self.bind({**command, "project_id": identity})
        return project

    def import_manifest(self, command):
        # Validate a local allowlisted manifest before writing any project records.
        from project_import import validate_manifest
        folder = Path(command.get("folder") or "").expanduser().resolve()
        manifest = command.get("manifest")
        if isinstance(manifest, str):
            path = Path(manifest).expanduser().resolve()
            if path.stat().st_size > 2 * 1024 * 1024:
                raise MarkdownError("manifest_too_large", "导入清单过大。")
            manifest = json.loads(path.read_text())
        validated = validate_manifest(manifest, folder)
        project = validated.get("project", validated)
        task_id = self._task_id({**command, "folder": str(folder)})
        identity = command.get("project_id") or self.config["bindings"].get(task_id)
        if not identity:
            identity = "p-" + hashlib.sha256(task_id.encode()).hexdigest()[:24]
        result = self.create({"name": project["name"], "icon": project.get("icon", "📁"),
                              "goal": project.get("vision"), "project_id": identity,
                              "folder": str(folder), "task_root": command.get("task_root")})
        path = self.vault / result["path"]
        digest = note_digest(path)
        props, body = read_note(path)
        old_revision = props.get("revision")
        icon = project["icon"]
        props.update(name=project["name"], status=project["status"], health=project["health"],
                     icon=icon.get("emoji") or (icon.get("external") or {}).get("url"),
                     next_step=project["next_step"], summary=project["summary"])
        if project.get("progress"):
            progress = project["progress"]
            props.update(progress=progress["percent"], progress_source="ai", progress_evidence=progress["evidence"], progress_reviewed=progress["reviewed_at"])
        body = replace_section(body, "总目标", project["vision"])
        body = replace_section(body, "目前要做的事", project["current_goal"] + "\n\n" + project["summary"] + "\n\n下一步：" + project["next_step"])
        artifacts = []
        from project_manager import register_artifact
        for artifact in project["artifacts"]:
            registered = register_artifact(self.vault, identity, artifact["title"],
                source_root=folder if artifact.get("relative_path") else None,
                relative_path=artifact.get("relative_path"), url=artifact.get("url"),
                summary=artifact["result"], scope="清单登记，未在导入时重新验证或人工验收。", kind="document")
            artifacts.append((artifact["title"], _note_url(self.vault, self.vault / registered["path"])))
        if artifacts:
            props.update(latest_output_title=artifacts[0][0], latest_output_url=artifacts[0][1])
        for action in project["manual_actions"]:
            key = hashlib.sha256((identity + ":task:" + action["id"]).encode()).hexdigest()[:24]
            target = path.parent / "任务" / ("t-" + key + ".md")
            old_props, old_body = read_note(target) if target.is_file() else ({}, "")
            action_props = {**old_props, "type": "task", "id": "t-" + key, "project_id": identity,
                            "name": action["name"], "kind": "决策", "managed_by": "人类确认", "status": action["status"],
                            "evidence": action.get("evidence_url"), "revision": old_props.get("revision", 0) + 1, "updated": now_iso()}
            action_body = old_body or "# " + action["name"] + "\n"
            for heading, value in (("事项", action["decision"] or action["name"]), ("推荐方案", action["recommendation"]), ("影响", action["impact"])):
                action_body = replace_section(action_body, heading, value or "待补充。")
            write_note(target, action_props, action_body,
                       expected_revision=old_props.get("revision") if old_props else None,
                       expected_digest=note_digest(target) if old_props else None)
        for event in project["history"]:
            key = hashlib.sha256((identity + ":event:" + event["id"]).encode()).hexdigest()[:24]
            target = path.parent / "历史" / ("e-" + key + ".md")
            if target.is_file():
                old, _ = read_note(target)
                if old.get("id") != "e-" + key:
                    raise MarkdownError("duplicate_id", "开发事件身份冲突。")
                continue
            occurred = event.get("occurred_at") or now_iso()
            event_props = {"type": "event", "id": "e-" + key, "project_id": identity,
                           "name": event["title"], "occurred_at": occurred, "created_at": now_iso(),
                           "evidence": event.get("evidence_url"), "revision": 1}
            event_body = "# " + event["title"] + "\n\n## 结果\n\n" + event["result"]
            if not event.get("occurred_at"):
                event_body += "\n\n本次回填，原完成时间未核实。"
            write_note(target, event_props, event_body)
        props.update(revision=(old_revision or 0) + 1, updated=now_iso())
        write_note(path, props, body, expected_revision=old_revision, expected_digest=digest)
        return next(p for p in self.projects() if p["project_id"] == identity)

    def sync(self, rows, now=None, force=False, complete_dataset_ids=None, complete_until=None):
        """Publish idempotent absolute daily snapshots, including rebind audit."""
        timestamp = _time(now) if now is not None else datetime.now(UTC)
        cutoff = _time(complete_until).isoformat() if complete_until else timestamp.isoformat()
        if not isinstance(rows, list):
            raise MarkdownError()
        with _ledger_publication(self.vault, self.data_dir), _file_lock(self.data_dir / "markdown.lock"):
            self.config = _json(self.config_path, self.config)
            self.state = _json(self.state_path, {})
            incoming, groups = set(), defaultdict(list)
            for raw in rows:
                task = _identity(raw.get("task_id"))
                project_id = self.config["bindings"].get(task)
                if not project_id:
                    continue
                dataset = _identity(raw.get("dataset_id"))
                day = date.fromisoformat(raw.get("day", ""))
                if day > timestamp.astimezone(SHANGHAI).date():
                    raise MarkdownError("invalid_time", "日摘要不能位于未来。")
                row = {"project_id": project_id, "dataset_id": dataset, "day": day.isoformat(),
                       "provider": "Codex", "task_ids": [task], "statistics_until": _time(raw.get("statistics_until") or cutoff).isoformat(),
                       "observed_history": bool(raw.get("observed_history", False)), "quality": raw.get("quality") or ["unknown"]}
                for field in COUNTERS:
                    value = raw.get(field, 0)
                    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2 ** 53:
                        raise MarkdownError("invalid_counter", "计量字段无效。")
                    row[field] = value
                row["sessions_count"] = raw.get("sessions_count")
                row["_sessions"] = set(raw["session_keys"]) if isinstance(raw.get("session_keys"), list) else None
                key = (dataset, task, day.isoformat())
                if key in incoming:
                    raise MarkdownError("duplicate_summary", "同批次存在重复日摘要。")
                incoming.add(key)
                groups[(dataset, project_id, day.isoformat())].append(row)
            desired = {}
            for (dataset, project, day), items in groups.items():
                row = {"project_id": project, "dataset_id": dataset, "day": day, "provider": "Codex",
                       "statistics_until": max((r["statistics_until"] for r in items), key=_time),
                       "observed_history": any(r["observed_history"] for r in items),
                       "task_ids": sorted({t for r in items for t in r["task_ids"]}),
                       "quality": sorted({q for r in items for q in (r["quality"] if isinstance(r["quality"], list) else [r["quality"]])})}
                row.update({field: sum(r[field] for r in items) for field in COUNTERS})
                row["sessions_count"] = len(set().union(*(r["_sessions"] for r in items))) if all(r["_sessions"] is not None for r in items) else items[0]["sessions_count"] if len(items) == 1 else None
                if row["sessions_count"] is None:
                    row["quality"].append("sessions_union_unverified")
                key = hashlib.sha256(json.dumps([dataset, project, day, "Codex"], separators=(",", ":")).encode()).hexdigest()
                desired[key] = {"type": "usage_daily", "id": "d-" + key, "summary_key": key, "schema_version": 1,
                                **row, "included": True, "counted_tokens": row["total_tokens"]}
            records = _records(self.vault)
            existing = {r["summary_key"]: r for r in records if r["type"] == "usage_daily"}
            project_names = {r["id"]: r.get("name") or Path(r["path"]).parent.name
                             for r in records if r["type"] == "project"}
            self._begin_publication(self.state, "daily")
            changed = skipped = 0
            complete = set(complete_dataset_ids or [])
            complete_cutoffs = self.state.setdefault("complete_cutoffs", {})
            stale_datasets = set()
            for dataset in list(complete):
                if complete_cutoffs.get(dataset) and _time(cutoff) < _time(complete_cutoffs[dataset]):
                    complete.remove(dataset)
                    stale_datasets.add(dataset)
            desired = {key: row for key, row in desired.items() if row["dataset_id"] not in stale_datasets}
            watermarks = self.state.setdefault("daily_watermarks", {})
            for key, row in existing.items():
                if row.get("dataset_id") in complete and key not in desired and row.get("included") is True and _time(row.get("statistics_until")) <= _time(cutoff):
                    props, body = read_note(self.vault / row["path"])
                    props.update(included=False, counted_tokens=0, retirement_reason="项目绑定已调整；原计量保留审计。", synced_at=timestamp.isoformat())
                    body = re.sub(r"当前计入：[\d,]+", "当前计入：0", body)
                    body += "\n> [!info] 历史分配已停计\n> 项目绑定已调整，原数值保留审计，不再进入当前汇总。\n"
                    write_note(self.vault / row["path"], props, body, expected_revision=props.get("revision"), expected_digest=row.get("digest"))
                    changed += 1
            for key, row in desired.items():
                row["name"] = _ledger_label(row, project_names)
                old = existing.get(key)
                observed = watermarks.get(key) or (old or {}).get("statistics_until")
                if observed and _time(row["statistics_until"]) < _time(observed):
                    skipped += 1
                    continue
                watermarks[key] = row["statistics_until"]
                comparable = lambda value: {k: v for k, v in value.items() if k not in ("path", "url", "body", "digest", "statistics_until", "synced_at", "revision", "updated")}
                if old and comparable(old) == comparable(row):
                    skipped += 1
                    continue
                path = self.vault / old["path"] if old else self.vault / "账本" / "每日" / ("d-" + key + ".md")
                row.update(synced_at=timestamp.isoformat(), revision=(old.get("revision", 0) if old else 0) + 1)
                body = ("# " + row["name"] + "\n\n"
                        + f"项目：{row['project_id']}\n\n已记录 Token：{row['total_tokens']:,}；当前计入：{row['counted_tokens']:,}。\n\n"
                        + "本机保留记录的绝对摘要，不代表完整账户用量或最终账单。细分项不重复加入总数。\n\n"
                        + ("包含此前已观测历史；本次未全部重新复算。\n" if row["observed_history"] else "本次基于本机当前保留记录。\n"))
                write_note(path, row, body, expected_revision=old.get("revision") if old else None,
                           expected_digest=old.get("digest") if old else None)
                changed += 1
            for dataset in complete:
                complete_cutoffs[dataset] = cutoff
            self.state.update(last_synced_at=timestamp.isoformat())
            _normalise_ledger_names(self.vault)
            self._finish_publication(self.state)
        return {"updated": changed, "skipped": skipped, "pending": 0}

    def sync_periods(self, accounting, sync_windows=False):
        ledgers, windows = _accounting_rows(accounting)
        changed = skipped = 0
        with _ledger_publication(self.vault, self.data_dir), _file_lock(self.data_dir / "markdown.lock"):
            state = _json(self.state_path, {})
            schedules = state.setdefault("schedules", {})
            watermarks = state.setdefault("period_watermarks", {})
            currents = state.setdefault("window_currents", {})
            records = _records(self.vault)
            self._begin_publication(state, "periods")
            existing = {r.get("period_key"): r for r in records if r["type"] in ("usage_period", "quota_window")}
            new_schedule = accounting.get("monthly", {}).get("schedule_id")
            generated = accounting.get("generated_at") or now_iso()
            dataset = accounting["dataset_id"]
            old_schedule = schedules.get(dataset)
            if old_schedule and _time(generated) < _time(old_schedule["cutoff"]):
                new_schedule = old_schedule.get("schedule_id")
                ledgers = [r for r in ledgers if r["kind"] != "billing_month" or r.get("schedule_id") == new_schedule]
            elif accounting.get("monthly", {}).get("status") == "configured":
                schedules[dataset] = {"schedule_id": new_schedule, "cutoff": generated}
            for old in existing.values():
                if old.get("dataset_id") == dataset and old.get("kind") == "billing_month" and old.get("included") and old.get("schedule_id") != new_schedule and new_schedule:
                    props, body = read_note(self.vault / old["path"])
                    props.update(included=False, counted_tokens=0, retirement_reason="会员续费锚点已调整，原摘要保留审计。")
                    write_note(self.vault / old["path"], props, body, expected_revision=props.get("revision"), expected_digest=old.get("digest"))
                    changed += 1
            if sync_windows:
                for source in sorted(windows, key=lambda r: _time(r["start"])):
                    group = source["dataset_id"] + ":" + source["kind"]
                    prior = currents.get(group)
                    if (not prior or (_time(source["start"]) >= _time(prior["start"]) and _time(source["scan_cutoff"]) >= _time(prior["cutoff"]))):
                        currents[group] = {"start": source["start"], "cutoff": source["scan_cutoff"]}
                for old in list(existing.values()):
                    if old["type"] != "quota_window":
                        continue
                    current = currents.get(old.get("dataset_id", "") + ":" + old.get("kind", ""))
                    if current and _time(old["start"]) < _time(current["start"]) and old.get("state") in ("open", "进行中"):
                        props, body = read_note(self.vault / old["path"])
                        props.update(state="closed", replacement_observed_at=current["cutoff"],
                                     replacement_note="后续已观测到同类新窗口；旧末次读数不代表最终消耗，切换时刻未核实。")
                        body += "\n> [!info] 已由后续窗口替代\n> 旧末次读数保留审计，不能据此推断真实重置时刻或最终消耗。\n"
                        write_note(self.vault / old["path"], props, body, expected_revision=old.get("revision"), expected_digest=old.get("digest"))
                        existing[old["period_key"]] = {**props, "path": old["path"], "digest": note_digest(self.vault / old["path"]), "body": body}
                        changed += 1
            for source in ledgers + (windows if sync_windows else []):
                window = source["kind"] in ("five_hour", "weekly_quota")
                if source["kind"] == "billing_month" and new_schedule and source.get("schedule_id") != new_schedule:
                    continue
                if _time(source["start"]) >= _time(source["end"]):
                    raise MarkdownError("invalid_period", "周期开始必须早于结束。")
                identity = [source["dataset_id"], source["kind"], source["start"], source["end"], source.get("schedule_id")]
                key = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
                old = existing.get(key)
                cutoff = source.get("scan_cutoff") or source["statistics_until"]
                observed = watermarks.get(key) or (old or {}).get("scan_cutoff") or (old or {}).get("statistics_until")
                if observed and _time(cutoff) < _time(observed):
                    skipped += 1
                    continue
                watermarks[key] = cutoff
                if not window:
                    for field in COUNTERS:
                        value = source.get(field)
                        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2 ** 53:
                            raise MarkdownError("invalid_counter", "周期计量字段无效。")
                props = {"type": "quota_window" if window else "usage_period", "id": ("w-" if window else "r-") + key,
                         "period_key": key, "schema_version": 1, "included": True}
                props.update({k: v for k, v in source.items() if not isinstance(v, (dict, list))})
                props.update(start_iso=source["start"], end_iso=source["end"], quality=source.get("quality"))
                coverage = source.get("coverage") or {}
                props.update(coverage_status=coverage.get("status"), retained_logs_only=True,
                             complete_subscription_history=False, accepted_records=coverage.get("accepted_records"))
                if not window:
                    props["counted_tokens"] = source["total_tokens"]
                else:
                    for field in ("used_percent", "remaining_percent", "first_used_percent", "peak_used_percent", "observed_increase_percent"):
                        value = source.get(field)
                        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100):
                            raise MarkdownError("invalid_percent", "额度百分比必须为 0 到 100 或未知。")
                    props["notes"] = source.get("notes", [])
                    current = currents.get(source["dataset_id"] + ":" + source["kind"])
                    if current and _time(source["start"]) < _time(current["start"]):
                        props.update(state="closed", replacement_observed_at=current["cutoff"],
                                     replacement_note="后续已观测到同类新窗口；保留旧读数，最终使用量和切换时刻未核实。")
                props["name"] = _ledger_label(props)
                details = {field: source.get(field) for field in ("projects", "models", "coverage", "notes")}
                props["detail_digest"] = hashlib.sha256(json.dumps(details, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                comparable = lambda value: {k: v for k, v in value.items() if k not in ("path", "url", "body", "digest", "statistics_until", "scan_cutoff", "synced_at", "revision")}
                if old and comparable(old) == comparable(props):
                    skipped += 1
                    continue
                props.update(synced_at=now_iso(), revision=(old.get("revision", 0) if old else 0) + 1)
                path = self.vault / old["path"] if old else self.vault / "账本" / ("额度窗口" if window else "周期") / (props["id"] + ".md")
                label = {"natural_week": "自然周投入", "billing_month": "会员月投入", "five_hour": "5小时额度", "weekly_quota": "周额度"}[source["kind"]]
                local_start = _time(source["start"]).astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M")
                local_end = _time(source["end"]).astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M")
                body = f"# {props['name']}\n\n开始：{local_start}；结束：{local_end}（上海时间）。开始包含，结束不包含。\n\n"
                if window:
                    value = source.get("used_percent")
                    body += f"最近观测已用：{str(value) + '%' if value is not None else '未知'}。\n\n"
                    body += "\n".join("- " + str(note) for note in source.get("notes", [])) + "\n\n"
                    if props.get("replacement_note"):
                        body += props["replacement_note"] + "\n\n"
                else:
                    sessions = source.get("sessions_count")
                    body += f"已记录 Token：{source['total_tokens']:,}；会话：{sessions if sessions is not None else '未知'}。\n\n"
                    body += "周与月是同一批已接受记录的不同视图，不能相加。\n\n"
                body += "本机保留记录的摘要，不代表完整账户用量或最终账单。\n\n"
                for title, field, name_key in (("项目分布", "projects", "name"), ("模型分布", "models", "model")):
                    if source.get(field):
                        body += "## " + title + "\n\n| 名称 | Token | 会话 |\n| --- | ---: | ---: |\n"
                        for item in source[field]:
                            name = str(item.get(name_key) or "未知").replace("|", "\\|").replace("\n", " ")
                            body += f"| {name} | {item.get('total_tokens', 0):,} | {item.get('sessions_count', '未知')} |\n"
                        body += "\n"
                if coverage:
                    body += "## 统计边界\n\n仅涵盖本机仍保留且能够识别的日志。\n\n"
                    body += f"已接受计量记录：{coverage.get('accepted_records', '未知')}；读取质量：{coverage.get('status', '未知')}。\n"
                write_note(path, props, body, expected_revision=old.get("revision") if old else None, expected_digest=old.get("digest") if old else None)
                changed += 1
            state["last_synced_at"] = now_iso()
            _normalise_ledger_names(self.vault)
            self._finish_publication(state)
        return {"updated": changed, "skipped": skipped}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--vault", type=Path)
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise MarkdownError("input_too_large", "命令输入超过上限。")
        command = json.loads(raw or b'{"command":"status"}')
        if not isinstance(command, dict):
            raise MarkdownError()
        workspace = MarkdownWorkspace(args.data_dir, args.vault)
        action = command.get("command", "status")
        if action == "status":
            result = {"status": workspace.status()}
        elif action == "projects":
            result = {"projects": workspace.projects()}
        elif action == "setup":
            result = {"status": workspace.setup(command.get("vault_path"), command.get("task_root"))}
        elif action == "create":
            result = {"project": workspace.create(command)}
        elif action == "bind":
            result = workspace.bind(command)
        elif action == "unbind":
            result = workspace.unbind(command)
        elif action == "import":
            result = {"project": workspace.import_manifest(command)}
        elif action == "snapshot":
            result = management_snapshot(workspace.vault, workspace.data_dir)
        else:
            raise MarkdownError("invalid_command", "不支持的 Markdown 管理命令。")
        print(json.dumps({"ok": True, **result}, ensure_ascii=False))
        return 0
    except (MarkdownError, ValueError, OSError, KeyError, TypeError) as exc:
        error = exc.public() if isinstance(exc, MarkdownError) else {"code": "read_failed", "message": "管理资料读取或写入失败，请检查输入和目录。"}
        print(json.dumps({"ok": False, "error": error}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
