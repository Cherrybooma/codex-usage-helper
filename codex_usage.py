#!/usr/bin/env python3
"""Read Codex rollout metadata and report observed token usage by project.

Python 3.10+; no dependencies for JSONL/gzip. Zstandard rollouts optionally use
Python 3.14's compression.zstd, the zstandard package, or the zstd executable.
Source schema checked against openai/codex commit
39e013c8b7cb6eec46d16ff2b74fac4e22992e8c. Local formats are version-dependent.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
from pathlib import Path, PureWindowsPath
import shutil
import subprocess
import sys
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as dt_timezone, tzinfo
from typing import Iterator, TextIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
COUNT_FIELDS = TOKEN_FIELDS + ("request_records", "legacy_updates")
UTC = dt_timezone.utc


def local_timezone() -> tzinfo:
    """Prefer a zone with historical DST rules over today's fixed UTC offset."""
    candidates = [os.environ.get("TZ", "").lstrip(":")]
    try:
        target = str(Path("/etc/localtime").resolve())
        if "/zoneinfo/" in target:
            candidates.append(target.split("/zoneinfo/", 1)[1])
        if Path("/etc/timezone").is_file():
            candidates.append(Path("/etc/timezone").read_text().strip())
    except OSError:
        pass
    for candidate in candidates:
        if candidate:
            try:
                return ZoneInfo(candidate)
            except (ZoneInfoNotFoundError, ValueError):
                continue
    return datetime.now().astimezone().tzinfo or UTC


def parse_timestamp(value: object, zone: tzinfo = UTC) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=zone)
    return result.astimezone(UTC)


@dataclass
class UsageEvent:
    thread_id: str
    timestamp: datetime | None
    cwd: str | None
    model: str | None
    turn_id: str | None
    values: dict[str, int]
    kind: str
    location: str
    response_id: str | None = None
    last_values: dict[str, int] | None = None
    missing_fields: tuple[str, ...] = ()
    native: bool = True
    context_known: bool = False
    source_file: str = ""
    file_created_at: datetime | None = None
    line_number: int = 0


@dataclass
class Thread:
    thread_id: str
    cwd: str | None = None
    created_at: datetime | None = None
    forked_from_id: str | None = None
    parent_thread_id: str | None = None
    files: set[str] = field(default_factory=set)
    events: list[UsageEvent] = field(default_factory=list)


@dataclass
class TurnContext:
    cwd: str | None
    model: str | None
    file_created_at: datetime | None
    source_file: str
    line_number: int


@contextmanager
def open_rollout(path: Path) -> Iterator[TextIO]:
    """Stream compressed files without changing Codex's state."""
    if path.name.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            yield stream
        return
    if path.name.endswith(".zst"):
        try:
            from compression import zstd as builtin_zstd
        except ImportError:
            builtin_zstd = None
        if builtin_zstd is not None:
            with builtin_zstd.open(path, "rt", encoding="utf-8") as stream:
                yield stream
            return
        try:
            import zstandard
        except ImportError:
            zstandard = None
        if zstandard is not None:
            with path.open("rb") as source:
                with zstandard.ZstdDecompressor().stream_reader(source) as reader:
                    with io.TextIOWrapper(reader, encoding="utf-8") as stream:
                        yield stream
            return
        executable = shutil.which("zstd")
        if executable is None:
            raise OSError(
                "zstd support unavailable; install zstandard or the zstd executable"
            )
        with subprocess.Popen(
            [executable, "-q", "-d", "-c", "--", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ) as process:
            assert process.stdout is not None
            try:
                with io.TextIOWrapper(process.stdout, encoding="utf-8") as stream:
                    yield stream
            finally:
                if process.wait() != 0:
                    raise OSError("zstd decompression failed")
        return
    with path.open("r", encoding="utf-8") as stream:
        yield stream


def rollout_files(home: Path) -> list[Path]:
    result: set[Path] = set()
    for directory in (home / "sessions", home / "archived_sessions"):
        if directory.is_dir():
            for pattern in ("*.jsonl", "*.jsonl.gz", "*.jsonl.zst"):
                result.update(directory.rglob(pattern))
    # Codex also prefers the plain file while compression is in progress.
    result = {
        path for path in result
        if not (
            path.name.endswith((".gz", ".zst"))
            and path.with_suffix("").is_file()
        )
    }
    return sorted(result, key=str)


def read_values(
    raw: object,
    location: str,
    warnings: list[str],
) -> tuple[dict[str, int], tuple[str, ...]] | None:
    if not isinstance(raw, dict):
        warnings.append(f"{location}: missing usage object; skipped")
        return None
    if not any(name in raw for name in ("input_tokens", "output_tokens", "total_tokens")):
        warnings.append(f"{location}: no token counters; skipped")
        return None
    result: dict[str, int] = {}
    missing: list[str] = []
    for name in TOKEN_FIELDS:
        value = raw.get(name)
        if value is None:
            missing.append(name)
            result[name] = 0
        elif isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            result[name] = value
        else:
            warnings.append(f"{location}: invalid {name}; skipped")
            return None
    if "total_tokens" in missing:
        if "input_tokens" in missing or "output_tokens" in missing:
            warnings.append(f"{location}: cannot determine total_tokens; skipped")
            return None
        result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
    return result, tuple(missing)


def string_value(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def read_file(
    path: Path,
    threads: dict[str, Thread],
    warnings: list[str],
    coverage: dict[str, object],
    turn_contexts: dict[tuple[str, str], list[TurnContext]],
) -> None:
    current: Thread | None = None
    cwd: str | None = None
    model: str | None = None
    turn_id: str | None = None
    context_turn_id: str | None = None
    has_context = False
    file_created_at: datetime | None = None
    seen_turn_contexts: dict[str, tuple[str | None, str | None]] = {}
    with open_rollout(path) as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            location = f"{path}:{line_number}"
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                warnings.append(f"{location}: invalid or unfinished JSON line; skipped")
                coverage["malformed_lines"] += 1
                continue
            if not isinstance(record, dict):
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            kind = record.get("type")
            if kind == "session_meta":
                # Fork history can include the parent's SessionMeta after
                # the new thread's header. Codex treats the FIRST as canonical.
                if current is not None:
                    continue
                thread_id = string_value(payload.get("id"))
                if thread_id is None:
                    warnings.append(f"{location}: session metadata has no id")
                    continue
                current = threads.setdefault(thread_id, Thread(thread_id))
                current.files.add(str(path))
                cwd = string_value(payload.get("cwd"))
                current.cwd = cwd or current.cwd
                file_created_at = parse_timestamp(
                    payload.get("timestamp") or record.get("timestamp")
                )
                if file_created_at and (
                    current.created_at is None or file_created_at < current.created_at
                ):
                    current.created_at = file_created_at
                current.forked_from_id = current.forked_from_id or string_value(
                    payload.get("forked_from_id")
                )
                current.parent_thread_id = current.parent_thread_id or string_value(
                    payload.get("parent_thread_id")
                )
                model = None
                turn_id = None
                has_context = False
                continue
            if kind == "turn_context":
                cwd = string_value(payload.get("cwd")) or cwd
                model = string_value(payload.get("model")) or model
                turn_id = string_value(payload.get("turn_id")) or turn_id
                context_turn_id = turn_id
                has_context = True
                if current is not None and turn_id:
                    seen_turn_contexts[turn_id] = (cwd, model)
                    turn_contexts.setdefault((current.thread_id, turn_id), []).append(
                        TurnContext(cwd, model, file_created_at, str(path), line_number)
                    )
                continue
            if kind == "event_msg" and payload.get("type") in (
                "task_started", "turn_started",
            ):
                turn_id = string_value(payload.get("turn_id")) or turn_id
                continue
            is_response = kind == "token_usage_record"
            is_legacy = kind == "event_msg" and payload.get("type") == "token_count"
            if not is_response and not is_legacy:
                continue
            if is_legacy:
                info = payload.get("info")
                if info is None:  # Rate-limit-only notification.
                    continue
                if not isinstance(info, dict):
                    warnings.append(f"{location}: invalid token_count info; skipped")
                    continue
                parsed = read_values(info.get("total_token_usage"), location, warnings)
                last_parsed = (
                    read_values(info["last_token_usage"], location, warnings)
                    if isinstance(info.get("last_token_usage"), dict)
                    else None
                )
                last_values = last_parsed[0] if last_parsed else None
            else:
                parsed = read_values(payload.get("usage"), location, warnings)
                last_values = None
            if parsed is None:
                continue
            thread_id = (
                string_value(payload.get("thread_id")) if is_response else None
            ) or (current.thread_id if current else None)
            if thread_id is None:
                warnings.append(f"{location}: usage has no thread metadata; skipped")
                coverage["unattributed_records"] += 1
                continue
            owner = threads.setdefault(thread_id, Thread(thread_id))
            owner.files.add(str(path))
            native = current is None or current.thread_id == thread_id
            if native:
                owner.cwd = owner.cwd or cwd
            values, missing = parsed
            event_turn_id = (
                string_value(payload.get("turn_id")) if is_response else None
            ) or turn_id
            context_known = has_context and event_turn_id == context_turn_id
            event_cwd = cwd if context_known or event_turn_id == turn_id else owner.cwd
            event_model = model if context_known else None
            if event_turn_id and not context_known and native:
                context = seen_turn_contexts.get(event_turn_id)
                if context:
                    event_cwd = context[0] or event_cwd
                    event_model = context[1]
                    context_known = True
            event = UsageEvent(
                thread_id=thread_id,
                timestamp=parse_timestamp(record.get("timestamp")),
                cwd=event_cwd or owner.cwd,
                model=event_model,
                turn_id=event_turn_id,
                values=values,
                kind="response" if is_response else "legacy",
                location=location,
                response_id=string_value(payload.get("response_id")) if is_response else None,
                last_values=last_values,
                missing_fields=missing,
                native=native,
                context_known=context_known,
                source_file=str(path),
                file_created_at=file_created_at,
                line_number=line_number,
            )
            owner.events.append(event)
    coverage["files_scanned"] += 1


def normalize_cwd(cwd: str) -> str:
    if PureWindowsPath(cwd).is_absolute() and os.name != "nt":
        return str(PureWindowsPath(cwd))
    return str(Path(cwd).expanduser().resolve())


def project_for(cwd: str | None, group_worktrees: bool = True) -> str:
    if not cwd:
        return "unknown"
    directory = normalize_cwd(cwd)
    if PureWindowsPath(directory).is_absolute() and os.name != "nt":
        return directory
    try:
        result = subprocess.run(
            ["git", "-C", directory, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=3, check=False,
        )
        if result.returncode != 0:
            return directory
        root = result.stdout.strip()
        if group_worktrees:
            common = subprocess.run(
                ["git", "-C", directory, "rev-parse", "--git-common-dir"],
                capture_output=True, text=True, timeout=3, check=False,
            )
            if common.returncode == 0:
                common_path = Path(common.stdout.strip())
                if not common_path.is_absolute():
                    common_path = Path(directory) / common_path
                common_path = common_path.resolve()
                if common_path.name == ".git":
                    return str(common_path.parent)
        return normalize_cwd(root)
    except (OSError, subprocess.TimeoutExpired):
        return directory


def signature(event: UsageEvent, include_timestamp: bool = True) -> tuple:
    values = tuple(event.values[name] for name in TOKEN_FIELDS)
    return (event.timestamp, values) if include_timestamp else values


def empty_counts() -> dict[str, int]:
    return dict.fromkeys(COUNT_FIELDS, 0)


def scan_usage(
    codex_home: Path,
    since: datetime | None = None,
    until: datetime | None = None,
    timezone: tzinfo | None = None,
    group_worktrees: bool = True,
) -> dict:
    home = Path(codex_home).expanduser().resolve()
    if not home.is_dir():
        raise ValueError(f"Codex home does not exist: {home}")
    zone = timezone or local_timezone()
    if since:
        since = (since if since.tzinfo else since.replace(tzinfo=zone)).astimezone(UTC)
    if until:
        until = (until if until.tzinfo else until.replace(tzinfo=zone)).astimezone(UTC)
    if since and until and since >= until:
        raise ValueError("since must be earlier than until")
    warnings: list[str] = []
    coverage = {
        "files_found": 0, "files_scanned": 0, "files_skipped": 0,
        "malformed_lines": 0, "unattributed_records": 0,
        "duplicate_response_records": 0, "duplicate_legacy_snapshots": 0,
        "inherited_legacy_snapshots": 0, "legacy_resets": 0,
        "missing_timestamps": 0, "missing_fields": {},
        "legacy_fork_records_skipped": 0, "legacy_synthetic_snapshots": 0,
        "legacy_snapshots_superseded": 0, "copied_responses_without_original": 0,
        "rewritten_legacy_snapshots": 0,
    }
    threads: dict[str, Thread] = {}
    turn_contexts: dict[tuple[str, str], list[TurnContext]] = {}
    paths = rollout_files(home)
    coverage["files_found"] = len(paths)
    if not paths:
        warnings.append("No rollout files found in sessions or archived_sessions")
    for path in paths:
        try:
            read_file(path, threads, warnings, coverage, turn_contexts)
        except (OSError, UnicodeError, EOFError) as error:
            coverage["files_skipped"] += 1
            warnings.append(f"{path}: read failed ({type(error).__name__}: {error})")
        except Exception as error:
            if type(error).__module__.startswith(("zstandard", "compression.zstd")):
                coverage["files_skipped"] += 1
                warnings.append(f"{path}: compressed rollout could not be decoded")
            else:
                raise
    coverage["usage_records_read"] = sum(len(thread.events) for thread in threads.values())
    if paths and not coverage["usage_records_read"]:
        warnings.append(
            "No usable token usage records found; retained logs may be empty, "
            "contain only activity/rate limits, or use an unsupported schema"
        )

    projects: dict[str, dict] = {}
    sessions: dict[str, dict] = {}
    models: dict[str, dict] = {}
    daily: dict[str, dict] = {}
    project_daily: dict[tuple[str, str], dict] = {}
    totals = empty_counts()
    canonical_responses: dict[str, UsageEvent] = {}
    project_cache: dict[str | None, str] = {}
    physical_order = lambda item: (
        item.file_created_at or datetime.max.replace(tzinfo=UTC),
        item.source_file, item.line_number,
    )
    for contexts in turn_contexts.values():
        contexts.sort(key=physical_order)
    for thread in threads.values():
        # Files may live under either archive directory. Replay/revert can
        # also leave multiple physical rollouts for one logical thread.
        # Timestamps can be absent or move backwards with the host clock.
        # Preserve physical row order inside every file for delta/cutover logic.
        thread.events.sort(key=physical_order)
        for event in thread.events:
            if not event.context_known:
                # Only preceding contexts from this thread can supply a
                # cross-file fallback; a later change must not rewrite usage.
                for context in reversed(turn_contexts.get((event.thread_id, event.turn_id), [])):
                    if physical_order(context) <= physical_order(event):
                        event.cwd = context.cwd or event.cwd
                        event.model = context.model or event.model
                        break
            if event.kind != "response" or event.response_id is None:
                continue
            original = canonical_responses.get(event.response_id)
            if original is None:
                canonical_responses[event.response_id] = event
                continue
            coverage["duplicate_response_records"] += 1
            if original.values != event.values:
                warnings.append(f"{event.location}: conflicting usage for response_id")
            # A fork copies history and may rewrite outer timestamps. Prefer
            # the source thread's own file, then its earliest original record.
            rank = lambda item: (
                not item.native,
                item.timestamp or datetime.max.replace(tzinfo=UTC),
            )
            if rank(event) < rank(original):
                canonical_responses[event.response_id] = event

    def attribute(event: UsageEvent, values: dict[str, int], kind: str) -> None:
        if event.timestamp is None:
            coverage["missing_timestamps"] += 1
            warnings.append(f"{event.location}: missing/invalid timestamp")
            if since or until:
                return
        elif (since and event.timestamp < since) or (until and event.timestamp >= until):
            return
        if event.cwd not in project_cache:
            project_cache[event.cwd] = project_for(event.cwd, group_worktrees)
        project = project_cache[event.cwd]
        model_name = event.model or "unknown"
        day = event.timestamp.astimezone(zone).date().isoformat() if event.timestamp else "unknown"
        project_row = projects.setdefault(project, {
            "project": project, **empty_counts(), "thread_ids": set(),
        })
        session_row = sessions.setdefault(event.thread_id, {
            "thread_id": event.thread_id, **empty_counts(),
            "projects": set(), "models": set(),
            "parent_thread_id": threads[event.thread_id].parent_thread_id,
            "forked_from_id": threads[event.thread_id].forked_from_id,
        })
        model_row = models.setdefault(model_name, {"model": model_name, **empty_counts()})
        day_row = daily.setdefault(day, {"day": day, **empty_counts()})
        # Keep this joint grouping on the accepted-event path: separate project
        # and daily totals cannot recover which project incurred each day's use.
        project_day_row = project_daily.setdefault((day, project), {
            "project": project, "day": day, **empty_counts(), "thread_ids": set(),
        })
        for bucket in (totals, project_row, session_row, model_row, day_row, project_day_row):
            for name in TOKEN_FIELDS:
                bucket[name] += values[name]
            bucket["request_records" if kind == "response" else "legacy_updates"] += 1
        project_row["thread_ids"].add(event.thread_id)
        project_day_row["thread_ids"].add(event.thread_id)
        session_row["projects"].add(project)
        session_row["models"].add(model_name)
        for name in event.missing_fields:
            missing = coverage["missing_fields"]
            missing[name] = missing.get(name, 0) + 1

    # Scan every event, including events outside the reporting window, so legacy
    # cumulative counters retain their baseline across days.
    for thread_id in sorted(threads):
        thread = threads[thread_id]
        ancestor_signatures: set[tuple] = set()
        visited = {thread_id}
        parent = thread.forked_from_id
        fork_parent_available = not parent
        while parent:
            if parent in visited:
                warnings.append(f"Thread {thread_id}: cyclic fork ancestry")
                break
            visited.add(parent)
            ancestor = threads.get(parent)
            if ancestor is None:
                warnings.append(f"Thread {thread_id}: fork parent {parent} not found")
                break
            fork_parent_available = True
            ancestor_signatures.update(
                signature(event, include_timestamp=False)
                for event in ancestor.events
                if event.kind == "legacy" and (
                    not thread.created_at or not event.timestamp
                    or event.timestamp <= thread.created_at
                )
            )
            parent = ancestor.forked_from_id
        previous: tuple[dict[str, int], set[str]] | None = None
        seen_snapshots: set[tuple] = set()
        seen_file_vectors: dict[tuple, str] = {}
        modern_seen = False
        inherited_prefix = bool(thread.forked_from_id)
        matched_inheritance = False
        if thread.forked_from_id and not fork_parent_available:
            warnings.append(
                f"Thread {thread_id}: legacy fork parent unavailable; legacy usage omitted"
            )
        for event in thread.events:
            if event.kind == "response":
                if event.response_id is None:
                    warnings.append(f"{event.location}: response has no response_id; skipped")
                    continue
                modern_seen = True
                if canonical_responses[event.response_id] is not event:
                    continue
                if not event.native:
                    coverage["copied_responses_without_original"] += 1
                    warnings.append(
                        f"{event.location}: only a copied response record is available; "
                        "its original timestamp is unknown"
                    )
                    event.timestamp = None
                attribute(event, event.values, "response")
                continue
            # A derived total remains usable, whereas unavailable input/output
            # or subcategories cannot supply a cumulative-delta baseline.
            missing_counters = set(event.missing_fields) - {"total_tokens"}
            baseline = (event.values, missing_counters)
            snapshot_signature = signature(event)
            if snapshot_signature in seen_snapshots:
                coverage["duplicate_legacy_snapshots"] += 1
                continue
            seen_snapshots.add(snapshot_signature)
            replay_key = (event.turn_id, signature(event, include_timestamp=False))
            original_file = seen_file_vectors.get(replay_key)
            if original_file is not None and original_file != event.source_file:
                # Revert/replay can copy a logical thread to another rollout
                # and rewrite every timestamp without new consumption.
                previous = baseline
                coverage["rewritten_legacy_snapshots"] += 1
                continue
            seen_file_vectors.setdefault(replay_key, event.source_file)
            if thread.forked_from_id and not fork_parent_available:
                coverage["legacy_fork_records_skipped"] += 1
                continue
            inherited = inherited_prefix and (
                signature(event, include_timestamp=False) in ancestor_signatures
            )
            if inherited:
                previous = baseline
                coverage["inherited_legacy_snapshots"] += 1
                matched_inheritance = True
                continue
            if inherited_prefix:
                inherited_prefix = False
                if not matched_inheritance and not modern_seen:
                    # Parent exists, but no identifiable copied prefix was
                    # retained. The first snapshot may include parent usage.
                    previous = baseline
                    coverage["legacy_fork_records_skipped"] += 1
                    warnings.append(
                        f"{event.location}: legacy fork baseline unknown; first snapshot "
                        "used as baseline only"
                    )
                    continue
            if modern_seen:
                previous = baseline
                coverage["legacy_snapshots_superseded"] += 1
                continue
            if (
                event.values["total_tokens"] > 0
                and not any(event.values[name] for name in TOKEN_FIELDS[:-1])
                and "input_tokens" not in event.missing_fields
                and "output_tokens" not in event.missing_fields
            ):
                # fill_to_context_window fabricates a total-only snapshot on
                # a context-limit error. It is not a model response's usage.
                previous = baseline
                coverage["legacy_synthetic_snapshots"] += 1
                warnings.append(
                    f"{event.location}: total-only legacy context snapshot; baseline only"
                )
                continue
            if previous is None:
                values = event.values.copy()
            elif any(
                event.values[name] < previous[0][name]
                for name in ("input_tokens", "output_tokens", "total_tokens")
                if name not in missing_counters and name not in previous[1]
            ):
                coverage["legacy_resets"] += 1
                warnings.append(
                    f"{event.location}: legacy counters reset; new baseline established, "
                    "reset event not counted"
                )
                previous = baseline
                continue
            else:
                unavailable = missing_counters | previous[1]
                uncertain = set()
                values = {}
                for name in TOKEN_FIELDS:
                    if name in unavailable or event.values[name] < previous[0][name]:
                        values[name] = 0
                        if name not in missing_counters:
                            uncertain.add(name)
                    else:
                        values[name] = event.values[name] - previous[0][name]
                if uncertain:
                    # Once a counter reappears, its first value only restores
                    # the baseline; it may include usage during the gap.
                    event.missing_fields = tuple(sorted(set(event.missing_fields) | uncertain))
                    warnings.append(
                        f"{event.location}: legacy counter delta unavailable for "
                        f"{', '.join(sorted(uncertain))}; baseline only for these counters"
                    )
            previous = baseline
            if not any(values.values()):
                coverage["duplicate_legacy_snapshots"] += 1
                continue
            attribute(event, values, "legacy")
    if totals["legacy_updates"]:
        warnings.append(
            "Legacy token_count deltas are fallback estimates; context resets, missing "
            "history, and forks can make these differ from billed/observed response usage"
        )
    if coverage["missing_fields"]:
        warnings.append(
            "Some counters or legacy deltas were unavailable and are represented as 0; see "
            "coverage.missing_fields. Missing totals were derived from input + output"
        )
    project_rows = []
    for row in projects.values():
        row["sessions_count"] = len(row["thread_ids"])
        row["thread_ids"] = sorted(row["thread_ids"])
        project_rows.append(row)
    for row in sessions.values():
        row["projects"] = sorted(row["projects"])
        row["models"] = sorted(row["models"])
    project_daily_rows = []
    for key in sorted(project_daily):
        row = project_daily[key]
        # Session counts are distinct within a project/day. A weekly count must
        # union these IDs rather than sum days when sessions cross midnight.
        row["sessions_count"] = len(row["thread_ids"])
        row["thread_ids"] = sorted(row["thread_ids"])
        project_daily_rows.append(row)
    ranked = lambda rows: sorted(rows, key=lambda row: (-row["total_tokens"], json.dumps(row, sort_keys=True)))
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "codex_home": str(home),
        "timezone": str(zone),
        "range": {
            "since_inclusive": since.isoformat() if since else None,
            "until_exclusive": until.isoformat() if until else None,
        },
        "totals": totals,
        "projects": ranked(project_rows),
        "sessions": ranked(sessions.values()),
        "models": ranked(models.values()),
        "daily": sorted(daily.values(), key=lambda row: row["day"]),
        "project_daily": project_daily_rows,
        "coverage": coverage,
        "warnings": list(dict.fromkeys(warnings)),
    }


def render_table(report: dict, group_by: str) -> str:
    key, rows_key = {
        "project": ("project", "projects"),
        "session": ("thread_id", "sessions"),
        "model": ("model", "models"),
        "day": ("day", "daily"),
    }[group_by]
    columns = [
        (group_by.title(), key), ("Input", "input_tokens"),
        ("Cached*", "cached_input_tokens"), ("Output", "output_tokens"),
        ("Reasoning*", "reasoning_output_tokens"), ("Total", "total_tokens"),
    ]
    rows = [
        [str(row[key])] + [f"{row[name]:,}" for _, name in columns[1:]]
        for row in report[rows_key]
    ]
    rows.append(
        ["TOTAL"] + [f"{report['totals'][name]:,}" for _, name in columns[1:]]
    )
    def display_width(value: str) -> int:
        return sum(
            2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
            for char in value
        )
    widths = [
        max(display_width(label), *(display_width(row[index]) for row in rows))
        for index, (label, _) in enumerate(columns)
    ]
    def formatted(row: list[str]) -> str:
        return "  ".join(
            value + " " * (widths[index] - display_width(value))
            if index == 0 else value.rjust(widths[index])
            for index, value in enumerate(row)
        )
    lines = [
        "Codex observed token usage",
        f"Home: {report['codex_home']}",
        f"Range: {report['range']['since_inclusive'] or 'all history'}"
        f" <= timestamp < {report['range']['until_exclusive'] or 'no end'}",
        "",
        formatted([label for label, _ in columns]),
        formatted(["-" * width for width in widths]),
        *(formatted(row) for row in rows),
        "",
        "* Cached input and reasoning output are subcategories, not extra totals.",
        "Cache-write input is available in JSON output.",
        f"Observed response records: {report['totals']['request_records']}; "
        f"legacy delta updates: {report['totals']['legacy_updates']}.",
        "These are recorded tokens, not a subscription quota or a final bill.",
    ]
    if report["warnings"]:
        lines.append(f"Warnings: {len(report['warnings'])} (printed to stderr).")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser())
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--days", type=int, help="Rolling number of days; default 7")
    selection.add_argument("--all-time", action="store_true", help="Include all retained history")
    selection.add_argument("--since", help="Inclusive ISO date/time; date means midnight")
    parser.add_argument("--until", help="Exclusive ISO date/time; date means midnight")
    parser.add_argument("--timezone", help="IANA timezone, e.g. Asia/Shanghai; default local timezone")
    parser.add_argument("--format", choices=("table", "json"), default="table")
    parser.add_argument("--group-by", choices=("project", "session", "model", "day"), default="project")
    parser.add_argument("--separate-worktrees", action="store_true")
    parser.add_argument("--output", type=Path, help="Write result to this file instead of stdout")
    args = parser.parse_args(argv)
    try:
        zone = ZoneInfo(args.timezone) if args.timezone else local_timezone()
    except ZoneInfoNotFoundError:
        parser.error(f"Unknown/unavailable timezone: {args.timezone}; install tzdata if needed")
    since = None
    until = None
    if args.since:
        since = parse_timestamp(args.since, zone)
        if since is None:
            parser.error("--since must be an ISO date/time")
    if args.until:
        until = parse_timestamp(args.until, zone)
        if until is None:
            parser.error("--until must be an ISO date/time")
    if not args.all_time and not args.since:
        days = args.days if args.days is not None else 7
        if days <= 0:
            parser.error("--days must be positive")
        until = until or datetime.now(UTC)
        since = until - timedelta(days=days)
    try:
        report = scan_usage(
            args.codex_home, since, until, zone,
            group_worktrees=not args.separate_worktrees,
        )
        output = (
            json.dumps(report, ensure_ascii=False, indent=2) + "\n"
            if args.format == "json"
            else render_table(report, args.group_by)
        )
        if args.output:
            args.output.expanduser().write_text(output, encoding="utf-8")
        else:
            sys.stdout.write(output)
        if args.format == "table":
            for warning in report["warnings"][:20]:
                print(f"Warning: {warning}", file=sys.stderr)
            if len(report["warnings"]) > 20:
                print("Warning: further details are included in --format json", file=sys.stderr)
    except (ValueError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
