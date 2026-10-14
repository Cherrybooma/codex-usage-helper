"""Exact-time weekly and billing-month views of accepted local token facts.

This module reads no credentials and never exports conversation text. It uses
the scanner's single accepted-event path; periods regroup those facts, rather
than adding daily summaries or pretending a month contains four weeks.
"""
from __future__ import annotations

from bisect import bisect_right
from calendar import monthrange
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from codex_usage import COUNT_FIELDS, scan_usage
from project_store import ProjectRegistry, project_location

UTC = timezone.utc
ZONE = ZoneInfo("Asia/Shanghai")


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Period timestamps require an explicit timezone")
    return value.astimezone(ZONE)


def parse_billing_anchor(value: object) -> datetime | None:
    """Reject missing/naive/invalid anchors instead of guessing a billing day."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    try:
        return _aware(value)
    except (ValueError, TypeError):
        return None


def calendar_week_bounds(now: datetime) -> tuple[datetime, datetime]:
    local = _aware(now)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=local.weekday())
    return start, start + timedelta(days=7)


def _month_shift(year: int, month: int, offset: int) -> tuple[int, int]:
    shifted = year * 12 + month - 1 + offset
    year, month_zero = divmod(shifted, 12)
    return year, month_zero + 1


def billing_boundary(anchor: datetime, offset: int) -> datetime:
    """Clamp each month from the original day, never from a clamped boundary."""
    local = _aware(anchor)
    year, month = _month_shift(local.year, local.month, offset)
    day = min(local.day, monthrange(year, month)[1])
    return local.replace(year=year, month=month, day=day)


def billing_month_bounds(now: datetime, anchor: datetime) -> tuple[datetime, datetime]:
    local, original = _aware(now), _aware(anchor)
    offset = (local.year - original.year) * 12 + local.month - original.month
    start = billing_boundary(original, offset)
    if local < start:
        offset -= 1
        start = billing_boundary(original, offset)
    return start, billing_boundary(original, offset + 1)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _counts() -> dict:
    return dict.fromkeys(COUNT_FIELDS, 0)


def _period(kind: str, start: datetime, end: datetime, now: datetime, identity: str,
            boundary_basis: str) -> dict:
    # Include the original anchor identity even when two configurations happen
    # to share one clamped month, so an edited schedule never overwrites it.
    return {
        "key": f"{kind}:{identity}:{_iso(start)}:{_iso(end)}",
        "kind": kind, "start": _iso(start), "end": _iso(end),
        "period_start": _iso(start), "period_end": _iso(end),
        "label": f"{start:%Y-%m-%d} 至 {end:%Y-%m-%d}",
        "timezone": str(ZONE), "boundary_basis": boundary_basis,
        "state": "open" if now < end else "closed",
        "observed_until": _iso(min(now, end)),
        "totals": _counts(), "_sessions": set(), "_projects": {},
        "_models": {}, "_daily": {}, "_first": None, "_last": None,
    }


def _bucket(rows: dict, identity: str, field: str, name: str) -> dict:
    return rows.setdefault(identity, {field: name, **_counts(), "_sessions": set()})


def _add(period: dict, fact: dict, timestamp: datetime, project: tuple[str, str]) -> None:
    project_key, project_name = project
    project_row = _bucket(period["_projects"], project_key, "name", project_name)
    project_row["task_id"] = project_key
    model_row = _bucket(period["_models"], fact["model"], "model", fact["model"])
    day = timestamp.astimezone(ZONE).date().isoformat()
    day_row = _bucket(period["_daily"], day, "day", day)
    for bucket in (period["totals"], project_row, model_row, day_row):
        for field in COUNT_FIELDS:
            bucket[field] += fact["counts"][field]
    for bucket in (period, project_row, model_row, day_row):
        bucket["_sessions"].add(fact["thread_id"])
    period["_first"] = min(period["_first"], timestamp) if period["_first"] else timestamp
    period["_last"] = max(period["_last"], timestamp) if period["_last"] else timestamp


def _finish(period: dict, source_coverage: dict, warnings: list[str]) -> dict:
    period["sessions_count"] = len(period.pop("_sessions"))
    total = period["totals"]["total_tokens"]
    period["total_tokens"] = total
    for internal, public, label in (("_projects", "projects", "name"),
                                    ("_models", "models", "model"),
                                    ("_daily", "daily", "day")):
        rows = list(period.pop(internal).values())
        for row in rows:
            row["sessions_count"] = len(row.pop("_sessions"))
            if public == "projects":
                row["percent"] = row["total_tokens"] * 100 / total if total else 0
        period[public] = sorted(rows, key=(lambda row: row[label]) if public == "daily"
                                else (lambda row: (-row["total_tokens"], row[label])))
    first, last = period.pop("_first"), period.pop("_last")
    problematic = any(source_coverage.get(field, 0) for field in (
        "files_skipped", "malformed_lines", "missing_timestamps",
        "copied_responses_without_original")) or any(
        any(marker in warning for marker in ("; skipped", "usage omitted", "read failed",
            "could not be decoded", "No usable token", "original timestamp is unknown"))
        for warning in warnings)
    accepted = period["totals"]["request_records"] + period["totals"]["legacy_updates"]
    quality = (["partial_unreadable"] if problematic else [])
    if period["totals"]["request_records"]:
        quality.append("response_records_verified")
    if period["totals"]["legacy_updates"]:
        quality.append("legacy_estimated")
    if not accepted:
        quality.append("no_observed_usage")
    period["coverage"] = {
        "status": "partial_unreadable" if problematic else
                  "observed" if accepted else "no_observed_usage",
        "quality": quality, "retained_logs_only": True,
        "complete_subscription_history": False,
        "accepted_records": accepted,
        "first_usage_at": _iso(first) if first else None,
        "last_usage_at": _iso(last) if last else None,
        "source_files_found": source_coverage.get("files_found", 0),
        "source_files_scanned": source_coverage.get("files_scanned", 0),
        "source_files_skipped": source_coverage.get("files_skipped", 0),
    }
    return period


def build_period_report(home: Path, data_dir: Path | None, now: datetime,
                        billing_anchor: datetime | str | None = None,
                        task_root: Path | None = None,
                        registry: ProjectRegistry | None = None,
                        week_count: int = 12, month_count: int = 12,
                        billing_anchor_precision: str = "datetime") -> dict:
    """Regroup one scanner replay into exact-time natural weeks/billing months.

    Periods use [start,end); the open period is sampled only up to ``now``.
    Monthly history before the confirmed anchor extrapolates that schedule,
    and is explicitly labelled as such, not historical membership invoices.
    Unknown or invalid anchors leave monthly totals absent, never zero.
    Full-history replay retains old cumulative baselines before any period.
    """
    now = _aware(now)
    if not all(isinstance(count, int) and not isinstance(count, bool) and 1 <= count <= 120
               for count in (week_count, month_count)):
        raise ValueError("History counts must be integers between 1 and 120")
    if billing_anchor_precision not in ("date", "datetime"):
        raise ValueError("billing_anchor_precision must be date or datetime")
    anchor = parse_billing_anchor(billing_anchor)
    if anchor and billing_anchor_precision == "date":
        # A date-only setting deliberately means local midnight; it must not
        # silently imply that the payment happened at this exact time.
        anchor = anchor.replace(hour=0, minute=0, second=0, microsecond=0)
    if anchor and anchor > now:
        anchor = None
    source_key = hashlib.sha256(str(Path(home).expanduser().resolve()).encode()).hexdigest()
    # Reports are read-only views. They must not save the management registry:
    # a simultaneous weekly refresh or project rename owns those writes.
    # Do not instantiate ProjectRegistry for a report either: its constructor
    # assigns a random dataset ID for an unseen home, even without save().
    saved_registry = {"datasets": {}, "projects": {}}
    registry_path = registry.path if registry else (
        Path(data_dir) / "project-registry.json" if data_dir is not None else None)
    if registry_path is not None and registry_path.is_file():
        saved_registry = json.loads(registry_path.read_text(encoding="utf-8"))
        if (not isinstance(saved_registry, dict) or saved_registry.get("version") != 1
                or not all(isinstance(saved_registry.get(key), dict)
                           for key in ("datasets", "projects"))):
            raise ValueError("项目身份配置版本不支持，请保留原文件。")
    dataset_id = saved_registry["datasets"].get(source_key)
    dataset_id = dataset_id or "local:" + source_key
    schedule_id = hashlib.sha256((_iso(anchor) + ":" + billing_anchor_precision).encode()).hexdigest()[:20] if anchor else None
    week_start, week_end = calendar_week_bounds(now)
    weekly = [_period("calendar_week", week_start - timedelta(weeks=i),
                      week_end - timedelta(weeks=i), now, str(ZONE), "calendar_week")
              for i in range(week_count)]
    monthly = []
    if anchor:
        month_start, _ = billing_month_bounds(now, anchor)
        offset = (month_start.year - anchor.year) * 12 + month_start.month - anchor.month
        for i in range(month_count):
            start, end = billing_boundary(anchor, offset - i), billing_boundary(anchor, offset - i + 1)
            monthly.append(_period("billing_month", start, end, now, schedule_id,
                                   "confirmed_anchor" if start == anchor else "extrapolated_schedule"))
    # Each accepted fact can enter one week and one month. These independent
    # views must never be added together as if they were additional usage.
    indexes = []
    for periods in (weekly, monthly):
        ascending = list(reversed(periods))
        indexes.append(([datetime.fromisoformat(row["start"]) for row in ascending], ascending))
    locations = {}
    first_usage = last_usage = None
    accepted_count = 0

    def collect(fact: dict) -> None:
        nonlocal first_usage, last_usage, accepted_count
        stamp = datetime.fromisoformat(fact["timestamp"])
        first_usage = min(first_usage, stamp) if first_usage else stamp
        last_usage = max(last_usage, stamp) if last_usage else stamp
        accepted_count += 1
        matched = []
        for starts, periods in indexes:
            index = bisect_right(starts, stamp) - 1
            if index >= 0 and stamp < datetime.fromisoformat(periods[index]["end"]):
                matched.append(periods[index])
        if not matched:
            return
        path = fact["project"]
        if path not in locations:
            if task_root is None:
                key, name = path, "未知目录" if path == "unknown" else Path(path).name or path
            else:
                key, name = project_location(path, Path(task_root))
            known = saved_registry["projects"].get(key, {})
            task_id = known.get("task_id") or "local:" + hashlib.sha256(
                (dataset_id + ":period-project:" + key).encode()).hexdigest()
            locations[path] = (task_id, name)
        for period in matched:
            _add(period, fact, stamp, locations[path])

    scan = scan_usage(Path(home), until=now, timezone=ZONE, accepted_event_sink=collect)
    for period in weekly + monthly:
        period["dataset_id"] = dataset_id
        _finish(period, scan["coverage"], scan["warnings"])
    return {
        "schema_version": 1, "provider": "codex", "generated_at": _iso(now),
        "timezone": str(ZONE), "dataset_id": dataset_id,
        "boundary_rule": "start_inclusive_end_exclusive",
        "source": {
            "scope": "retained_local_logs", "coverage": scan["coverage"],
            "warnings": scan["warnings"], "totals": scan["totals"],
            "accepted_records": accepted_count,
            "first_usage_at": _iso(first_usage) if first_usage else None,
            "last_usage_at": _iso(last_usage) if last_usage else None,
            "statistics_until": _iso(now), "complete_subscription_history": False,
        },
        "weekly": {"status": "available", "policy": "calendar_week",
                   "current": weekly[0], "periods": weekly},
        "monthly": {"status": "configured" if anchor else "needs_anchor",
                    "policy": "billing_month", "anchor": _iso(anchor) if anchor else None,
                    "anchor_precision": billing_anchor_precision,
                    "schedule_id": schedule_id,
                    "anchor_note": "仅确认日期，按 Asia/Shanghai 00:00 作为核算边界；不是精确支付时刻。"
                                   if anchor and billing_anchor_precision == "date" else None,
                    "current": monthly[0] if monthly else None, "periods": monthly},
        "notes": ["Token 为本机保留记录的投入，不等于余额、收费或完整订阅历史。",
                  "周报与月核算是同一份记录的不同视图，不能把两者相加。",
                  "额度百分比使用独立的官方观测窗口，不从 Token 反推。"],
    }
