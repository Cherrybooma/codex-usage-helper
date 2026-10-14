"""Summaries of recorded official windows, independent of local token totals.

Read only the application's allowlisted quota history. A changed deadline is
not proof of a bonus reset, and a final sampled balance is not an unused refund.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import math
import sqlite3

UTC = timezone.utc
GAP_SECONDS = 180


def _iso(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, UTC).isoformat()


def quota_window_report(path: Path | str, now: datetime, *, history_limit: int = 30) -> dict:
    if now.tzinfo is None:
        raise ValueError("An aware cutoff is required")
    if isinstance(history_limit, bool) or not isinstance(history_limit, int) or not 1 <= history_limit <= 500:
        raise ValueError("Quota history limit must be 1 to 500")
    cutoff = now.timestamp()
    path = Path(path)
    if not path.is_file():
        return {"status": "unavailable", "statistics_until": _iso(cutoff), "windows": [],
                "notes": ["尚无本机额度观测；不补造历史额度。"]}
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "observations" not in tables:
            return {"status": "unavailable", "statistics_until": _iso(cutoff), "windows": [], "notes": ["尚无有效额度观测。"]}
        # The fixed end is exclusive, including for quota history. Never use a
        # later sample to improve an earlier report retrospectively.
        rows = db.execute("SELECT observed,window_key,duration,reset,used FROM observations WHERE observed<? ORDER BY observed,window_key", (cutoff,)).fetchall()
        contexts = {}
        if "observation_context" in tables:
            contexts = {(a, b): (c, d) for a, b, c, d in db.execute("SELECT observed,window_key,plan,limit_id FROM observation_context WHERE observed<?", (cutoff,))}
        changes = {}
        if "window_changes" in tables:
            changes = {(a, b, c): d for a, b, c, d in db.execute("SELECT window_key,duration,reset,observed_change FROM window_changes WHERE observed_change<?", (cutoff,))}
    grouped = defaultdict(list)
    skipped = 0
    for observed, key, duration, reset, used in rows:
        if (duration not in (300, 10080) or not isinstance(key, str) or
                not all(isinstance(n, (int, float)) and math.isfinite(n) for n in (observed, reset, used)) or
                not 0 <= used <= 100 or observed >= reset):
            skipped += 1
            continue
        grouped[(key, duration, reset)].append((observed, used, contexts.get((observed, key), (None, None))))
    windows = []
    for (key, duration, reset), samples in grouped.items():
        first, last = samples[0], samples[-1]
        inferred_start = reset - duration * 60
        change = changes.get((key, duration, reset))
        ended_at = min(reset, change) if change is not None else reset
        notes = []
        if first[0] > inferred_start + GAP_SECONDS:
            notes.append("未从窗口开始连续采集，首次观测之前的过程未知。")
        if any(b[0] - a[0] > GAP_SECONDS for a, b in zip(samples, samples[1:])):
            notes.append("存在超过3分钟的采集间隔，可能漏掉变化。")
        decrease = any(b[1] < a[1] for a, b in zip(samples, samples[1:]))
        if decrease:
            notes.append("同截止时间下计数下降，可能重置或校正；不累计为确定消耗。")
        plans = {s[2][0] for s in samples if s[2][0]}
        buckets = {s[2][1] for s in samples if s[2][1]}
        incompatible = len(plans) > 1 or len(buckets) > 1 or bool(buckets - {"codex"})
        if incompatible:
            notes.append("套餐或额度分类不一致，不能比较为同一份容量。")
        if change is not None:
            notes.append("观测到截止时间变化；不能单凭变化确认福利重置或最终剩余。")
        if cutoff - last[0] > GAP_SECONDS or ended_at <= cutoff:
            notes.append("只保留末次观测，不代表窗口结束时的最终使用或未用完额度。")
        delta = None if decrease or incompatible else round(sum(b[1] - a[1] for a, b in zip(samples, samples[1:])), 6)
        windows.append({
            "key": f"quota:{key}:{duration}:{reset}",
            "kind": "周额度" if duration == 10080 else "5小时",
            "start": _iso(inferred_start), "end": _iso(reset),
            "observed_until": _iso(last[0]), "ended_at": _iso(ended_at),
            "duration_mins": duration, "used_percent": last[1],
            "remaining_percent": max(0, 100 - last[1]),
            "first_used_percent": first[1], "peak_used_percent": max(s[1] for s in samples),
            "observed_increase_percent": delta, "observations": len(samples),
            "first_observed_at": _iso(first[0]), "last_observed_at": _iso(last[0]),
            "state": "进行中" if ended_at > cutoff else "已结束",
            "reset_kind": "未确认" if change is not None else "常规",
            "status": "incomplete" if notes else "observed",
            "boundary_basis": "duration_before_reset",
            "notes": notes,
            "data_scope": "官方已返回的百分比；窗口开始按时长反推，不是Token账单。",
        })
    windows.sort(key=lambda row: (row["end"], row["kind"]), reverse=True)
    total_windows = len(windows)
    current = [row for row in windows if row["state"] == "进行中"]
    history = [row for row in windows if row["state"] == "已结束"][:history_limit]
    # This is a partial display/export, never a reconciliation scope. Keep all
    # raw allowlisted observations in the local database, and retain previous
    # cloud history even after it falls outside this recent list.
    windows = current + history
    return {"status": "recorded" if windows else "unavailable", "statistics_until": _iso(cutoff), "windows": windows,
            "windows_recorded_locally": total_windows, "history_limit": history_limit,
            "history_scope": "current_and_recent_closed_windows",
            "history_limited": total_windows > len(windows),
            "skipped_observations": skipped,
            "notes": ["不对不同窗口的剩余百分比求和；未采集的历史额度保持未知。"]}
