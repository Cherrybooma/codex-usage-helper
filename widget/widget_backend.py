"""Local token aggregation, read-only official quota, opt-in Notion summaries.

Never opens auth.json or reads credential environment variables. Authentication
belongs to the already-installed official Codex app-server. The sole upstream
method used here is account/rateLimits/read; no tasks or quota resets are sent.
Notion sync runs in a separate invocation and sends allowlisted daily summaries.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import queue
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import tempfile
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from codex_usage import scan_usage
from project_store import ProjectRegistry, aggregate, split_small, remember_daily, management_projects

DATA = Path(__file__).resolve().parent / "data"
UTC = timezone.utc


def group_tasks(projects: list[dict], task_root: Path) -> tuple[list[dict], list[dict]]:
    buckets: dict[str, dict] = {}
    task_root = task_root.expanduser().resolve()
    for project in projects:
        path = project["project"]
        if path == "unknown":
            name = "未知目录"
        else:
            try:
                relative = Path(path).resolve().relative_to(task_root)
                name = relative.parts[0] if relative.parts else "文稿根目录"
            except ValueError:
                name = "临时及其他目录"
        row = buckets.setdefault(name, {"name": name, "total_tokens": 0,
                                       "request_records": 0, "thread_ids": set()})
        row["total_tokens"] += project["total_tokens"]
        row["request_records"] += project["request_records"]
        row["thread_ids"].update(project.get("thread_ids", []))
    total = sum(row["total_tokens"] for row in buckets.values())
    rows = sorted(buckets.values(), key=lambda row: (-row["total_tokens"], row["name"]))
    for row in rows:
        row["percent"] = row["total_tokens"] * 100 / total if total else 0
        row["sessions_count"] = len(row.pop("thread_ids"))
    # Use unrounded shares. A task at exactly 5% remains visible.
    small = [row for row in rows if row["total_tokens"] * 100 < total * 5]
    major = [row for row in rows if row["total_tokens"] * 100 >= total * 5]
    if small:
        other_tokens = sum(row["total_tokens"] for row in small)
        major.append({"name": "其他", "total_tokens": other_tokens,
                      "percent": other_tokens * 100 / total if total else 0,
                      "request_records": sum(row["request_records"] for row in small),
                      "members_count": len(small), "is_other": True})
    return major, small


def normalize_limits(raw: dict, observed_at: datetime, source: str) -> dict:
    """Whitelist quota fields; discard account IDs, credits and auth data."""
    by_id = raw.get("rateLimitsByLimitId") or {}
    rate = by_id.get("codex") if isinstance(by_id, dict) else None
    rate = rate or raw.get("rateLimits") or {}
    if not isinstance(rate, dict):
        raise ValueError("No Codex quota bucket")
    windows = []
    for key in ("primary", "secondary"):
        value = rate.get(key)
        if not isinstance(value, dict):
            continue
        used, duration, reset = (value.get(name) for name in
                                 ("usedPercent", "windowDurationMins", "resetsAt"))
        if (isinstance(used, bool) or not isinstance(used, (int, float)) or
            not 0 <= used <= 100 or isinstance(duration, bool) or
            not isinstance(duration, int) or duration <= 0 or
            isinstance(reset, bool) or not isinstance(reset, (int, float))):
            continue
        windows.append({"key": key, "used_percent": float(used),
                        "remaining_percent": max(0, 100 - float(used)),
                        "duration_mins": duration, "resets_at": int(reset),
                        "label": "周窗口" if duration == 10080 else
                                 "5 小时窗口" if duration == 300 else f"{duration} 分钟窗口"})
    if not windows:
        raise ValueError("Official quota counters unavailable")
    plan = rate.get("planType")
    return {"observed_at": observed_at.astimezone(UTC).isoformat(),
            "source": source, "limit_id": rate.get("limitId") or "codex",
            "plan": plan if isinstance(plan, str) else None,
            "windows": windows}


def official_cli() -> str | None:
    # Prefer the native executable bundled with the desktop app; the npm shim
    # on this machine is broken and is not repaired by this application.
    for path in ("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
                 "/Applications/Codex.app/Contents/Resources/codex"):
        if Path(path).is_file():
            return path
    return shutil.which("codex")


def fetch_official_limits(timeout: float = 18) -> dict:
    executable = official_cli()
    if not executable:
        raise RuntimeError("找不到官方 Codex CLI，请先打开已登录的 Codex。")
    messages: queue.Queue = queue.Queue()
    process = subprocess.Popen([executable, "app-server"], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               text=True)

    def consume():
        for line in process.stdout:
            try:
                messages.put(json.loads(line))
            except ValueError:
                continue
        messages.put(None)

    threading.Thread(target=consume, daemon=True).start()

    def send(value):
        process.stdin.write(json.dumps(value, ensure_ascii=False) + "\n")
        process.stdin.flush()

    try:
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {
            "name": "local_usage_widget", "title": "用量小助手", "version": "0.1.0"}}})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                message = messages.get(timeout=max(.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if message is None:
                break
            if message.get("id") == 1:
                if "error" in message:
                    raise RuntimeError("官方服务初始化失败，已保留上次观测。")
                send({"method": "initialized", "params": {}})
                send({"id": 2, "method": "account/rateLimits/read", "params": {}})
            elif message.get("id") == 2:
                if "error" in message:
                    raise RuntimeError("官方额度读取失败，请确认 Codex 已登录。")
                return normalize_limits(message.get("result") or {}, datetime.now(UTC),
                                        "official_app_server")
        raise RuntimeError("官方额度读取超时，已保留上次观测。")
    finally:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdin.close()
        process.stdout.close()


def open_history(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE IF NOT EXISTS observations (
        observed REAL NOT NULL, window_key TEXT NOT NULL,
        duration INTEGER NOT NULL, reset INTEGER NOT NULL, used REAL NOT NULL,
        source TEXT NOT NULL, PRIMARY KEY(observed,window_key))""")
    db.execute("""CREATE TABLE IF NOT EXISTS window_changes (
        window_key TEXT NOT NULL,duration INTEGER NOT NULL,reset INTEGER NOT NULL,
        observed_change REAL NOT NULL,PRIMARY KEY(window_key,duration,reset))""")
    db.execute("""CREATE TABLE IF NOT EXISTS budget_anchor (
        id INTEGER PRIMARY KEY CHECK(id=1), anchor REAL NOT NULL,
        window_key TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS observation_context (
        observed REAL NOT NULL,window_key TEXT NOT NULL,plan TEXT,limit_id TEXT,
        PRIMARY KEY(observed,window_key))""")
    return db


def save_observation(db: sqlite3.Connection, quota: dict) -> None:
    observed = datetime.fromisoformat(quota["observed_at"]).timestamp()
    for window in quota["windows"]:
        # Stale snapshots are retained as display cache, never inserted into a
        # newer quota cycle or presented as a reset-end observation.
        if observed >= window["resets_at"]:
            continue
        # The real service returned the same weekly deadline with 1-second
        # jitter on consecutive reads. That must not create fictitious cycles.
        close_reset = db.execute("""SELECT reset FROM observations
            WHERE window_key=? AND duration=? AND ABS(reset-?)<=120 AND reset>?
            ORDER BY ABS(reset-?) LIMIT 1""",
            (window["key"], window["duration_mins"], window["resets_at"], observed,
             window["resets_at"])).fetchone()
        canonical_reset = close_reset[0] if close_reset else window["resets_at"]
        # An observed deadline change before the old deadline is NOT proof of
        # a scheduled reset, a user redemption, or unused allowance forfeiture.
        # Keep that transition explicitly separate from ordinary expirations.
        first_current = db.execute("SELECT MIN(observed) FROM observations WHERE window_key=? AND duration=? AND reset=?",
                                   (window["key"], window["duration_mins"], canonical_reset)).fetchone()[0]
        change_at = min(observed, first_current) if first_current else observed
        older = db.execute("""SELECT reset,MAX(observed) FROM observations
            WHERE window_key=? AND duration=? AND ABS(reset-?)>120
            GROUP BY reset""", (window["key"], window["duration_mins"], canonical_reset)).fetchall()
        for old_reset, last_seen in older:
            if last_seen < change_at < old_reset:
                db.execute("INSERT OR IGNORE INTO window_changes VALUES (?,?,?,?)",
                           (window["key"], window["duration_mins"], old_reset, change_at))
        db.execute("INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?)",
                   (observed, window["key"], window["duration_mins"],
                    canonical_reset, window["used_percent"], quota["source"]))
        db.execute("INSERT OR IGNORE INTO observation_context VALUES (?,?,?,?)",
                   (observed, window["key"], quota.get("plan"), quota.get("limit_id", "codex")))
    db.commit()


WEEK_SECONDS = 7 * 24 * 60 * 60
SAMPLE_GAP_SECONDS = 180


def calendar_week(now: datetime) -> tuple[datetime, datetime]:
    """Local Monday 00:00 through next Monday 00:00 (exclusive)."""
    local = now.astimezone(ZoneInfo("Asia/Shanghai"))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=local.weekday())
    return start, start + timedelta(days=7)


def weekly_budget(db: sqlite3.Connection, now: datetime, *, read_failed: bool = False) -> dict:
    """Accumulate observed usage against a fixed week, across early resets.

    No token-to-entitlement conversion. Positive counter deltas are additive;
    snapshots are not. Seed only a counter whose service window starts at our
    budget boundary. Crossing an unobserved boundary contributes no invented
    usage. Missing observations or ambiguous window changes stay explicit.
    """
    stamp = now.timestamp()
    rows = db.execute("""SELECT o.observed,o.window_key,o.reset,o.used,c.plan,c.limit_id
        FROM observations o LEFT JOIN observation_context c
        ON o.observed=c.observed AND o.window_key=c.window_key
        WHERE o.duration=10080 AND o.observed<=? ORDER BY o.observed,o.window_key""",
        (stamp,)).fetchall()
    if not rows:
        return {"status": "unavailable", "observed_total_percent": None,
                "excess_percent": None, "reset_count": 0,
                "notes": ["尚无周额度观测，暂无法确认超额。"]}
    week_start, week_end = calendar_week(now)
    start, end = week_start.timestamp(), week_end.timestamp()
    window_key = rows[0][1]
    reasons = []
    def incomplete(reason):
        if reason not in reasons:
            reasons.append(reason)
    previous = None
    total = 0.0
    resets = 0
    seen = False
    incompatible = False
    ambiguous = False
    for row in rows:
        observed, key, reset, used, plan, bucket = row
        if observed < start:
            previous = row
            continue
        if key != window_key or (bucket is not None and bucket != "codex"):
            incompatible = True
            incomplete("额度分类发生变化，无法换算为同一份正常周额度。")
        if not seen:
            # Seed only when the service window began inside this calendar
            # week. Otherwise its counter includes last week's usage; use the
            # first in-week observation as a baseline instead of importing it.
            service_start = reset - WEEK_SECONDS
            if start <= service_start <= observed + 120:
                total = used
                if service_start > start + SAMPLE_GAP_SECONDS:
                    incomplete("周初记录不完整，累计只包含已记录的消耗。")
            else:
                incomplete("周初缺少计数基线，未补算跨周间隔的消耗。")
            if observed > start + SAMPLE_GAP_SECONDS and (previous is None or previous[0] < start - SAMPLE_GAP_SECONDS):
                incomplete("本周开始时未连续采集，可能缺少更早的重置或消耗。")
            seen = True
        elif previous:
            old_time, old_key, old_reset, old_used, old_plan, old_bucket = previous
            if observed - old_time > SAMPLE_GAP_SECONDS:
                incomplete("存在超过 3 分钟的采集间隔，可能漏掉期间的重置。")
            if old_key != key or (old_plan and plan and old_plan != plan) or (old_bucket and bucket and old_bucket != bucket):
                incompatible = True
                incomplete("套餐或额度分类发生变化，无法换算为同一份正常周额度。")
            elif reset == old_reset and used >= old_used:
                total += used - old_used
            elif reset == old_reset:
                # A decrease without a new deadline might be a partial reset
                # or correction. Preserve prior use but do not add the new
                # baseline as a whole new allowance.
                ambiguous = True
                incomplete("已用比例下降但截止时间未变，变化原因未确认。")
            else:
                new_start = reset - WEEK_SECONDS
                # A fresh weekly window must begin between observations.
                # Otherwise a shifted deadline alone is not proof of a reset.
                if old_time - 120 <= new_start <= observed + 120 and new_start >= start:
                    total += used
                    if new_start < old_reset - 120:
                        resets += 1
                    # Even a close pair may miss the old counter's final use.
                    if old_used < 100 and observed > old_time:
                        incomplete("重置前末次观测并非最终消耗，累计值按已观测下限记录。")
                else:
                    ambiguous = True
                    incomplete("观测到窗口变化，无法确认重置时间或额度容量。")
        previous = row
    latest = rows[-1]
    if not seen:
        incomplete("本周尚无新观测。")
    if stamp - latest[0] > SAMPLE_GAP_SECONDS or latest[2] <= stamp or read_failed:
        incomplete("当前观测已过期或读取失败，等待恢复采集。")
    # These are quota-equivalent estimates even with continuous sampling:
    # the API reports percentages, not exact capacity or charged tokens.
    total = round(total, 6)
    valid_total = None if incompatible or not seen else total
    excess = (round(max(0, total - 100), 6) if valid_total is not None and
              (not reasons or (total > 100 and not ambiguous)) else None)
    return {"status": "incomplete" if reasons else "estimated",
            "period_start": start, "period_end": end,
            "observed_total_percent": valid_total, "excess_percent": excess,
            "reset_count": resets, "last_observed_at": latest[0],
            "notes": reasons,
            "basis": "一份正常周额度为 100%；按额度容量不变累计已观测消耗，不代表额外收费。"}


def cycle_history(db: sqlite3.Connection, now: datetime) -> list[dict]:
    result = []
    rows = db.execute("""SELECT window_key,duration,reset,MIN(observed),MAX(observed),
                         MAX(used),COUNT(*) FROM observations
                         GROUP BY window_key,duration,reset ORDER BY reset DESC""").fetchall()
    for key, duration, reset, first, last, peak, count in rows:
        change = db.execute("SELECT observed_change FROM window_changes WHERE window_key=? AND duration=? AND reset=?",
                            (key, duration, reset)).fetchone()
        ended_at = change[0] if change else reset
        if ended_at > now.timestamp():
            continue
        last_used = db.execute("SELECT used FROM observations WHERE window_key=? AND duration=? AND reset=? ORDER BY observed DESC LIMIT 1",
                               (key, duration, reset)).fetchone()[0]
        result.append({"key": key, "duration_mins": duration, "resets_at": reset,
                       "first_observed_at": first, "last_observed_at": last,
                       "last_used_percent": last_used, "peak_used_percent": peak,
                       "last_remaining_percent": max(0, 100 - last_used),
                       "minutes_before_reset": round((reset - last) / 60, 1),
                       "ended_at": ended_at,
                       "minutes_before_end_observation": round((ended_at - last) / 60, 1),
                       "reason": "window_changed" if change else "scheduled_deadline_passed",
                       "observations": count,
                       "status": "last_observed_only"})
    return result[:20]


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def build_snapshot(home: Path, task_root: Path, data: Path, refresh_official: bool = True,
                   now: datetime | None = None) -> dict:
    end = now or datetime.now(UTC)
    week_start, _ = calendar_week(end)
    report = scan_usage(home, since=week_start, until=end,
                        timezone=ZoneInfo("Asia/Shanghai"))
    registry = ProjectRegistry(data, home)
    metadata = {}
    workspace = None
    notion_status = {"connected": False, "state": "disconnected", "message": "尚未连接 Notion"}
    try:
        from notion_sync import NotionWorkspace, NotionError
        workspace = NotionWorkspace(data)
        # Cached project metadata only. Cloud I/O belongs to --sync-notion.
        for entry in registry.data["projects"].values():
            item = workspace.project_for_task(entry["task_id"])
            if item:
                metadata[entry["task_id"]] = item
        notion_status = workspace.status()
    except ImportError:
        pass
    except (OSError, ValueError, RuntimeError, NotionError):
        workspace = None
        notion_status = {"connected": False, "state": "error", "message": "Notion 配置暂不可读，本地统计正常"}
    grouping = registry.data.setdefault("grouping_roots", {})
    normalized_root = str(task_root.expanduser().resolve())
    previous_root = grouping.get(registry.dataset_id)
    if previous_root and previous_root != normalized_root and (workspace is None or notion_status.get("binding_count", 0)):
        raise ValueError("已绑定账本的项目根目录不能直接改变；请先解除绑定或使用独立 data-dir。")
    grouping[registry.dataset_id] = normalized_root
    all_tasks, daily_rows, local_projects = aggregate(report, task_root, registry, metadata)
    today = end.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    today_known = isinstance(report.get("project_daily"), list)
    today_tokens = sum(row.get("total_tokens", 0) for row in report.get("project_daily", []) if row["day"] == today) if today_known else None
    # Initial ids are assigned during aggregation; apply existing bindings on
    # the following pass without changing any count or its denominator.
    if workspace is not None:
        try:
            for entry in local_projects:
                if entry["task_id"] not in metadata:
                    item = workspace.project_for_task(entry["task_id"])
                    if item:
                        metadata[entry["task_id"]] = item
        except (OSError, ValueError, RuntimeError, NotionError):
            notion_status = {"connected": False, "state": "error", "message": "Notion 配置暂不可读，本地统计正常"}
        if metadata:
            all_tasks, daily_rows, local_projects = aggregate(report, task_root, registry, metadata)
    major, small = split_small(all_tasks, report["totals"]["total_tokens"])
    managed = None
    if workspace is not None and notion_status.get("connected"):
        try:
            cached_projects = workspace.cached_projects()
            if cached_projects:
                managed = management_projects(cached_projects, all_tasks, notion_status.get("bindings", {}), today_known=today_known)
        except (OSError, ValueError, RuntimeError, NotionError):
            # Presentation failure cannot alter the token ledger or its denominator.
            managed = None
    saved_daily = remember_daily(data / "daily-ledger.sqlite3", daily_rows, registry.dataset_id)
    atomic_json(data / "notion-daily.json", {"dataset_id": registry.dataset_id,
        "statistics_until": report["range"]["until_exclusive"], "rows": saved_daily})
    quota_file = data / "official-latest.json"
    quota = None
    quota_error = None
    if refresh_official:
        try:
            quota = fetch_official_limits()
            atomic_json(quota_file, quota)
        except (RuntimeError, ValueError, OSError) as error:
            quota_error = str(error)
    if quota is None and quota_file.is_file():
        quota = json.loads(quota_file.read_text(encoding="utf-8"))
    with closing(open_history(data / "quota-history.sqlite3")) as db:
        if quota:
            save_observation(db, quota)
        observed_now = now or datetime.now(UTC)
        history = cycle_history(db, observed_now)
        budget = weekly_budget(db, observed_now, read_failed=quota_error is not None)
    if quota:
        age = max(0, (end - datetime.fromisoformat(quota["observed_at"])).total_seconds())
        quota = {**quota, "age_seconds": round(age), "stale": age > 180 or
                 any(window["resets_at"] <= end.timestamp() for window in quota["windows"])}
    return {"generated_at": datetime.now(UTC).isoformat(), "range": report["range"],
            "period_mode": "calendar_week", "timezone": "Asia/Shanghai", "total_tokens": report["totals"]["total_tokens"],
            "request_records": report["totals"]["request_records"],
            "legacy_updates": report["totals"]["legacy_updates"],
            "today_tokens": today_tokens,
            "tasks": major, "other_tasks": small,
            "management_projects": managed,
            "dataset_id": registry.dataset_id, "available_projects": local_projects,
            "notion": notion_status,
            "coverage": {name: report["coverage"][name] for name in
                         ("files_found", "files_scanned", "files_skipped", "malformed_lines")},
            "warnings_count": len(report["warnings"]),
            "quota": quota, "quota_error": quota_error, "cycles": history,
            "weekly_budget": budget}


def main():
    import os
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", type=Path,
                        default=Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser())
    parser.add_argument("--task-root", type=Path)
    parser.add_argument("--data-dir", type=Path, default=DATA)
    parser.add_argument("--offline", action="store_true", help="Use only saved official snapshot")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--sync-notion", action="store_true", help="Sync saved summaries independently of local scanning")
    parser.add_argument("--force-sync", action="store_true", help="Bypass the Notion sync interval")
    args = parser.parse_args()
    try:
        if args.sync_notion:
            # Opt-in is persisted independently of any cached connection or
            # database configuration. Even a manual retry cannot bypass local
            # mode and provoke a Keychain prompt or a remote request.
            settings_file = args.data_dir / "app-settings.json"
            try:
                settings = json.loads(settings_file.read_text(encoding="utf-8")) if settings_file.is_file() else {}
            except (ValueError, OSError):
                settings = {}
            if not isinstance(settings, dict) or settings.get("notion_sync_enabled") is not True:
                print(json.dumps({"ok": False, "error": {"code": "local_mode",
                    "message": "当前为本机模式，Notion 同步未开启；本地统计继续可用。"}}, ensure_ascii=False))
                return 1
            from notion_sync import NotionWorkspace, NotionError
            try:
                workspace = NotionWorkspace(args.data_dir)
                if not workspace.status().get("ready"):
                    print(json.dumps({"ok": False, "error": {"message": "尚未配置 Notion；本地统计继续可用。"}}, ensure_ascii=False))
                    return 1
                workspace.projects(force=args.force_sync)
                source = args.data_dir / "notion-daily.json"
                saved = json.loads(source.read_text(encoding="utf-8")) if source.is_file() else {}
                rows = saved.get("rows", [])
                result = workspace.sync(rows, now=datetime.now(UTC), force=args.force_sync,
                                        complete_dataset_ids=[saved["dataset_id"]] if saved.get("dataset_id") else [],
                                        complete_until=saved.get("statistics_until"))
            except NotionError as error:
                print(json.dumps({"ok": False, "error": error.public()}, ensure_ascii=False))
                return 1
            okay = not result.get("error") and not result.get("projects_error")
            print(json.dumps({"ok": okay, "result": result, "error": result.get("error") or result.get("projects_error")}, ensure_ascii=False))
            return 0 if okay else 1
        settings_file = args.data_dir / "app-settings.json"
        settings = json.loads(settings_file.read_text(encoding="utf-8")) if settings_file.is_file() else {}
        args.task_root = args.task_root or Path(settings.get("task_root") or Path.home() / "Documents")
        result = build_snapshot(args.codex_home, args.task_root, args.data_dir, not args.offline)
        atomic_json(args.data_dir / "latest.json", result)
        if args.output:
            atomic_json(args.output, result)
        else:
            print(json.dumps(result, ensure_ascii=False))
    except (ValueError, OSError) as error:
        print(json.dumps({"error": f"本机用量读取失败：{type(error).__name__}"}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
