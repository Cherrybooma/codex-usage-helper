"""Stateful, network-free regressions for the separate account period outbox."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import notion_sync as ns
import period_notion as pn
from test_notion_sync import FakeNotion, PARENT

NOW = datetime(2026, 10, 14, 1, 0, 13, 987654, tzinfo=timezone.utc)
START = datetime(2026, 10, 11, 16, tzinfo=timezone.utc)
END = START + timedelta(days=7)


class FakePeriods(FakeNotion):
    def __init__(self):
        super().__init__()
        self.hide_periods = False

    def _apply(self, method, path, payload, url):
        result = super()._apply(method, path, payload, url)
        if self.hide_periods and method == "POST" and path.endswith("/query"):
            body = result[2]
            body["results"] = [page for page in body["results"] if "Period Key" not in page.get("properties", {})]
        return result

    def period_pages(self, kind=None):
        pages = [page for page in self.pages.values() if "Period Key" in page.get("properties", {})]
        if kind:
            pages = [page for page in pages if page["properties"]["Kind"]["select"]["name"] == kind]
        return pages


class PeriodNotionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.api = FakePeriods()
        self.now = NOW
        self.ws = pn.PeriodNotionWorkspace(self.temp.name, token_provider=lambda: "TEST_SECRET_DO_NOT_PRINT",
                                           transport=self.api, clock=lambda: self.now, sleeper=lambda _: None)
        self.ws.setup(PARENT)
        self.dataset = self.ws.config["dataset_id"]
        self.api.calls.clear()

    def row(self, *, kind="natural_week", start=START, end=END, total=100, until=None):
        return {"dataset_id": self.dataset, "kind": kind, "start": start.isoformat(), "end": end.isoformat(),
                "statistics_until": (until or self.now).isoformat(), "total_tokens": total,
                "input_tokens": 80, "output_tokens": 20, "cached_input_tokens": 50, "reasoning_output_tokens": 12,
                "request_records": 2, "legacy_updates": 0, "sessions_count": 1,
                "quality": "response_records_verified", "coverage": {"files_scanned": 3, "complete": True},
                "projects": [{"display_name": "用量小助手", "total_tokens": total}],
                "models": [{"name": "gpt-test", "total_tokens": total}], "boundary_basis": "natural_week"}

    def window(self):
        start = NOW - timedelta(hours=2)
        return {"dataset_id": self.dataset, "kind": "five_hour", "start": start.isoformat(),
                "end": (start + timedelta(hours=5)).isoformat(), "statistics_until": NOW.isoformat(),
                "observed_at": (NOW - timedelta(seconds=5)).isoformat(), "duration_minutes": 300,
                "used_percent": 30, "remaining_percent": 70, "observations": 3,
                "first_used_percent": 20, "peak_used_percent": 30, "observed_increase_percent": 10,
                "reset_kind": "unknown", "coverage": {"observations_complete": False},
                "boundary_basis": "duration_before_reset"}

    def shifted_window(self, minutes=1, **changes):
        result = self.window()
        for field in ("start", "end", "statistics_until", "observed_at"):
            result[field] = (datetime.fromisoformat(result[field]) + timedelta(minutes=minutes)).isoformat()
        result.update(changes)
        return result

    def sync(self, rows, windows=None, **kwargs):
        return self.ws.sync(rows, windows or [], force=True, **kwargs)

    def test_schema_is_separate_from_project_relations(self):
        for family in ("ledger", "windows"):
            schema = pn.database_schema(family)
            self.assertNotIn("Project", schema)
            self.assertFalse(any("relation" in prop for prop in schema.values()))
        self.assertEqual(len(self.api.databases), 2)

    def test_setup_reuses_databases_after_process_restart(self):
        self.ws.setup(PARENT)
        new = pn.PeriodNotionWorkspace(self.temp.name, token_provider=lambda: "TEST_SECRET_DO_NOT_PRINT",
                                      transport=self.api, clock=lambda: self.now, sleeper=lambda _: None)
        new.setup(PARENT)
        self.assertEqual(len(self.api.databases), 2)

    def test_setup_rejects_replaced_remote_source_and_preserves_bound_destination(self):
        self.sync([self.row()])
        old_source = self.ws.config["period_ledger_data_source_id"]
        database = self.api.databases[self.ws.config["period_ledger_database_id"]]
        replacement = self.api.uid()
        database["data_sources"] = [{"id": replacement}]
        self.api.calls.clear()
        with self.assertRaises(ns.NotionError) as error:
            self.ws.setup(PARENT)
        self.assertEqual(error.exception.code, "invalid_input")
        self.assertEqual(self.ws.config["period_ledger_data_source_id"], old_source)
        self.assertEqual(self.api.period_pages()[0]["parent"]["data_source_id"], old_source)
        self.assertFalse(any(method in ("POST", "PATCH") for method, _, _ in self.api.calls))
        reloaded = pn.PeriodNotionWorkspace(self.temp.name, token_provider=lambda: "TEST_SECRET_DO_NOT_PRINT",
                                           transport=self.api, clock=lambda: self.now, sleeper=lambda _: None)
        self.assertEqual(reloaded.config["period_ledger_data_source_id"], old_source)

    def test_absolute_replacement_and_unchanged_snapshot_skip(self):
        first = self.sync([self.row()])
        second = self.sync([self.row()])
        self.assertEqual((first["changed"], second["changed"]), (1, 0))
        self.assertEqual(self.sync([self.row(total=150)])["changed"], 1)
        page = self.api.period_pages()[0]
        self.assertEqual(page["properties"]["Total Tokens"]["number"], 150)
        self.assertEqual(page["properties"]["Counted Tokens"]["number"], 150)
        self.assertEqual(len(self.api.period_pages()), 1)

    def test_cutoff_with_seconds_and_fraction_is_preserved(self):
        self.sync([self.row()])
        props = self.api.period_pages()[0]["properties"]
        self.assertEqual(ns._plain(props["Statistics Until ISO"]["rich_text"]), NOW.isoformat())
        self.assertEqual(ns._plain(props["Start ISO"]["rich_text"]), START.isoformat())
        self.assertIn("2026-10-12—2026-10-19", ns._plain(props["Name"]["title"]))
        self.assertEqual(props["Start"]["date"]["start"], "2026-10-12T00:00:00+08:00")

    def test_closed_period_is_corrected_after_new_scan(self):
        start, end = START - timedelta(days=7), START
        self.sync([self.row(start=start, end=end)])
        self.now += timedelta(hours=1)
        self.sync([self.row(start=start, end=end, total=125)])
        props = self.api.period_pages()[0]["properties"]
        self.assertEqual(props["State"]["select"]["name"], "已结束")
        self.assertEqual(props["Total Tokens"]["number"], 125)

    def test_missing_month_anchor_is_status_not_zero_month(self):
        result = self.sync([self.row()], billing_configured=False, complete_kinds=["natural_week", "billing_month"])
        self.assertIsNone(result["error"])
        self.assertEqual(result["billing_status"], "待配置会员续费时间")
        self.assertEqual(len(self.api.period_pages("会员月度")), 0)

    def test_monthly_row_without_anchor_is_rejected_before_any_write(self):
        result = self.sync([self.row(kind="billing_month")], billing_configured=False)
        self.assertEqual(result["error"]["code"], "invalid_input")
        self.assertFalse(self.api.calls)

    def test_month_anchor_change_keeps_old_tokens_as_excluded_audit(self):
        old = self.row(kind="billing_month", start=START - timedelta(days=20), end=END + timedelta(days=3))
        self.sync([old], complete_kinds=["billing_month"], billing_configured=True)
        new = self.row(kind="billing_month", start=START - timedelta(days=18), end=END + timedelta(days=5), total=75)
        result = self.sync([new], complete_kinds=["billing_month"], billing_configured=True)
        self.assertIsNone(result["error"])
        pages = self.api.period_pages("会员月度")
        self.assertEqual(len(pages), 2)
        retired = next(page for page in pages if not page["properties"]["Included"]["checkbox"])
        self.assertEqual(retired["properties"]["Total Tokens"]["number"], 100)
        self.assertEqual(retired["properties"]["Counted Tokens"]["number"], 0)
        self.assertEqual(sum(page["properties"]["Counted Tokens"]["number"] for page in pages), 75)

    def test_schedule_change_only_retires_other_schedule_keeps_older_matching_history(self):
        old_schedule, new_schedule = "a" * 20, "b" * 20
        old = {**self.row(kind="billing_month"), "schedule_id": old_schedule}
        past_matching = {**self.row(kind="billing_month", start=START - timedelta(days=500), end=END - timedelta(days=500)),
                         "schedule_id": new_schedule}
        self.sync([old, past_matching], billing_configured=True)
        current = {**self.row(kind="billing_month", total=125), "schedule_id": new_schedule}
        result = self.sync([current], active_monthly_schedule=new_schedule, billing_configured=True)
        self.assertIsNone(result["error"])
        pages = self.api.period_pages("会员月度")
        self.assertEqual(len(pages), 3)
        self.assertEqual(sum(page["properties"]["Included"]["checkbox"] for page in pages), 2)
        retired = next(page for page in pages if not page["properties"]["Included"]["checkbox"])
        self.assertEqual(ns._plain(retired["properties"]["Schedule"]["rich_text"]), old_schedule)
        self.assertEqual(retired["properties"]["Total Tokens"]["number"], 100)

    def test_schedule_change_survives_lost_local_sync_cache(self):
        old_schedule, new_schedule = "a" * 20, "b" * 20
        self.sync([{**self.row(kind="billing_month"), "schedule_id": old_schedule}], billing_configured=True)
        with self.ws._locked():
            self.ws.state["periods"]["synced"] = {}
        result = self.sync([{**self.row(kind="billing_month", total=80), "schedule_id": new_schedule}],
                           billing_configured=True, active_monthly_schedule=new_schedule)
        self.assertIsNone(result["error"])
        self.assertEqual(sum(page["properties"]["Counted Tokens"]["number"] for page in self.api.period_pages()), 80)

    def test_delayed_old_schedule_does_not_reactivate_audit_or_retire_new_schedule(self):
        old_schedule, new_schedule = "a" * 20, "b" * 20
        original = {**self.row(kind="billing_month"), "schedule_id": old_schedule, "scan_cutoff": NOW.isoformat()}
        self.sync([original], billing_configured=True, active_monthly_schedule=old_schedule)
        self.now += timedelta(minutes=5)
        new = {**self.row(kind="billing_month", total=90), "schedule_id": new_schedule, "scan_cutoff": self.now.isoformat()}
        self.sync([new], billing_configured=True, active_monthly_schedule=new_schedule)
        result = self.sync([original], billing_configured=True, active_monthly_schedule=old_schedule)
        self.assertIsNone(result["error"])
        self.assertEqual(result["changed"], 0)
        included = [page for page in self.api.period_pages() if page["properties"]["Included"]["checkbox"]]
        self.assertEqual(len(included), 1)
        self.assertEqual(ns._plain(included[0]["properties"]["Schedule"]["rich_text"]), new_schedule)

    def test_intervening_other_dataset_does_not_remove_schedule_freshness_guard(self):
        old = {**self.row(kind="billing_month"), "schedule_id": "a" * 20, "scan_cutoff": self.now.isoformat()}
        self.sync([old], billing_configured=True, active_monthly_schedule="a" * 20)
        self.now += timedelta(minutes=5)
        new = {**self.row(kind="billing_month", total=90), "schedule_id": "b" * 20, "scan_cutoff": self.now.isoformat()}
        self.sync([new], billing_configured=True, active_monthly_schedule="b" * 20)
        self.now += timedelta(minutes=5)
        source_b = {**self.row(kind="billing_month", total=70), "dataset_id": str(uuid.UUID(int=778)),
                    "schedule_id": "c" * 20, "scan_cutoff": self.now.isoformat()}
        self.sync([source_b], billing_configured=True, active_monthly_schedule="c" * 20)
        late = self.sync([old], billing_configured=True, active_monthly_schedule="a" * 20)
        self.assertIsNone(late["error"])
        self.assertEqual(late["changed"], 0)
        included_a = [page for page in self.api.period_pages() if page["properties"]["Included"]["checkbox"]
                      and ns._plain(page["properties"]["Dataset"]["rich_text"]) == self.dataset]
        self.assertEqual(len(included_a), 1)
        self.assertEqual(included_a[0]["properties"]["Total Tokens"]["number"], 90)
        self.assertEqual(ns._plain(included_a[0]["properties"]["Schedule"]["rich_text"]), "b" * 20)

    def test_single_source_schedule_state_is_upgraded_before_other_source_sync(self):
        cutoff = self.now.isoformat()
        with self.ws._locked():
            state = self.ws._period_state()
            state["schedule_dataset"] = self.dataset
            state["schedule_cutoff"] = cutoff
            state["active_monthly_schedule"] = "b" * 20
        other = {**self.row(kind="billing_month"), "dataset_id": str(uuid.UUID(int=778)), "schedule_id": "c" * 20}
        self.sync([other], billing_configured=True, active_monthly_schedule="c" * 20)
        guard = self.ws.state["periods"]["schedules"][self.dataset]
        self.assertEqual(guard, {"schedule_id": "b" * 20, "cutoff": cutoff})

    def test_limited_history_does_not_retire_older_rows(self):
        old = self.row(start=START - timedelta(days=140), end=END - timedelta(days=140))
        self.sync([old, self.row()])
        self.sync([self.row(total=120)])
        self.assertEqual(len(self.api.period_pages()), 2)
        self.assertTrue(all(page["properties"]["Included"]["checkbox"] for page in self.api.period_pages()))

    def test_reconcile_after_local_sync_cache_loss(self):
        old = self.row(kind="billing_month", start=START - timedelta(days=20), end=END + timedelta(days=3))
        self.sync([old], complete_kinds=["billing_month"])
        with self.ws._locked():
            self.ws.state["periods"]["synced"] = {}
        new = self.row(kind="billing_month", start=START - timedelta(days=18), end=END + timedelta(days=5), total=60)
        result = self.sync([new], complete_kinds=["billing_month"])
        self.assertIsNone(result["error"])
        self.assertEqual(sum(page["properties"]["Counted Tokens"]["number"] for page in self.api.period_pages()), 60)

    def test_duplicate_remote_row_blocks_even_unchanged_complete_snapshot(self):
        self.sync([self.row()], complete_kinds=["natural_week"])
        duplicate = deepcopy(self.api.period_pages()[0])
        duplicate["id"] = self.api.uid()
        self.api.pages[duplicate["id"]] = duplicate
        result = self.sync([self.row()], complete_kinds=["natural_week"])
        self.assertEqual(result["error"]["code"], "duplicate_summary")
        self.assertFalse(any(method == "PATCH" for method, _, _ in self.api.calls))

    def test_duplicate_is_also_detected_with_partial_recent_history(self):
        self.sync([self.row()])
        duplicate = deepcopy(self.api.period_pages()[0])
        duplicate["id"] = self.api.uid()
        self.api.pages[duplicate["id"]] = duplicate
        result = self.sync([self.row()])
        self.assertEqual(result["error"]["code"], "duplicate_summary")

    def test_lost_create_response_is_recovered_by_query_without_second_create(self):
        self.api.schedule(lambda method, path, payload: method == "POST" and path == "/pages",
                          OSError("transport private details"), after=True)
        result = self.sync([self.row()])
        self.assertIsNone(result["error"])
        self.assertEqual(len(self.api.period_pages()), 1)
        self.assertEqual(sum(method == "POST" and url.endswith("/pages") for method, url, _ in self.api.calls), 1)

    def test_uncertain_create_is_not_blindly_retried(self):
        self.api.hide_periods = True
        self.api.schedule(lambda method, path, payload: method == "POST" and path == "/pages",
                          OSError("transport private details"), after=True)
        first, second = self.sync([self.row()]), self.sync([self.row()])
        self.assertEqual((first["error"]["code"], second["error"]["code"]), ("write_uncertain", "write_uncertain"))
        self.assertEqual(len(self.api.period_pages()), 1)
        self.assertEqual(sum(method == "POST" and url.endswith("/pages") for method, url, _ in self.api.calls), 1)
        self.api.hide_periods = False
        result = self.sync([self.row()])
        self.assertIsNone(result["error"])
        self.assertEqual(result["uncertain_count"], 0)

    def test_partial_failure_retains_only_failed_row_for_retry(self):
        self.api.schedule(lambda method, path, payload: method == "POST" and path == "/pages"
                          and payload["properties"]["Kind"]["select"]["name"] == "周额度",
                          (403, {}, {"message": "private server content"}))
        window = self.window()
        window["kind"] = "weekly_quota"
        first = self.sync([self.row()], [window])
        self.assertEqual(first["changed"], 1)
        self.assertEqual(first["pending_count"], 1)
        self.assertEqual(first["error"]["code"], "restricted_resource")
        second = self.sync([self.row()], [window])
        self.assertEqual(second["changed"], 1)
        self.assertEqual(second["pending_count"], 0)
        self.assertEqual(len(self.api.period_pages()), 2)

    def test_complete_dataset_snapshot_does_not_discard_other_dataset_pending_row(self):
        other = {**self.row(), "dataset_id": str(uuid.UUID(int=779))}
        self.api.schedule(lambda method, path, payload: method == "POST" and path == "/pages",
                          (403, {}, {"code": "restricted_resource"}))
        initial = self.sync([other])
        self.assertEqual(initial["pending_count"], 1)
        current = self.sync([self.row()], complete_kinds=["natural_week"], complete_until=self.now)
        self.assertIsNone(current["error"])
        other_pages = [page for page in self.api.period_pages()
                       if ns._plain(page["properties"]["Dataset"]["rich_text"]) == other["dataset_id"]]
        self.assertEqual(len(other_pages), 1)
        self.assertTrue(other_pages[0]["properties"]["Included"]["checkbox"])

    def test_delayed_old_scan_cannot_roll_back_or_retire_newer_snapshot(self):
        self.sync([self.row(total=120)], complete_kinds=["natural_week"], complete_until=NOW)
        result = self.sync([], complete_kinds=["natural_week"], complete_until=NOW - timedelta(hours=1))
        self.assertIsNone(result["error"])
        self.assertEqual(result["changed"], 0)
        self.assertTrue(self.api.period_pages()[0]["properties"]["Included"]["checkbox"])
        self.sync([self.row(total=50, until=NOW - timedelta(hours=1))])
        self.assertEqual(self.api.period_pages()[0]["properties"]["Total Tokens"]["number"], 120)

    def test_closed_period_uses_scan_cutoff_to_reject_old_replay(self):
        start, end = START - timedelta(days=7), START
        old = {**self.row(start=start, end=end, total=100, until=end), "scan_cutoff": NOW.isoformat()}
        self.sync([old])
        self.now += timedelta(minutes=5)
        current = {**self.row(start=start, end=end, total=130, until=end), "scan_cutoff": self.now.isoformat()}
        self.sync([current])
        self.assertEqual(self.sync([old])["changed"], 0)
        self.assertEqual(self.api.period_pages()[0]["properties"]["Total Tokens"]["number"], 130)

    def test_actual_scanner_dataset_may_differ_from_connection_bootstrap(self):
        actual = str(uuid.UUID(int=777))
        old_schedule, new_schedule = "a" * 20, "b" * 20
        old = {**self.row(kind="billing_month"), "dataset_id": actual, "schedule_id": old_schedule}
        self.sync([old], billing_configured=True, active_monthly_schedule=old_schedule)
        new = {**self.row(kind="billing_month", total=85), "dataset_id": actual, "schedule_id": new_schedule}
        result = self.sync([new], billing_configured=True, active_monthly_schedule=new_schedule)
        self.assertIsNone(result["error"])
        self.assertEqual(sum(page["properties"]["Counted Tokens"]["number"] for page in self.api.period_pages()), 85)

    def test_whitelist_discards_private_ids_paths_warnings_and_raw_text(self):
        row = self.row()
        row.update({"session_ids": ["SECRET_SESSION"], "response_id": "SECRET_RESPONSE", "conversation": "SECRET_CHAT",
                    "root": "/Users/private/project", "warning": "SECRET_WARNING"})
        row["coverage"].update({"warnings": ["SECRET_WARNING"], "path": "/Users/private/project"})
        row["projects"].append({"name": "/Users/private/SECRET_PATH", "total_tokens": 0, "session_id": "SECRET_SESSION"})
        row["models"][0].update({"response_id": "SECRET_RESPONSE"})
        self.sync([row], [self.window()])
        payloads = json.dumps([payload for _, _, payload in self.api.calls], ensure_ascii=False)
        persisted = Path(self.ws.state_path).read_text()
        for private in ("SECRET_SESSION", "SECRET_RESPONSE", "SECRET_CHAT", "/Users/private", "SECRET_WARNING", "SECRET_PATH"):
            self.assertNotIn(private, payloads)
            self.assertNotIn(private, persisted)
        self.assertIn("用量小助手", payloads)
        self.assertIn("gpt-test", payloads)

    def test_incomplete_quota_and_real_readings_are_preserved(self):
        result = self.sync([], [self.window()])
        self.assertIsNone(result["error"])
        props = self.api.period_pages()[0]["properties"]
        self.assertEqual(props["Status"]["select"]["name"], "观测不足")
        self.assertEqual(props["Used Percent"]["number"], 30)
        self.assertEqual(props["Remaining Percent"]["number"], 70)
        self.assertIn("连续观测增量 10%", ns._plain(props["Estimate"]["rich_text"]))
        self.assertNotIn("Total Tokens", props)

    def test_replaced_quota_window_is_closed_even_before_original_deadline(self):
        window = {**self.window(), "state": "已结束", "ended_at": (NOW - timedelta(seconds=1)).isoformat(),
                  "note_flags": ["deadline_changed", "counter_decrease", "sampling_gap", "incompatible_bucket", "SECRET_WARNING"]}
        self.assertIsNone(self.sync([], [window])["error"])
        props = self.api.period_pages()[0]["properties"]
        self.assertEqual(props["State"]["select"]["name"], "已结束")
        note = ns._plain(props["Note"]["rich_text"])
        for flag in ("deadline_changed", "counter_decrease", "sampling_gap", "incompatible_bucket"):
            self.assertIn(pn.WINDOW_NOTES[flag], note)
        self.assertIn("原截止时间已被替换", note)
        self.assertIn("按实际 reset 与时长回推", note)
        self.assertNotIn("SECRET_WARNING", note)

    def test_partial_quota_report_closes_superseded_current_without_changing_readings(self):
        old = self.window()
        self.sync([], [old])
        old_page = self.api.period_pages()[0]
        before = deepcopy(old_page["properties"])
        self.now += timedelta(minutes=1)
        current = self.shifted_window(used_percent=0, remaining_percent=100)
        result = self.sync([], [current])
        self.assertIsNone(result["error"])
        self.assertEqual(result["changed"], 2)
        self.assertEqual(old_page["properties"]["State"]["select"]["name"], "已结束")
        for field, value in before.items():
            if field not in {"State", "Note", "Synced At"}:
                self.assertEqual(old_page["properties"][field], value, field)
        note = ns._plain(old_page["properties"]["Note"]["rich_text"])
        self.assertIn(pn.WINDOW_NOTES["superseded_window"], note)
        self.assertIn("切换时刻未保留", note)
        self.assertIn("原 End 仍为旧读数的预计重置时间", note)
        self.assertIn("不能据此确认福利重置", note)
        self.assertEqual(sum(p["properties"]["State"]["select"]["name"] == "进行中" for p in self.api.period_pages()), 1)
        self.assertEqual(self.sync([], [current])["changed"], 0)

    def test_partial_quota_keeps_old_closed_history_and_included_false_unchanged(self):
        old = self.window()
        historical = self.shifted_window(-600, state="已结束")
        self.sync([], [old, historical])
        old_page = next(p for p in self.api.period_pages() if p["properties"]["State"]["select"]["name"] == "进行中")
        old_page["properties"]["Included"] = {"checkbox": False}
        historical_page = next(p for p in self.api.period_pages() if p is not old_page)
        before_history = deepcopy(historical_page)
        self.now += timedelta(minutes=1)
        self.sync([], [self.shifted_window()])
        self.assertEqual(old_page["properties"]["State"]["select"]["name"], "已结束")
        self.assertFalse(old_page["properties"]["Included"]["checkbox"])
        self.assertEqual(historical_page, before_history)
        self.assertEqual(len(self.api.period_pages()), 3)

    def test_superseded_quota_does_not_close_other_kind_or_dataset(self):
        other_dataset = str(uuid.UUID(int=885))
        old = self.window()
        weekly = {**old, "kind": "weekly_quota"}
        other = {**old, "dataset_id": other_dataset}
        self.sync([], [old, weekly, other])
        self.now += timedelta(minutes=1)
        result = self.sync([], [self.shifted_window()])
        self.assertIsNone(result["error"])
        for page in self.api.period_pages():
            props = page["properties"]
            if props["Kind"]["select"]["name"] == "周额度" or ns._plain(props["Dataset"]["rich_text"]) == other_dataset:
                self.assertEqual(props["State"]["select"]["name"], "进行中")

    def test_delayed_quota_report_does_not_reopen_superseded_window(self):
        old = self.window()
        self.sync([], [old])
        self.now += timedelta(minutes=1)
        current = self.shifted_window()
        self.sync([], [current])
        before = deepcopy(self.api.period_pages())
        self.now += timedelta(minutes=1)
        self.assertEqual(self.sync([], [old])["changed"], 0)
        newer_scan_old_observation = {**old, "scan_cutoff": self.now.isoformat()}
        self.assertEqual(self.sync([], [newer_scan_old_observation])["changed"], 0)
        self.assertEqual(self.api.period_pages(), before)

    def test_old_complete_quota_report_cannot_retire_new_window_or_advance_guard(self):
        old = self.window()
        self.sync([], [old])
        self.now += timedelta(minutes=1)
        current = self.shifted_window()
        self.sync([], [current])
        before = deepcopy(self.api.period_pages())
        self.now += timedelta(minutes=1)
        for cutoff in (NOW, self.now):
            with self.subTest(complete_until=cutoff):
                result = self.sync([], [old], complete_kinds=["five_hour"], complete_until=cutoff)
                self.assertIsNone(result["error"])
                self.assertEqual(result["changed"], 0)
                self.assertEqual(self.api.period_pages(), before)
                self.assertNotIn(self.dataset + ":five_hour", self.ws.state["periods"]["complete_cutoffs"])

    def test_supersession_reconciles_remote_current_after_local_cache_loss(self):
        self.sync([], [self.window()])
        with self.ws._locked():
            self.ws.state["periods"]["synced"] = {}
            self.ws.state["periods"]["window_currents"] = {}
        self.now += timedelta(minutes=1)
        result = self.sync([], [self.shifted_window()])
        self.assertIsNone(result["error"])
        self.assertEqual(result["changed"], 2)
        self.assertEqual([p["properties"]["State"]["select"]["name"] for p in self.api.period_pages()], ["已结束", "进行中"])

    def test_newer_remote_current_rejects_old_report_after_cache_loss_including_complete_scope(self):
        old = self.window()
        self.sync([], [old])
        self.now += timedelta(minutes=1)
        self.sync([], [self.shifted_window()])
        before = deepcopy(self.api.period_pages())
        with self.ws._locked():
            self.ws.state["periods"]["synced"] = {}
            self.ws.state["periods"]["window_currents"] = {}
        self.now += timedelta(minutes=1)
        result = self.sync([], [old], complete_kinds=["five_hour"], complete_until=self.now)
        self.assertIsNone(result["error"])
        self.assertEqual(result["changed"], 0)
        self.assertEqual(self.api.period_pages(), before)
        self.assertEqual(result["pending_count"], 0)

    def test_failed_new_current_create_keeps_old_closed_and_old_replay_retries_only_new_row(self):
        old = self.window()
        self.sync([], [old])
        self.now += timedelta(minutes=1)
        current = self.shifted_window()
        self.api.schedule(lambda method, path, payload: method == "POST" and path == "/pages",
                          (403, {}, {"code": "restricted_resource"}))
        first = self.sync([], [current])
        self.assertEqual(first["error"]["code"], "restricted_resource")
        self.assertEqual(first["pending_count"], 1)
        self.assertEqual(self.api.period_pages()[0]["properties"]["State"]["select"]["name"], "已结束")
        self.now += timedelta(minutes=1)
        retry = self.sync([], [old])
        self.assertIsNone(retry["error"])
        self.assertEqual(retry["changed"], 1)
        self.assertEqual(retry["pending_count"], 0)
        self.assertEqual([p["properties"]["State"]["select"]["name"] for p in self.api.period_pages()], ["已结束", "进行中"])

    def test_failed_old_window_create_is_preserved_as_closed_history_in_outbox(self):
        self.api.schedule(lambda method, path, payload: method == "POST" and path == "/pages",
                          (403, {}, {"code": "restricted_resource"}))
        first = self.sync([], [self.window()])
        self.assertEqual(first["pending_count"], 1)
        self.now += timedelta(minutes=1)
        result = self.sync([], [self.shifted_window()])
        self.assertIsNone(result["error"])
        self.assertEqual(result["pending_count"], 0)
        self.assertEqual(result["changed"], 2)
        old, current = self.api.period_pages()
        self.assertEqual(old["properties"]["State"]["select"]["name"], "已结束")
        self.assertEqual(old["properties"]["Used Percent"]["number"], 30)
        self.assertEqual(ns._plain(old["properties"]["Observed At ISO"]["rich_text"]), self.window()["observed_at"])
        self.assertEqual(ns._plain(old["properties"]["Statistics Until ISO"]["rich_text"]), self.window()["statistics_until"])
        self.assertTrue(old["properties"]["Included"]["checkbox"])
        self.assertIn(pn.WINDOW_NOTES["superseded_window"], ns._plain(old["properties"]["Note"]["rich_text"]))
        self.assertEqual(current["properties"]["State"]["select"]["name"], "进行中")

    def test_supersession_patch_failure_can_retry_without_fabricating_readings(self):
        self.sync([], [self.window()])
        old_page = self.api.period_pages()[0]
        self.now += timedelta(minutes=1)
        current = self.shifted_window()
        self.api.schedule(lambda method, path, payload: method == "PATCH" and path.endswith(old_page["id"]),
                          (403, {}, {"code": "restricted_resource"}))
        first = self.sync([], [current])
        self.assertEqual(first["error"]["code"], "restricted_resource")
        self.assertEqual(old_page["properties"]["State"]["select"]["name"], "进行中")
        result = self.sync([], [current])
        self.assertIsNone(result["error"])
        self.assertEqual(result["changed"], 2)
        self.assertEqual(old_page["properties"]["State"]["select"]["name"], "已结束")
        self.assertEqual(old_page["properties"]["Remaining Percent"]["number"], 70)

    def test_no_current_window_or_ambiguous_current_inputs_do_not_guess_supersession(self):
        self.sync([], [self.window()])
        old_page = self.api.period_pages()[0]
        self.now += timedelta(minutes=2)
        self.sync([], [self.shifted_window(1, state="已结束")])
        self.assertEqual(old_page["properties"]["State"]["select"]["name"], "进行中")
        result = self.sync([], [self.shifted_window(1), self.shifted_window(2)])
        self.assertIsNone(result["error"])
        self.assertEqual(old_page["properties"]["State"]["select"]["name"], "进行中")

    def test_older_closed_quota_input_cannot_rollback_newer_remote_reading_after_cache_loss(self):
        closed = {**self.window(), "state": "已结束"}
        self.sync([], [closed])
        self.now += timedelta(minutes=1)
        newer = {**closed, "observed_at": (NOW + timedelta(seconds=30)).isoformat(),
                 "statistics_until": self.now.isoformat(), "used_percent": 40, "remaining_percent": 60}
        self.sync([], [newer])
        before = deepcopy(self.api.period_pages())
        with self.ws._locked():
            self.ws.state["periods"]["synced"] = {}
            self.ws.state["periods"]["window_currents"] = {}
        self.assertEqual(self.sync([], [closed])["changed"], 0)
        self.assertEqual(self.api.period_pages(), before)

    def test_invalid_percentage_or_boolean_count_never_reaches_network(self):
        for change in ({"used_percent": float("nan")}, {"observations": True}, {"used_percent": 50, "remaining_percent": 70}):
            with self.subTest(change=change):
                window = {**self.window(), **change}
                result = self.sync([], [window])
                self.assertEqual(result["error"]["code"], "invalid_input")
                self.assertFalse(self.api.calls)

    def test_period_error_does_not_overwrite_daily_project_error(self):
        with self.ws._locked():
            self.ws.state["error"] = {"code": "network_error", "message": "project issue"}
        result = self.sync([self.row()])
        self.assertIsNone(result["error"])
        self.assertEqual(self.ws.state["error"]["message"], "project issue")

    def test_existing_database_configuration_validates_schema_and_returns_accounting_url(self):
        result = self.ws.configure(PARENT, self.ws.config["period_ledger_data_source_id"],
                                   self.ws.config["period_windows_data_source_id"])
        self.assertEqual(result["accounting_url"], result["period_page_url"])
        self.assertTrue(result["ready"])

    def test_configure_rejects_different_sources_without_changing_synced_destination(self):
        self.sync([self.row()])
        original = {family: self.ws.config[f"period_{family}_data_source_id"] for family in ("ledger", "windows")}
        new_sources = {}
        for family in ("ledger", "windows"):
            old_database = next(database for database in self.api.databases.values()
                                if database["data_sources"][0]["id"] == original[family])
            cloned = deepcopy(old_database)
            cloned["id"], source_id = self.api.uid(), self.api.uid()
            cloned["data_sources"] = [{"id": source_id}]
            self.api.databases[cloned["id"]] = cloned
            new_sources[family] = source_id
        self.api.calls.clear()
        with self.assertRaises(ns.NotionError) as error:
            self.ws.configure(PARENT, new_sources["ledger"], new_sources["windows"])
        self.assertEqual(error.exception.code, "invalid_input")
        self.assertFalse(self.api.calls)
        for family in ("ledger", "windows"):
            self.assertEqual(self.ws.config[f"period_{family}_data_source_id"], original[family])
        self.assertEqual(self.sync([self.row()])["changed"], 0)
        self.assertEqual(self.api.period_pages()[0]["parent"]["data_source_id"], original["ledger"])

    def test_engine_adapter_uses_shared_totals_and_keeps_missing_month_empty(self):
        row = self.row()
        quota = self.window()
        report = {"generated_at": NOW.isoformat(), "weekly": {"periods": [{
            "start": row["start"], "end": row["end"], "observed_until": row["statistics_until"],
            "totals": {counter: row.get(counter, 0) for counter in ns.COUNTERS}, "sessions_count": 1,
            "projects": row["projects"], "models": row["models"], "coverage": row["coverage"]}]},
            "monthly": {"periods": [], "status": "missing_anchor"}, "quota": {"windows": [{
                "kind": "5小时", "start": quota["start"], "end": quota["end"], "observed_until": NOW.isoformat(),
                "last_observed_at": quota["observed_at"], "duration_mins": 300, "used_percent": 30,
                "remaining_percent": 70, "observations": 3, "status": "incomplete", "reset_kind": "未确认"}]}}
        ledgers, windows = pn.report_rows(report, self.dataset)
        self.assertEqual((len(ledgers), len(windows)), (1, 1))
        result = self.sync(ledgers, windows, billing_configured=False)
        self.assertIsNone(result["error"])
        self.assertEqual(self.api.period_pages("自然周")[0]["properties"]["Total Tokens"]["number"], 100)
        self.assertFalse(self.api.period_pages("会员月度"))

    def test_engine_adapter_preserves_real_model_and_coverage_quality(self):
        row = self.row()
        report = {"generated_at": NOW.isoformat(), "weekly": {"periods": [{
            "kind": "calendar_week", "start": row["start"], "end": row["end"], "observed_until": NOW.isoformat(),
            "totals": {counter: row.get(counter, 0) for counter in ns.COUNTERS}, "sessions_count": 1,
            "projects": [{"name": "用量小助手", "task_id": "SECRET_TASK", "total_tokens": 100}],
            "models": [{"model": "gpt-test", "total_tokens": 100}],
            "coverage": {"quality": ["response_records_verified", "legacy_estimated"], "source_files_found": 4,
                         "source_files_scanned": 3, "source_files_skipped": 1, "retained_logs_only": True,
                         "complete_subscription_history": False, "status": "partial_unreadable"}}]},
            "monthly": {"periods": []}}
        ledger, windows = pn.report_rows(report, self.dataset)
        self.assertIsNone(self.sync(ledger, windows, billing_configured=False)["error"])
        props = self.api.period_pages()[0]["properties"]
        self.assertIn("gpt-test", ns._plain(props["Model Summary"]["rich_text"]))
        self.assertIn("按响应记录", ns._plain(props["Quality"]["rich_text"]))
        self.assertIn("含旧累计估算", ns._plain(props["Quality"]["rich_text"]))
        self.assertIn('"source_files_scanned": 3', ns._plain(props["Coverage"]["rich_text"]))
        self.assertNotIn("SECRET_TASK", json.dumps(props))

    def test_cached_token_report_and_fresh_quota_keep_independent_cutoffs(self):
        cached_end = NOW - timedelta(minutes=10)
        row, quota = self.row(until=cached_end), self.window()
        report = {"generated_at": cached_end.isoformat(), "weekly": {"periods": [{
            "start": row["start"], "end": row["end"], "observed_until": row["statistics_until"],
            "totals": {counter: row.get(counter, 0) for counter in ns.COUNTERS}, "sessions_count": 1}]},
            "monthly": {"periods": []}, "quota": {"statistics_until": NOW.isoformat(), "windows": [{
                "kind": "5小时", "start": quota["start"], "end": quota["end"], "observed_until": quota["observed_at"],
                "last_observed_at": quota["observed_at"], "duration_mins": 300, "used_percent": 30,
                "remaining_percent": 70, "observations": 3, "status": "incomplete", "reset_kind": "未确认"}]}}
        ledgers, windows = pn.report_rows(report, self.dataset)
        self.assertEqual(ledgers[0]["scan_cutoff"], cached_end.isoformat())
        self.assertEqual(windows[0]["scan_cutoff"], NOW.isoformat())
        result = self.sync(ledgers, windows, billing_configured=False)
        self.assertIsNone(result["error"])
        self.assertEqual(len(self.api.period_pages()), 2)
        cached = self.api.period_pages("自然周")[0]
        self.assertEqual(ns._plain(cached["properties"]["Statistics Until ISO"]["rich_text"]), cached_end.isoformat())
        # Invalid quota clock must still be rejected rather than make the
        # cached Token ledger appear newer than it actually is.
        report["quota"]["statistics_until"] = cached_end.isoformat()
        ledgers, windows = pn.report_rows(report, self.dataset)
        self.assertEqual(self.sync(ledgers, windows)["error"]["code"], "invalid_input")

    def test_real_engine_contract_can_sync_week_and_month_without_deltas(self):
        from period_accounting import build_period_report
        from test_codex_usage import session, turn, request, usage
        root = Path(self.temp.name)
        home, projects = root / "fixture-codex", root / "fixture-projects"
        project = projects / "用量小助手"
        project.mkdir(parents=True)
        log = home / "sessions" / "1900" / "fixture.jsonl"
        log.parent.mkdir(parents=True)
        entries = [session("PRIVATE_SESSION", project, "2026-10-12T00:00:00Z"), turn("t", project),
                   request("2026-10-12T01:00:00Z", "PRIVATE_SESSION", "t", "PRIVATE_RESPONSE", usage(100, 20, total=125))]
        log.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")
        report = build_period_report(home, None, NOW, "2026-09-15T12:30:00+08:00", projects,
                                     week_count=2, month_count=2)
        ledgers, windows = pn.report_rows(report, self.dataset)
        result = self.sync(ledgers, windows, billing_configured=True,
                           active_monthly_schedule=report["monthly"]["schedule_id"])
        self.assertIsNone(result["error"])
        for kind in ("自然周", "会员月度"):
            current = next(page for page in self.api.period_pages(kind)
                           if page["properties"]["State"]["select"]["name"] == "进行中")
            self.assertEqual(current["properties"]["Total Tokens"]["number"], 125)
            self.assertIn("按响应记录", ns._plain(current["properties"]["Quality"]["rich_text"]))
        payloads = json.dumps([payload for _, _, payload in self.api.calls])
        self.assertNotIn("PRIVATE_SESSION", payloads)
        self.assertNotIn("PRIVATE_RESPONSE", payloads)
        self.assertNotIn(str(root), payloads)


if __name__ == "__main__":
    unittest.main()
