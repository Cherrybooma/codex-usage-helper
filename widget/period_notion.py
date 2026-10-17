"""Opt-in account period summaries, separate from Notion project management.

The input is already-counted aggregate data. This module never reads Codex logs,
credentials, project pages, or user conversations. Only the explicit allowlist
below is transmitted; rows replace absolute snapshots, never add deltas.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import math
import re

if __package__:
    from .notion_sync import (COUNTERS, NUMBER_PROPERTIES, SHANGHAI, NotionError,
                              NotionWorkspace, _plain, _safe_https, _text, notion_id)
else:
    from notion_sync import (COUNTERS, NUMBER_PROPERTIES, SHANGHAI, NotionError,
                             NotionWorkspace, _plain, _safe_https, _text, notion_id)

LEDGER_KINDS = {"natural_week": "自然周", "billing_month": "会员月度"}
WINDOW_KINDS = {"five_hour": "5小时", "weekly_quota": "周额度"}
ALL_KINDS = {**LEDGER_KINDS, **WINDOW_KINDS}
DATA_SCOPE = "本机保留记录及已观测额度；不代表完整账户用量或最终账单"
QUALITY_NAMES = {"response_records_verified": "按响应记录",
                 "legacy_estimated": "含旧累计估算",
                 "partial_unreadable": "部分记录未读取", "no_observed_usage": "窗口内未记录用量", "unknown": "未核实"}
RESET_KINDS = {"regular": "常规", "early": "提前重置", "unknown": "未确认"}
WINDOW_NOTES = {
    "late_start": "未从窗口开始连续采集，首次观测之前的过程未知。",
    "sampling_gap": "存在超过3分钟的采集间隔，可能漏掉变化。",
    "counter_decrease": "同截止时间下计数下降，可能重置或校正；不累计为确定消耗。",
    "incompatible_bucket": "套餐或额度分类不一致，不能比较为同一份容量。",
    "deadline_changed": "观测到截止时间变化；不能单凭变化确认福利重置或最终剩余。",
    "final_usage_unknown": "只保留末次观测，不代表窗口结束时的最终使用或未用完额度。",
    "superseded_window": "后续报告已观测到同类新窗口，此记录已不再是当前观测窗口；切换时刻未保留。原 End 仍为旧读数的预计重置时间，末次读数不代表最终消耗，不能据此确认福利重置。",
}


def database_schema(kind: str) -> dict:
    """Public schema for bootstrap; intentionally has no Project relation."""
    if kind not in {"ledger", "windows"}:
        raise NotionError("invalid_input")
    result = {"Name": {"title": {}}, "Period Key": {"rich_text": {}},
              "Dataset": {"rich_text": {}}, "Provider": {"select": {"options": [{"name": "Codex"}]}},
              "Start": {"date": {}}, "End": {"date": {}},
              "Start ISO": {"rich_text": {}}, "End ISO": {"rich_text": {}},
              "Statistics Until": {"date": {}}, "Statistics Until ISO": {"rich_text": {}},
              "Included": {"checkbox": {}}, "Synced At": {"date": {}},
              "Schema Version": {"number": {}}, "Quality": {"rich_text": {}},
              "Coverage": {"rich_text": {}}, "Data Scope": {"rich_text": {}},
              "Boundary Basis": {"rich_text": {}}}
    names = LEDGER_KINDS if kind == "ledger" else WINDOW_KINDS
    result["Kind"] = {"select": {"options": [{"name": name} for name in names.values()]}}
    if kind == "ledger":
        result.update({name: {"number": {}} for name in NUMBER_PROPERTIES.values()})
        result.update({"State": {"select": {"options": [{"name": name} for name in ("进行中", "已结束")]}},
                       "Sessions": {"number": {}}, "Counted Tokens": {"number": {}},
                       "Project Summary": {"rich_text": {}}, "Model Summary": {"rich_text": {}},
                       "Schedule": {"rich_text": {}}})
    else:
        result.update({"Used Percent": {"number": {}}, "Remaining Percent": {"number": {}},
                       "State": {"select": {"options": [{"name": name} for name in ("进行中", "已结束")]}},
                       "Duration Minutes": {"number": {}}, "Observed At": {"date": {}},
                       "Observed At ISO": {"rich_text": {}}, "Observations": {"number": {}},
                       "Status": {"select": {"options": [{"name": name} for name in ("已观测", "观测不足")]}},
                       "Reset Kind": {"select": {"options": [{"name": name} for name in RESET_KINDS.values()]}},
                       "Estimate": {"rich_text": {}}, "Note": {"rich_text": {}}})
    return result


def _integer(value, *, nullable=False):
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2 ** 53:
        raise NotionError("invalid_input")
    return value


def _percent(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100:
        raise NotionError("invalid_input")
    return float(value)


def _label(value, fallback):
    """Only short display labels, not arbitrary scanner text or local paths."""
    if not isinstance(value, str) or not value.strip():
        return fallback
    value = value.strip()
    if (len(value) > 120 or any(ord(char) < 32 for char in value)
            or re.search(r"(?:^|\s)(?:/|~[/\\]|[A-Za-z]:[/\\]|file:|https?://)", value)):
        return fallback
    return value


def _summary(values, *, model=False):
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > 1000:
        raise NotionError("invalid_input")
    result = []
    for value in values:
        if not isinstance(value, dict):
            raise NotionError("invalid_input")
        name = _label(value.get("display_name", value.get("name", value.get("model"))), "unknown" if model else "未命名项目")
        result.append({"name": name, "total_tokens": _integer(value.get("total_tokens", 0))})
    return sorted(result, key=lambda row: (-row["total_tokens"], row["name"]))


def _coverage(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise NotionError("invalid_input")
    result = {}
    for name in ("files_scanned", "files_with_usage", "unreadable_files", "skipped_files",
                 "response_records", "legacy_updates", "warnings_count", "accepted_records",
                 "source_files_found", "source_files_scanned", "source_files_skipped"):
        if name in value:
            result[name] = _integer(value[name])
    for name in ("complete", "history_complete", "observations_complete", "retained_logs_only", "complete_subscription_history"):
        if name in value:
            if not isinstance(value[name], bool):
                raise NotionError("invalid_input")
            result[name] = value[name]
    if value.get("status") in {"partial_unreadable", "observed", "no_observed_usage"}:
        result["status"] = value["status"]
    return result


def report_rows(report: dict, dataset_id: str) -> tuple[list[dict], list[dict]]:
    """Adapt the accounting engine without importing its calculation functions.

    Limited history lists are partial snapshots. Callers must not pass their
    kinds as complete_kinds merely because every displayed row is present.
    """
    if not isinstance(report, dict):
        raise NotionError("invalid_input")
    dataset = notion_id(dataset_id)
    ledger = []
    for section, kind in (("weekly", "natural_week"), ("monthly", "billing_month")):
        section_data = report.get(section) or {}
        if not isinstance(section_data, dict) or not isinstance(section_data.get("periods", []), list):
            raise NotionError("invalid_input")
        for period in section_data.get("periods", []):
            if not isinstance(period, dict) or not isinstance(period.get("totals", {}), dict):
                raise NotionError("invalid_input")
            totals = period.get("totals", {})
            coverage = period.get("coverage", report.get("coverage", {}))
            row = {"dataset_id": dataset, "kind": kind, "start": period.get("start"), "end": period.get("end"),
                   "statistics_until": period.get("observed_until", report.get("statistics_until", report.get("generated_at"))),
                   "scan_cutoff": report.get("generated_at", period.get("observed_until")),
                   "sessions_count": period.get("sessions_count"), "projects": period.get("projects", []),
                   "models": period.get("models", []), "coverage": coverage,
                   "quality": period.get("quality", coverage.get("quality", section_data.get("quality", "unknown")))}
            row.update({counter: totals.get(counter, 0) for counter in COUNTERS})
            precision = section_data.get("billing_anchor_precision", section_data.get("anchor_precision"))
            row["boundary_basis"] = "natural_week" if kind == "natural_week" else (
                "confirmed_anchor_date" if precision == "date" else "confirmed_anchor_datetime")
            if kind == "billing_month":
                row["schedule_id"] = section_data.get("schedule_id")
            ledger.append(row)
    quota = report.get("quota") or {}
    if not isinstance(quota, dict) or not isinstance(quota.get("windows", []), list):
        raise NotionError("invalid_input")
    windows = []
    kind_names = {"5小时": "five_hour", "周额度": "weekly_quota"}
    reset_names = {"常规": "regular", "提前重置": "early", "未确认": "unknown"}
    for window in quota.get("windows", []):
        if not isinstance(window, dict):
            raise NotionError("invalid_input")
        windows.append({"dataset_id": dataset, "kind": kind_names.get(window.get("kind"), window.get("kind")),
                        "start": window.get("start"), "end": window.get("end"),
                        "statistics_until": window.get("observed_until", report.get("statistics_until", report.get("generated_at"))),
                        # Quota is read afresh even when the Token report is a
                        # cached replay. Keep both clocks independently true.
                        "scan_cutoff": quota.get("statistics_until", report.get("generated_at", window.get("observed_until"))),
                        "observed_at": window.get("last_observed_at", window.get("observed_at")),
                        "duration_minutes": window.get("duration_mins", window.get("duration_minutes")),
                        "observations": window.get("observations", 1),
                        "used_percent": window.get("used_percent"), "remaining_percent": window.get("remaining_percent"),
                        "first_used_percent": window.get("first_used_percent"),
                        "peak_used_percent": window.get("peak_used_percent"),
                        "observed_increase_percent": window.get("observed_increase_percent"),
                        "reset_kind": reset_names.get(window.get("reset_kind"), window.get("reset_kind", "unknown")),
                        "boundary_basis": "duration_before_reset",
                        "state": window.get("state"), "ended_at": window.get("ended_at"),
                        "note_flags": [flag for flag, note in WINDOW_NOTES.items() if note in window.get("notes", [])],
                        "coverage": {"observations_complete": window.get("status") == "observed"}, "quality": "unknown"})
    return ledger, windows


class PeriodNotionWorkspace(NotionWorkspace):
    """Shares the authorized connection, but owns separate outbox/state keys."""
    def _period_state(self):
        state = self.state.setdefault("periods", {"pending": {}, "synced": {}, "complete_cutoffs": {}})
        if not isinstance(state, dict) or any(not isinstance(state.get(key), dict) for key in ("pending", "synced", "complete_cutoffs")):
            raise NotionError("invalid_response")
        schedules = state.setdefault("schedules", {})
        if not isinstance(schedules, dict):
            raise NotionError("invalid_response")
        if not isinstance(state.setdefault("window_currents", {}), dict):
            raise NotionError("invalid_response")
        # Upgrade the original single-source state once. New sources must not
        # replace the freshness guard belonging to another scanner dataset.
        if state.get("schedule_dataset") and state.get("schedule_cutoff"):
            schedules.setdefault(state["schedule_dataset"], {
                "schedule_id": state.get("active_monthly_schedule"), "cutoff": state["schedule_cutoff"]})
        for field in ("schedule_dataset", "schedule_cutoff", "active_monthly_schedule"):
            state.pop(field, None)
        return state

    def _ready(self):
        return bool(self.config.get("period_ledger_data_source_id") and
                    self.config.get("period_windows_data_source_id"))

    def _source(self, family):
        return notion_id(self.config.get(f"period_{family}_data_source_id"))

    def _validate_source(self, family, source):
        remote = self._request("GET", f"/data_sources/{notion_id(source)}", idempotent=True)
        props = remote.get("properties")
        if not isinstance(props, dict):
            raise NotionError("schema_mismatch")
        for name, required in database_schema(family).items():
            actual = props.get(name, {})
            if not isinstance(actual, dict) or (actual.get("type") or next(
                    (key for key in required if key in actual), None)) != next(iter(required)):
                raise NotionError("schema_mismatch")

    def _remember_period_error(self, exc):
        self._period_state()["error"] = exc.public()
        if exc.code in ("not_connected", "unauthorized"):
            self.state["credential_available"] = False
        if exc.code == "api_access_blocked":
            self.state["automatic_sync_paused"] = True
        if exc.retry_after:
            self.state["next_retry"] = (self._now() + timedelta(seconds=exc.retry_after)).isoformat()

    def configure(self, page_id: str, ledger_data_source_id: str, windows_data_source_id: str,
                  *, ledger_database_id=None, windows_database_id=None) -> dict:
        """Bind an already-created independent page without touching Projects."""
        with self._locked():
            try:
                page = notion_id(page_id)
                sources = {"ledger": notion_id(ledger_data_source_id), "windows": notion_id(windows_data_source_id)}
                if sources["ledger"] == sources["windows"]:
                    raise NotionError("invalid_input")
                if self.config.get("period_page_id") not in (None, page):
                    raise NotionError("invalid_input")
                # Outbox fingerprints belong to the bound destinations. A
                # migration needs its own explicit workflow; silently swapping
                # sources would incorrectly treat an empty database as synced.
                for family, source in sources.items():
                    bound = self.config.get(f"period_{family}_data_source_id")
                    if bound and notion_id(bound) != source:
                        raise NotionError("invalid_input")
                self._request("GET", f"/pages/{page}", idempotent=True)
                for family, source in sources.items():
                    self._validate_source(family, source)
                self.config["period_page_id"] = page
                self.config["period_page_url"] = "https://app.notion.com/p/" + page.replace("-", "")
                self.config["accounting_page_url"] = self.config["period_page_url"]
                for family, source in sources.items():
                    self.config[f"period_{family}_data_source_id"] = source
                for family, database in (("ledger", ledger_database_id), ("windows", windows_database_id)):
                    if database is not None:
                        database = notion_id(database)
                        self.config[f"period_{family}_database_id"] = database
                        self.config[f"period_{family}_url"] = "https://app.notion.com/p/" + database.replace("-", "")
                self._period_state().pop("error", None)
            except NotionError as exc:
                self._remember_period_error(exc)
                raise
            return self._status()

    def setup(self, parent_page: str, now=None) -> dict:
        """Create/reuse the two account databases under a chosen account page."""
        with self._locked():
            try:
                parent = notion_id(parent_page)
                if self.config.get("period_page_id") not in (None, parent):
                    raise NotionError("invalid_input")
                self._request("GET", f"/pages/{parent}", idempotent=True)
                self.config["period_page_id"] = parent
                self.config["period_page_url"] = "https://app.notion.com/p/" + parent.replace("-", "")
                self.config["accounting_page_url"] = self.config["period_page_url"]
                for family in ("ledger", "windows"):
                    key = "period_" + family
                    marker = f"codex-usage-helper:{self.config['dataset_id']}:{key}:v1"
                    operation = "database:" + key
                    database = None
                    if self.config.get(key + "_database_id"):
                        database = self._request("GET", "/databases/" + notion_id(self.config[key + "_database_id"]), idempotent=True)
                    if database is None:
                        database = self._find_database(parent, marker)
                    if database is None:
                        if operation in self.state["uncertain_creates"]:
                            raise NotionError("write_uncertain")
                        self.state["uncertain_creates"][operation] = {"kind": key}
                        self._save(self.state_path, self.state)
                        try:
                            database = self._request("POST", "/databases", {
                                "parent": {"type": "page_id", "page_id": parent},
                                "title": _text("周期账本" if family == "ledger" else "额度窗口"),
                                "description": _text(marker), "is_inline": True,
                                "initial_data_source": {"properties": database_schema(family)}})
                        except NotionError as exc:
                            if exc.committed_id:
                                database = self._request("GET", "/databases/" + exc.committed_id, idempotent=True)
                            elif exc.uncertain:
                                database = self._find_database(parent, marker)
                                if database is None:
                                    raise NotionError("write_uncertain") from None
                            else:
                                self.state["uncertain_creates"].pop(operation, None)
                                raise
                    sources = database.get("data_sources")
                    if not isinstance(sources, list) or len(sources) != 1:
                        raise NotionError("schema_mismatch")
                    source = notion_id(sources[0].get("id"))
                    bound = self.config.get(key + "_data_source_id")
                    if bound and notion_id(bound) != source:
                        # setup must obey the same migration guard as
                        # configure, including remote source replacements.
                        raise NotionError("invalid_input")
                    self._validate_source(family, source)
                    self.config[key + "_database_id"] = notion_id(database.get("id"))
                    self.config[key + "_data_source_id"] = source
                    self.config[key + "_url"] = _safe_https(database.get("url"))
                    self.state["uncertain_creates"].pop(operation, None)
                    self._save(self.config_path, self.config)
                self._period_state().pop("error", None)
            except NotionError as exc:
                self._remember_period_error(exc)
                raise
            return self._status()

    def _normalize(self, rows, family, now):
        if not isinstance(rows, list):
            raise NotionError("invalid_input")
        allowed = LEDGER_KINDS if family == "ledger" else WINDOW_KINDS
        result, seen = [], set()
        for raw in rows:
            if not isinstance(raw, dict) or raw.get("kind") not in allowed:
                raise NotionError("invalid_input")
            dataset = notion_id(raw.get("dataset_id", self.config["dataset_id"]))
            start, end = self._now(raw.get("start")), self._now(raw.get("end"))
            until = self._now(raw.get("statistics_until") or now)
            if not raw.get("start") or not raw.get("end") or start >= end or until > now or until < start:
                raise NotionError("invalid_input")
            row = {"dataset_id": dataset, "kind": raw["kind"], "family": family,
                   "start": start.isoformat(), "end": end.isoformat(), "statistics_until": until.isoformat(),
                   "coverage": _coverage(raw.get("coverage"))}
            cutoff = self._now(raw.get("scan_cutoff") or until)
            if cutoff > now or cutoff < until:
                raise NotionError("invalid_input")
            row["scan_cutoff"] = cutoff.isoformat()
            basis = raw.get("boundary_basis")
            row["boundary_basis"] = basis if basis in {"natural_week", "confirmed_anchor_date", "confirmed_anchor_datetime", "duration_before_reset"} else "unknown"
            quality = raw.get("quality", "unknown")
            quality = quality if isinstance(quality, list) else [quality]
            row["quality"] = sorted({q if isinstance(q, str) and q in QUALITY_NAMES else "unknown" for q in quality}) or ["unknown"]
            if family == "ledger":
                for counter in COUNTERS:
                    row[counter] = _integer(raw.get(counter, 0))
                row["sessions_count"] = _integer(raw.get("sessions_count"), nullable=True)
                row["state"] = "closed" if until >= end else "current"
                row["projects"] = _summary(raw.get("projects"))
                row["models"] = _summary(raw.get("models"), model=True)
                schedule = raw.get("schedule_id")
                if schedule is not None and (raw["kind"] != "billing_month" or not isinstance(schedule, str) or not re.fullmatch(r"[0-9a-f]{20,64}", schedule)):
                    raise NotionError("invalid_input")
                row["schedule_id"] = schedule
            else:
                used, remaining = _percent(raw.get("used_percent")), _percent(raw.get("remaining_percent"))
                if used is not None and remaining is not None and abs(used + remaining - 100) > .01:
                    raise NotionError("invalid_input")
                observed = self._now(raw.get("observed_at"))
                if not raw.get("observed_at") or not start <= observed <= until:
                    raise NotionError("invalid_input")
                row.update({"used_percent": used, "remaining_percent": remaining,
                            "duration_minutes": _integer(raw.get("duration_minutes")),
                            "observed_at": observed.isoformat(),
                            "observations": _integer(raw.get("observations", 1)),
                            "reset_kind": raw.get("reset_kind") if raw.get("reset_kind") in RESET_KINDS else "unknown"})
                window_state = raw.get("state")
                row["state"] = "closed" if window_state in {"已结束", "closed"} else "current" if window_state in {"进行中", "open", "current"} else "closed" if now >= end else "current"
                ended_at = self._now(raw["ended_at"]) if raw.get("ended_at") else end
                if not start <= ended_at <= end:
                    raise NotionError("invalid_input")
                row["ended_at"] = ended_at.isoformat()
                flags = raw.get("note_flags", [])
                if not isinstance(flags, list):
                    raise NotionError("invalid_input")
                row["note_flags"] = sorted({flag for flag in flags if isinstance(flag, str) and flag in WINDOW_NOTES})
                for field in ("first_used_percent", "peak_used_percent"):
                    row[field] = _percent(raw.get(field))
                increment = raw.get("observed_increase_percent")
                if increment is not None and (isinstance(increment, bool) or not isinstance(increment, (int, float)) or not math.isfinite(increment) or not 0 <= increment <= 2 ** 53):
                    raise NotionError("invalid_input")
                row["observed_increase_percent"] = increment
                row["complete"] = bool(row["coverage"].get("observations_complete", False))
            key = self._key(row)
            if key in seen:
                raise NotionError("invalid_input")
            seen.add(key)
            result.append(row)
        return result

    @staticmethod
    def _key(row):
        values = [row["dataset_id"], row["kind"], row["start"], row["end"]]
        if row.get("schedule_id"):
            values.append(row["schedule_id"])
        return hashlib.sha256(json.dumps(values,
                                         separators=(",", ":")).encode()).hexdigest()

    @staticmethod
    def _fingerprint(row):
        # Keep scan watermarks locally. Advancing time alone is not new usage:
        # preserve the last transmitted cutoff without repainting the cloud row.
        # Window observations retain their observation time/count semantics.
        ignored = {"scan_cutoff"}
        if row.get("family") == "ledger":
            ignored.add("statistics_until")
        content = {field: value for field, value in row.items() if field not in ignored}
        return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def _properties(self, row, key, now):
        local_start = self._now(row["start"]).astimezone(SHANGHAI)
        local_end = self._now(row["end"]).astimezone(SHANGHAI)
        name = f"{ALL_KINDS[row['kind']]} · {local_start:%Y-%m-%d}—{local_end:%Y-%m-%d}"
        props = {"Name": {"title": _text(name)}, "Period Key": {"rich_text": _text(key)},
                 "Dataset": {"rich_text": _text(row["dataset_id"])}, "Kind": {"select": {"name": ALL_KINDS[row["kind"]]}},
                 "Provider": {"select": {"name": "Codex"}}, "Start": {"date": {"start": local_start.isoformat()}},
                 "End": {"date": {"start": local_end.isoformat()}}, "Start ISO": {"rich_text": _text(row["start"])},
                 "End ISO": {"rich_text": _text(row["end"])},
                 "Statistics Until": {"date": {"start": row["statistics_until"]}},
                 "Statistics Until ISO": {"rich_text": _text(row["statistics_until"])},
                 "Included": {"checkbox": True}, "Synced At": {"date": {"start": now.isoformat()}},
                 "Schema Version": {"number": 1}, "Data Scope": {"rich_text": _text(DATA_SCOPE)},
                 "Boundary Basis": {"rich_text": _text({
                     "natural_week": "Asia/Shanghai 周一 00:00 至下周一 00:00；开始包含、结束不包含",
                     "confirmed_anchor_date": "用户确认续费日期；按该锚点推算，日期按当地 00:00，日粒度边界估算，非历史账单证明",
                     "confirmed_anchor_datetime": "用户确认续费时间；按该锚点推算，非历史账单证明",
                     "duration_before_reset": "采用实际 reset 时间与窗口时长；窗口起点按时长回推",
                 }.get(row["boundary_basis"], "周期边界来源未核实"))},
                 "Quality": {"rich_text": _text("；".join(QUALITY_NAMES[value] for value in row["quality"]))},
                 "Coverage": {"rich_text": _text(json.dumps(row["coverage"], sort_keys=True, ensure_ascii=False))}}
        if row["family"] == "ledger":
            props.update({name: {"number": row[field]} for field, name in NUMBER_PROPERTIES.items()})
            props.update({"State": {"select": {"name": "已结束" if row["state"] == "closed" else "进行中"}},
                          "Sessions": {"number": row["sessions_count"]}, "Counted Tokens": {"number": row["total_tokens"]},
                          "Schedule": {"rich_text": _text(row["schedule_id"] or "")}})
            for field, name in (("projects", "Project Summary"), ("models", "Model Summary")):
                # Long rankings stay finite; no IDs, paths, raw warnings, or conversations.
                entries = [f"{value['name']}: {value['total_tokens']}" for value in row[field]]
                summary = "\n".join(entries)
                if len(summary) > 1900:
                    summary = summary[:1850] + "\n…仅展示部分汇总"
                props[name] = {"rich_text": _text(summary)}
        else:
            parts = ["仅保留真实读数；未观测用量不补零、不补满额"]
            for field, label in (("first_used_percent", "首个读数"), ("peak_used_percent", "已观测峰值"),
                                 ("observed_increase_percent", "连续观测增量")):
                if row.get(field) is not None:
                    parts.append(f"{label} {row[field]:g}%")
            notes = ["窗口开始按实际 reset 与时长回推，非充值时刻；额度百分比不是 Token 数，不按周相加成为月完成率。"]
            notes.extend(WINDOW_NOTES[flag] for flag in row["note_flags"])
            if row["ended_at"] != row["end"]:
                notes.append("原截止时间已被替换；实际观测结束于 " + row["ended_at"])
            props.update({"Used Percent": {"number": row["used_percent"]},
                          "State": {"select": {"name": "已结束" if row["state"] == "closed" else "进行中"}},
                          "Remaining Percent": {"number": row["remaining_percent"]},
                          "Duration Minutes": {"number": row["duration_minutes"]},
                          "Observed At": {"date": {"start": row["observed_at"]}},
                          "Observed At ISO": {"rich_text": _text(row["observed_at"])},
                          "Observations": {"number": row["observations"]},
                          "Status": {"select": {"name": "已观测" if row["complete"] else "观测不足"}},
                          "Reset Kind": {"select": {"name": RESET_KINDS[row["reset_kind"]]}},
                          "Estimate": {"rich_text": _text("；".join(parts))},
                          "Note": {"rich_text": _text("\n".join(notes))}})
        return props

    def _find(self, family, key):
        found = self._query(self._source(family), filter={"property": "Period Key", "rich_text": {"equals": key}})
        if len(found) > 1:
            raise NotionError("duplicate_summary")
        return found

    def _upsert_period(self, key, row, now):
        found = self._find(row["family"], key)
        props = self._properties(row, key, now)
        operation = "period:" + key
        if found:
            page_id = notion_id(found[0].get("id"))
            if row["family"] == "windows":
                existing = found[0].get("properties", {})
                observed = _plain(existing.get("Observed At ISO", {}).get("rich_text"))
                until = _plain(existing.get("Statistics Until ISO", {}).get("rich_text"))
                note = _plain(existing.get("Note", {}).get("rich_text"))
                if (observed and self._now(observed) > self._now(row["observed_at"])) or (
                        until and self._now(until) > self._now(row["scan_cutoff"])) or (
                        row["state"] == "current" and WINDOW_NOTES["superseded_window"] in note
                        and observed and self._now(row["observed_at"]) <= self._now(observed)):
                    self._period_state()["pending"].pop(key, None)
                    self.state["uncertain_creates"].pop(operation, None)
                    self._save(self.state_path, self.state)
                    return False
            self._request("PATCH", "/pages/" + page_id, {"properties": props}, idempotent=True)
        else:
            if operation in self.state["uncertain_creates"]:
                raise NotionError("write_uncertain")
            self.state["uncertain_creates"][operation] = {"kind": "period", "family": row["family"]}
            self._save(self.state_path, self.state)
            try:
                page = self._request("POST", "/pages", {"parent": {"type": "data_source_id", "data_source_id": self._source(row["family"])},
                                                       "properties": props})
                page_id = notion_id(page.get("id"))
            except NotionError as exc:
                if exc.committed_id:
                    page = self._request("GET", "/pages/" + exc.committed_id, idempotent=True)
                    if (_plain((page.get("properties", {}).get("Period Key") or {}).get("rich_text")) != key
                            or page.get("parent", {}).get("data_source_id") != self._source(row["family"])):
                        raise NotionError("write_uncertain") from None
                    page_id = notion_id(page.get("id"))
                elif exc.uncertain:
                    found = self._find(row["family"], key)
                    if len(found) != 1:
                        raise NotionError("write_uncertain") from None
                    page_id = notion_id(found[0].get("id"))
                else:
                    self.state["uncertain_creates"].pop(operation, None)
                    raise
        self.state["uncertain_creates"].pop(operation, None)
        state = self._period_state()
        state["synced"][key] = {"page_id": page_id, "fingerprint": self._fingerprint(row),
                                "dataset_id": row["dataset_id"], "kind": row["kind"], "family": row["family"],
                                "start": row["start"], "end": row["end"], "statistics_until": row["statistics_until"],
                                "scan_cutoff": row["scan_cutoff"], "schedule_id": row.get("schedule_id")}
        state["pending"].pop(key, None)
        self._save(self.state_path, self.state)
        return True

    def sync(self, ledger_rows: list[dict], window_rows: list[dict], *, now=None, force=False, sync_windows=True,
             complete_kinds=None, complete_until=None, billing_configured=None,
             active_monthly_schedule=None) -> dict:
        """Reconcile explicit complete kinds only; partial inputs never retire history.

        A uniquely observed current quota window may close a superseded current
        observation of the same dataset/kind. This only changes display state:
        it does not exclude history, alter samples, or prove an actual reset.

        Rows use dataset_id, kind, start/end (aware ISO), statistics_until,
        counters/coverage/quality. Window rows add observed_at, duration_minutes,
        used_percent/remaining_percent and observations. Missing billing anchor
        is a local status, not a fabricated monthly row.
        """
        with self._locked():
            state = self._period_state()
            now, changed = self._now(now), 0
            try:
                if not isinstance(sync_windows, bool):
                    raise NotionError("invalid_input")
                rows = self._normalize(ledger_rows, "ledger", now) + (self._normalize(window_rows, "windows", now) if sync_windows else [])
                if complete_kinds is not None and (not isinstance(complete_kinds, list) or any(kind not in ALL_KINDS for kind in complete_kinds)):
                    raise NotionError("invalid_input")
                if billing_configured is not None and not isinstance(billing_configured, bool):
                    raise NotionError("invalid_input")
                if billing_configured is False and any(row["kind"] == "billing_month" for row in rows):
                    raise NotionError("invalid_input")
                if active_monthly_schedule is not None and (billing_configured is False
                        or not isinstance(active_monthly_schedule, str) or not re.fullmatch(r"[0-9a-f]{20,64}", active_monthly_schedule)
                        or any(row.get("schedule_id") != active_monthly_schedule for row in rows if row["kind"] == "billing_month")):
                    raise NotionError("invalid_input")
                cutoff = self._now(complete_until or now)
                if cutoff > now:
                    raise NotionError("invalid_input")
                complete = set(complete_kinds or [])
                if not sync_windows:
                    complete -= WINDOW_KINDS.keys()
                if billing_configured is False:
                    complete.discard("billing_month")
                if billing_configured is not None:
                    state["billing_configured"] = billing_configured
                datasets = {row["dataset_id"] for row in rows}
                # A scope describes one actual scanner dataset. Its UUID can
                # differ from the Notion connection's bootstrap dataset.
                if (complete or active_monthly_schedule) and len(datasets) > 1:
                    raise NotionError("invalid_input")
                dataset = next(iter(datasets), self.config["dataset_id"])
                cutoff_keys = {kind: dataset + ":" + kind for kind in complete}
                stale = {kind for kind in complete if state["complete_cutoffs"].get(cutoff_keys[kind]) and
                         cutoff < self._now(state["complete_cutoffs"][cutoff_keys[kind]])}
                # A delayed report produced before a newly accepted schedule
                # cannot reinstate its rows or exclude the current schedule.
                monthly_cutoff = max((self._now(row["scan_cutoff"]) for row in rows if row["kind"] == "billing_month"), default=cutoff)
                schedule_state = state["schedules"].get(dataset, {})
                if active_monthly_schedule and schedule_state.get("cutoff") and monthly_cutoff < self._now(schedule_state["cutoff"]):
                    stale.add("billing_month")
                    active_monthly_schedule = None
                complete -= stale
                rows = [row for row in rows if row["kind"] not in stale]
                # A bounded history can still identify its current window. A
                # Dataset+kind guard prevents a delayed report from reviving
                # a superseded observation, including after a failed create.
                current_groups = {}
                for row in rows:
                    if row["family"] == "windows" and row["state"] == "current":
                        scope = row["dataset_id"] + ":" + row["kind"]
                        current_groups.setdefault(scope, []).append(row)
                current_windows = {scope: values[0] for scope, values in current_groups.items() if len(values) == 1}
                submitted_currents = dict(current_windows)
                stale_windows = set()
                for scope, row in current_windows.items():
                    guard = state["window_currents"].get(scope, {})
                    prior_cutoffs = [self._now(prior.get("scan_cutoff", prior["statistics_until"]))
                                     for prior in state["synced"].values()
                                     if prior["family"] == "windows" and prior["dataset_id"] == row["dataset_id"]
                                     and prior["kind"] == row["kind"]]
                    if (prior_cutoffs and self._now(row["scan_cutoff"]) < max(prior_cutoffs)) or (guard and (
                            self._now(row["scan_cutoff"]) < self._now(guard["cutoff"])
                            or self._now(row["observed_at"]) < self._now(guard["observed_at"])
                            or (row["scan_cutoff"] == guard["cutoff"] and self._key(row) != guard["key"]))):
                        stale_windows.add(scope)
                rows = [row for row in rows if row["dataset_id"] + ":" + row["kind"] not in stale_windows]
                current_windows = {scope: row for scope, row in current_windows.items() if scope not in stale_windows}
                complete -= {kind for kind in WINDOW_KINDS if dataset + ":" + kind in stale_windows}
                active = {self._key(row) for row in rows}
                for row in rows:
                    key = self._key(row)
                    prior = state["pending"].get(key) or state["synced"].get(key)
                    if prior and self._now(prior.get("scan_cutoff", prior["statistics_until"])) > self._now(row["scan_cutoff"]):
                        continue
                    if state["synced"].get(key, {}).get("fingerprint") != self._fingerprint(row) or state["synced"].get(key, {}).get("retired"):
                        state["pending"][key] = row
                    else:
                        state["pending"].pop(key, None)
                        state["synced"][key]["scan_cutoff"] = row["scan_cutoff"]
                # Retired anchor rows cannot remain queued after a failed older write.
                for key, row in list(state["pending"].items()):
                    if (row["dataset_id"] == dataset and row["kind"] in complete and key not in active) or (active_monthly_schedule
                            and row["dataset_id"] == dataset and row["kind"] == "billing_month"
                            and row.get("schedule_id") not in (None, active_monthly_schedule)):
                        state["pending"].pop(key, None)
                self._save(self.state_path, self.state)
                if not self._ready():
                    raise NotionError("not_configured")
                if not force and self.state.get("automatic_sync_paused"):
                    return {**self._status(), "changed": 0}
                if not force and state.get("last_attempt") and (now - self._now(state["last_attempt"])).total_seconds() < 300:
                    return {**self._status(), "changed": 0}
                self._token()
                state["last_attempt"] = now.isoformat()
                # Validate both data sources before any cloud writes.
                families = ("ledger", "windows") if sync_windows else ("ledger",)
                for family in families:
                    self._validate_source(family, self._source(family))
                retire, retire_scopes, superseded = {}, {}, {}
                query_datasets = datasets | {row["dataset_id"] for row in state["pending"].values() if sync_windows or row["family"] == "ledger"}
                if complete or active_monthly_schedule:
                    query_datasets.add(dataset)
                reconciled = state.setdefault("reconciled_datasets", {})
                audits = {}
                for family in families:
                    family_complete = complete & (LEDGER_KINDS.keys() if family == "ledger" else WINDOW_KINDS.keys())
                    if family == "ledger" and active_monthly_schedule:
                        family_complete = family_complete | {"billing_month"}
                    seen = set()
                    for remote_dataset in sorted(query_datasets):
                        keys = sorted(self._key(row) for row in rows if row["family"] == family and row["dataset_id"] == remote_dataset)
                        fingerprint = hashlib.sha256(json.dumps([keys, sorted(family_complete), active_monthly_schedule]).encode()).hexdigest()
                        scope = remote_dataset + ":" + family
                        prior_audit = reconciled.get(scope) or {}
                        audit_due = (force or bool(state.get("error")) or not prior_audit.get("checked_at")
                                     or prior_audit.get("fingerprint") != fingerprint
                                     or any(key not in state["synced"] for key in keys))
                        try:
                            audit_due = audit_due or (now - self._now(prior_audit.get("checked_at"))).total_seconds() >= 86400
                        except NotionError:
                            audit_due = True
                        if not audit_due:
                            continue
                        remote = self._query(self._source(family), filter={"property": "Dataset", "rich_text": {"equals": remote_dataset}})
                        audits[scope] = {"fingerprint": fingerprint, "checked_at": now.isoformat()}
                        for page in remote:
                            if not isinstance(page, dict):
                                raise NotionError("invalid_response")
                            props = page.get("properties", {})
                            if not isinstance(props, dict):
                                raise NotionError("invalid_response")
                            if (props.get("Provider", {}).get("select") or {}).get("name") != "Codex":
                                continue
                            if _plain(props.get("Dataset", {}).get("rich_text")) != remote_dataset:
                                continue
                            display_kind = (props.get("Kind", {}).get("select") or {}).get("name")
                            kind = next((kind for kind, label in ALL_KINDS.items() if label == display_kind), None)
                            if kind not in (LEDGER_KINDS if family == "ledger" else WINDOW_KINDS):
                                continue
                            key = _plain(props.get("Period Key", {}).get("rich_text"))
                            if key in seen:
                                raise NotionError("duplicate_summary")
                            seen.add(key)
                            start, end = _plain(props.get("Start ISO", {}).get("rich_text")), _plain(props.get("End ISO", {}).get("rich_text"))
                            schedule = _plain(props.get("Schedule", {}).get("rich_text")) or None
                            if not start or not end or self._key({"dataset_id": remote_dataset, "kind": kind, "start": start, "end": end, "schedule_id": schedule}) != key:
                                raise NotionError("schema_mismatch")
                            obsolete = remote_dataset == dataset and kind in complete and key not in active
                            if remote_dataset == dataset and kind == "billing_month" and active_monthly_schedule and schedule not in (None, active_monthly_schedule):
                                obsolete = True
                            if obsolete and props.get("Included", {}).get("checkbox"):
                                retire[key] = (notion_id(page.get("id")), family)
                                retire_scopes[key] = remote_dataset + ":" + kind
                            scope = remote_dataset + ":" + str(kind)
                            current = current_windows.get(scope)
                            if family == "windows" and current and (props.get("State", {}).get("select") or {}).get("name") == "进行中":
                                observed = _plain(props.get("Observed At ISO", {}).get("rich_text"))
                                until = _plain(props.get("Statistics Until ISO", {}).get("rich_text"))
                                if not observed or not until:
                                    raise NotionError("schema_mismatch")
                                # Exact remote observation times also protect
                                # against stale input after local cache loss.
                                prior = state["synced"].get(key, {})
                                if (self._now(observed) > self._now(current["observed_at"])
                                        or self._now(until) > self._now(current["scan_cutoff"])
                                        or (prior.get("scan_cutoff") and self._now(prior["scan_cutoff"]) > self._now(current["scan_cutoff"]))):
                                    stale_windows.add(scope)
                                elif key != self._key(current) and key not in active:
                                    superseded[key] = (notion_id(page.get("id")), props, scope)
                # Discovery is finished before any write. A newer remote
                # current window invalidates this old report's whole scope.
                for key, row in list(state["pending"].items()):
                    scope = row["dataset_id"] + ":" + row["kind"]
                    incoming = submitted_currents.get(scope)
                    if (row["family"] == "windows" and scope in stale_windows and incoming
                            and self._now(row["scan_cutoff"]) <= self._now(incoming["scan_cutoff"])
                            and self._now(row["observed_at"]) <= self._now(incoming["observed_at"])):
                        state["pending"].pop(key, None)
                current_windows = {scope: row for scope, row in current_windows.items() if scope not in stale_windows}
                complete -= {kind for kind in WINDOW_KINDS if dataset + ":" + kind in stale_windows}
                retire = {key: value for key, value in retire.items() if retire_scopes[key] not in stale_windows}
                for scope, current in current_windows.items():
                    state["window_currents"][scope] = {"key": self._key(current), "cutoff": current["scan_cutoff"],
                                                      "observed_at": current["observed_at"]}
                # Keep unsent older rows as history rather than dropping their
                # observed usage or allowing an outbox retry to reopen them.
                for row in state["pending"].values():
                    scope = row["dataset_id"] + ":" + row["kind"]
                    guard = state["window_currents"].get(scope)
                    if (row["family"] == "windows" and guard and row["state"] == "current"
                            and self._key(row) != guard["key"]
                            and self._now(row["scan_cutoff"]) <= self._now(guard["cutoff"])
                            and self._now(row["observed_at"]) <= self._now(guard["observed_at"])):
                        row["state"] = "closed"
                        row["scan_cutoff"] = guard["cutoff"]
                        row["note_flags"] = sorted(set(row["note_flags"]) | {"superseded_window", "final_usage_unknown"})
                self._save(self.state_path, self.state)
                for key, (page_id, old_props, scope) in superseded.items():
                    if scope not in current_windows or key in state["pending"]:
                        continue
                    current = current_windows[scope]
                    note = _plain(old_props.get("Note", {}).get("rich_text"))
                    if WINDOW_NOTES["superseded_window"] not in note:
                        note = (note + "\n" + WINDOW_NOTES["superseded_window"]).strip()
                    self._request("PATCH", "/pages/" + page_id, {"properties": {
                        "State": {"select": {"name": "已结束"}}, "Note": {"rich_text": _text(note)},
                        "Synced At": {"date": {"start": now.isoformat()}}}}, idempotent=True)
                    if key in state["synced"]:
                        state["synced"][key]["fingerprint"] = None
                        state["synced"][key]["scan_cutoff"] = current["scan_cutoff"]
                    changed += 1
                    self._save(self.state_path, self.state)
                for key, prior in state["synced"].items():
                    obsolete = prior["kind"] in complete and key not in active
                    if active_monthly_schedule and prior["kind"] == "billing_month" and prior.get("schedule_id") not in (None, active_monthly_schedule):
                        obsolete = True
                    if obsolete and prior["dataset_id"] == dataset and not prior.get("retired"):
                        retire.setdefault(key, (notion_id(prior["page_id"]), prior["family"]))
                for key, (page_id, family) in retire.items():
                    props = {"Included": {"checkbox": False}, "Synced At": {"date": {"start": now.isoformat()}}}
                    if family == "ledger":
                        props["Counted Tokens"] = {"number": 0}
                    self._request("PATCH", "/pages/" + page_id, {"properties": props}, idempotent=True)
                    if key in state["synced"]:
                        state["synced"][key]["retired"] = True
                    changed += 1
                    self._save(self.state_path, self.state)
                for key, row in list(state["pending"].items()):
                    if not sync_windows and row["family"] == "windows":
                        continue
                    changed += int(self._upsert_period(key, row, now))
                for kind in complete:
                    state["complete_cutoffs"][cutoff_keys[kind]] = cutoff.isoformat()
                if active_monthly_schedule:
                    state["schedules"][dataset] = {"schedule_id": active_monthly_schedule,
                                                  "cutoff": monthly_cutoff.isoformat()}
                state["last_sync"] = now.isoformat()
                reconciled.update(audits)
                state.pop("error", None)
            except NotionError as exc:
                self._remember_period_error(exc)
            return {**self._status(), "changed": changed}

    def _status(self):
        state = self.state.get("periods", {})
        error = state.get("error")
        credential = bool(self.state.get("credential_available", False))
        return {"ready": self._ready(), "connected": credential and self._ready(),
                "period_page_url": self.config.get("period_page_url"),
                "accounting_url": self.config.get("accounting_page_url", self.config.get("period_page_url")),
                "period_ledger_url": self.config.get("period_ledger_url"),
                "period_windows_url": self.config.get("period_windows_url"),
                "billing_configured": bool(state.get("billing_configured", False)),
                "billing_status": "已配置" if state.get("billing_configured") else "待配置会员续费时间",
                "pending_count": len(state.get("pending", {})),
                "uncertain_count": sum(key.startswith(("period:", "database:period_")) for key in self.state.get("uncertain_creates", {})),
                "last_sync": state.get("last_sync"), "error": error,
                "automatic_sync_paused": bool(self.state.get("automatic_sync_paused", False))}

    def cached_status(self):
        self.config = self._load(self.config_path, {"dataset_id": "", "bindings": {}})
        self.state = self._load(self.state_path, {"periods": {}, "uncertain_creates": {}})
        return self._status()
