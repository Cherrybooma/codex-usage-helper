"""Meaningful boundaries of display aggregation and quota accounting."""
from datetime import datetime, timedelta, timezone
from contextlib import redirect_stdout
import io
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import subprocess
import json
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from widget_backend import group_tasks, normalize_limits, save_observation, cycle_history, open_history, weekly_budget, WEEK_SECONDS, calendar_week
from widget_backend import build_snapshot, main as backend_main
from widget_backend import estimated_investment, attach_project_investment


class SnapshotIntegrationTests(unittest.TestCase):
    NOW = datetime(2026, 10, 13, 1, tzinfo=timezone.utc)

    def report(self):
        project = "/tmp/widget-root/task/sub"
        return {"range": {"since_inclusive": "2026-10-11T16:00:00+00:00", "until_exclusive": self.NOW.isoformat()},
                "totals": {"total_tokens": 100, "request_records": 1, "legacy_updates": 0},
                "projects": [{"project": project, "total_tokens": 100, "request_records": 1, "thread_ids": ["private-session-id"]}],
                "project_daily": [{"project": project, "day": "2026-10-13", "total_tokens": 100, "request_records": 1, "thread_ids": ["private-session-id"]}],
                "coverage": {"files_found": 1, "files_scanned": 1, "files_skipped": 0, "malformed_lines": 0}, "warnings": []}

    def test_corrupt_notion_cache_is_local_only_error_with_unchanged_tokens(self):
        with tempfile.TemporaryDirectory() as temporary, patch("widget_backend.scan_usage", return_value=self.report()):
            data = Path(temporary)
            (data / "notion-config.json").write_text('{"version":1}')
            (data / "notion-state.json").write_text('{"version":1}')
            snapshot = build_snapshot(data, Path("/tmp/widget-root"), data, False, self.NOW)
            self.assertEqual(snapshot["total_tokens"], 100)
            self.assertEqual(snapshot["notion"]["state"], "error")

    def test_bound_root_cannot_be_silently_regrouped_by_cli_override(self):
        from notion_sync import NotionWorkspace
        with tempfile.TemporaryDirectory() as temporary, patch("widget_backend.scan_usage", return_value=self.report()):
            data = Path(temporary)
            snapshot = build_snapshot(data, Path("/tmp/widget-root"), data, False, self.NOW)
            workspace = NotionWorkspace(data)
            with workspace._locked():
                workspace.config["bindings"][snapshot["available_projects"][0]["task_id"]] = "00000000-0000-0000-0000-000000000001"
            with self.assertRaises(ValueError):
                build_snapshot(data, Path("/tmp/another-root"), data, False, self.NOW)

    def test_sync_cli_config_error_is_structured_and_does_not_emit_private_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            (data / "app-settings.json").write_text('{"notion_sync_enabled":true}')
            (data / "notion-config.json").write_text("PRIVATE_SYNTHETIC_MARKER")
            (data / "notion-state.json").write_text('{}')
            process = subprocess.run([sys.executable, str(Path(__file__).with_name("widget_backend.py")),
                "--sync-notion", "--data-dir", str(data)], capture_output=True, text=True)
            self.assertEqual(process.returncode, 1)
            self.assertFalse(json.loads(process.stdout)["ok"])
            self.assertEqual(process.stderr, "")
            self.assertNotIn("PRIVATE_SYNTHETIC_MARKER", process.stdout)

    def sync_command(self, data, force=False):
        arguments = ["widget_backend.py", "--sync-notion", "--data-dir", str(data)]
        if force:
            arguments.append("--force-sync")
        output = io.StringIO()
        with patch("sys.argv", arguments), redirect_stdout(output):
            code = backend_main()
        return code, json.loads(output.getvalue())

    def test_sync_requires_literal_opt_in_before_workspace_credentials_or_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            # A corrupt previously configured workspace must not be inspected
            # in local mode, nor should a saved connection trigger a request.
            (data / "notion-config.json").write_text("PRIVATE_SYNTHETIC_MARKER")
            with patch("notion_sync.NotionWorkspace", side_effect=AssertionError("workspace accessed")), \
                 patch("notion_sync.keychain_token", side_effect=AssertionError("credential accessed")), \
                 patch("notion_sync.urllib_transport", side_effect=AssertionError("network accessed")):
                cases = [{}, {"notion_sync_enabled": False}, {"notion_sync_enabled": 1},
                         {"notion_sync_enabled": "true"}, {"notion_sync_enabled": None}, []]
                for settings in cases:
                    with self.subTest(settings=settings):
                        (data / "app-settings.json").write_text(json.dumps(settings))
                        code, result = self.sync_command(data)
                        self.assertEqual(code, 1)
                        self.assertFalse(result["ok"])
                        self.assertEqual(result["error"]["code"], "local_mode")

    def test_forced_sync_without_settings_stays_local(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch("notion_sync.NotionWorkspace", side_effect=AssertionError("workspace accessed")):
            code, result = self.sync_command(Path(temporary), force=True)
            self.assertEqual(code, 1)
            self.assertEqual(result["error"]["code"], "local_mode")

    def test_corrupt_opt_in_settings_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch("notion_sync.NotionWorkspace", side_effect=AssertionError("workspace accessed")):
            data = Path(temporary)
            (data / "app-settings.json").write_text("PRIVATE_SYNTHETIC_MARKER")
            code, result = self.sync_command(data)
            self.assertEqual(code, 1)
            self.assertEqual(result["error"]["code"], "local_mode")
            self.assertNotIn("PRIVATE_SYNTHETIC_MARKER", str(result))

    def test_explicit_opt_in_preserves_forced_summary_sync(self):
        with tempfile.TemporaryDirectory() as temporary, patch("period_notion.PeriodNotionWorkspace") as periods, \
             patch("notion_sync.NotionWorkspace") as constructor, \
             patch("notion_sync.keychain_token", side_effect=AssertionError("credential accessed")), \
             patch("notion_sync.urllib_transport", side_effect=AssertionError("network accessed")):
            data = Path(temporary)
            (data / "app-settings.json").write_text('{"notion_sync_enabled":true}')
            saved = {"rows": [{"total_tokens": 100}], "dataset_id": "test-dataset",
                     "statistics_until": self.NOW.isoformat()}
            (data / "notion-daily.json").write_text(json.dumps(saved))
            workspace = constructor.return_value
            # This project-summary test must not depend on optional account
            # pages, nor import their base class for the first time under a mock.
            periods.return_value.status.return_value = {"ready": False}
            workspace.status.return_value = {"ready": True}
            workspace.sync.return_value = {"changed": 1}
            code, result = self.sync_command(data, force=True)
            self.assertEqual(code, 0)
            self.assertTrue(result["ok"])
            constructor.assert_called_once_with(data)
            workspace.projects.assert_called_once_with(force=True)
            self.assertEqual(workspace.sync.call_args.args, (saved["rows"],))
            self.assertTrue(workspace.sync.call_args.kwargs["force"])
            self.assertEqual(workspace.sync.call_args.kwargs["complete_dataset_ids"], ["test-dataset"])
            self.assertEqual(workspace.sync.call_args.kwargs["complete_until"], self.NOW.isoformat())

    def test_snapshot_and_daily_share_counts_without_credentials_or_network(self):
        with tempfile.TemporaryDirectory() as temporary, patch("widget_backend.scan_usage", return_value=self.report()), \
             patch("notion_sync.keychain_token", side_effect=AssertionError("credential accessed")), \
             patch("notion_sync.urllib_transport", side_effect=AssertionError("network accessed")):
            data = Path(temporary)
            snapshot = build_snapshot(data, Path("/tmp/widget-root"), data, False, self.NOW)
            self.assertEqual(snapshot["total_tokens"], 100)
            self.assertEqual(snapshot["tasks"][0]["name"], "task")
            self.assertEqual(snapshot["tasks"][0]["percent"], 100)
            self.assertFalse(snapshot["notion"]["connected"])
            import json
            rows = json.loads((data / "notion-daily.json").read_text())["rows"]
            self.assertEqual(sum(row["total_tokens"] for row in rows), 100)
            self.assertNotIn("private-session-id", str(rows))
            again = build_snapshot(data, Path("/tmp/widget-root"), data, False, self.NOW)
            self.assertEqual(snapshot["tasks"], again["tasks"])

    def test_first_scan_metadata_error_does_not_break_local_usage(self):
        from notion_sync import NotionError
        with tempfile.TemporaryDirectory() as temporary, patch("widget_backend.scan_usage", return_value=self.report()), \
             patch("notion_sync.NotionWorkspace.project_for_task", side_effect=NotionError("invalid_input")):
            data = Path(temporary)
            snapshot = build_snapshot(data, Path("/tmp/widget-root"), data, False, self.NOW)
            self.assertEqual(snapshot["total_tokens"], 100)
            self.assertEqual(snapshot["notion"]["state"], "error")

    def test_cached_management_projects_with_zero_usage_keep_ledger_unchanged(self):
        from notion_sync import NotionWorkspace
        with tempfile.TemporaryDirectory() as temporary, patch("widget_backend.scan_usage", return_value=self.report()), \
             patch("notion_sync.keychain_token", side_effect=AssertionError("credential accessed")), \
             patch("notion_sync.urllib_transport", side_effect=AssertionError("network accessed")):
            data = Path(temporary)
            initial = build_snapshot(data, Path("/tmp/widget-root"), data, False, self.NOW)
            task = initial["available_projects"][0]["task_id"]
            workspace = NotionWorkspace(data, clock=lambda: self.NOW)
            with workspace._locked():
                workspace.config.update({"projects_data_source_id": "00000000-0000-0000-0000-000000000010",
                    "usage_data_source_id": "00000000-0000-0000-0000-000000000011",
                    "bindings": {task: "used", "unseen-local": "zero"}})
                workspace.state["credential_available"] = True
                workspace.state["projects"] = {
                    "used": {"page_id": "used", "display_name": "有用量项目", "status": "开发中", "project_priority": "中", "available": True, "fetched_at": self.NOW.isoformat()},
                    "zero": {"page_id": "zero", "display_name": "优先项目", "status": "开发中", "project_priority": "高", "available": True, "fetched_at": self.NOW.isoformat()},
                    "unbound": {"page_id": "unbound", "display_name": "云端项目", "status": "计划中", "available": True, "fetched_at": self.NOW.isoformat()}}
            managed = build_snapshot(data, Path("/tmp/widget-root"), data, False, self.NOW)
            self.assertEqual(managed["total_tokens"], initial["total_tokens"])
            self.assertEqual(managed["tasks"][0]["total_tokens"], initial["tasks"][0]["total_tokens"])
            self.assertEqual(managed["tasks"][0]["percent"], 100)
            rows = managed["management_projects"]
            self.assertEqual([row["task_id"] for row in rows], ["notion:zero", "notion:used", "notion:unbound"])
            self.assertEqual([row["usage_linked"] for row in rows], [True, True, False])
            self.assertEqual([row["total_tokens"] for row in rows], [0, 100, 0])
            saved = json.loads((data / "notion-daily.json").read_text())["rows"]
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0]["total_tokens"], 100)
            self.assertNotIn("project_goal", saved[0])

    def test_management_cache_failure_falls_back_without_dropping_usage(self):
        with tempfile.TemporaryDirectory() as temporary, patch("widget_backend.scan_usage", return_value=self.report()), \
             patch("notion_sync.NotionWorkspace.cached_projects", side_effect=ValueError("private cache")), \
             patch("notion_sync.NotionWorkspace.status", return_value={"connected": True}):
            snapshot = build_snapshot(Path(temporary), Path("/tmp/widget-root"), Path(temporary), False, self.NOW)
            self.assertIsNone(snapshot["management_projects"])
            self.assertEqual(snapshot["total_tokens"], 100)
            self.assertEqual(snapshot["tasks"][0]["percent"], 100)

    def test_today_snapshot_selects_shanghai_day_and_keeps_total_denominator(self):
        payload = self.report()
        payload["project_daily"] = [dict(payload["project_daily"][0], day="2026-10-13", total_tokens=40), dict(payload["project_daily"][0], day="2026-10-14", total_tokens=60)]
        end = datetime(2026,10,13,16,5,tzinfo=timezone.utc)
        payload["range"]["until_exclusive"] = end.isoformat()
        with tempfile.TemporaryDirectory() as temporary, patch("widget_backend.scan_usage", return_value=payload):
            snapshot = build_snapshot(Path(temporary), Path('/tmp/widget-root'), Path(temporary), False, end)
            self.assertEqual(snapshot["today_tokens"], 60)
            self.assertEqual(snapshot["total_tokens"], 100)
            self.assertEqual(snapshot["tasks"][0]["today_tokens"], 60)
            self.assertEqual(snapshot["tasks"][0]["percent"], 100)

    def test_official_cache_staleness_uses_fixed_snapshot_clock(self):
        import json
        with tempfile.TemporaryDirectory() as temporary, patch("widget_backend.scan_usage", return_value=self.report()):
            data = Path(temporary)
            quota = {"observed_at": self.NOW.isoformat(), "plan": "plus", "source": "test", "limit_id": "codex",
                     "windows": [{"key": "secondary", "label": "weekly", "duration_mins": 10080,
                                  "used_percent": 10, "remaining_percent": 90, "resets_at": self.NOW.timestamp() + 5000}]}
            (data / "official-latest.json").write_text(json.dumps(quota))
            snapshot = build_snapshot(data, Path("/tmp/widget-root"), data, False, self.NOW)
            self.assertFalse(snapshot["quota"]["stale"])
            self.assertEqual(snapshot["quota"]["age_seconds"], 0)

    def test_stored_remaining_is_recomputed_from_used_not_mislabeled(self):
        with tempfile.TemporaryDirectory() as temporary, patch("widget_backend.scan_usage", return_value=self.report()):
            data = Path(temporary)
            quota = {"observed_at": self.NOW.isoformat(), "plan": "plus", "source": "test", "limit_id": "codex",
                     "windows": [{"key": "secondary", "label": "weekly", "duration_mins": 10080,
                                  "used_percent": 69, "remaining_percent": 69,
                                  "resets_at": self.NOW.timestamp() + 5000}]}
            (data / "official-latest.json").write_text(json.dumps(quota))
            snapshot = build_snapshot(data, Path("/tmp/widget-root"), data, False, self.NOW)
            self.assertEqual(snapshot["quota"]["windows"][0]["remaining_percent"], 31)

    def test_invalid_quota_cache_does_not_drop_local_usage_or_print_contents(self):
        for value in ["PRIVATE_SYNTHETIC_CACHE", '["private"]', '{"windows":[{}]}']:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temporary, \
                 patch("widget_backend.scan_usage", return_value=self.report()):
                data = Path(temporary)
                (data / "official-latest.json").write_text(value)
                snapshot = build_snapshot(data, Path("/tmp/widget-root"), data, False, self.NOW)
                self.assertEqual(snapshot["total_tokens"], 100)
                self.assertIsNone(snapshot["quota"])
                self.assertNotIn("PRIVATE", snapshot["quota_error"])

    def test_all_positive_folder_tasks_survive_threshold_and_notion_rollup(self):
        payload = self.report()
        numbers = [70, 10, 8, 5, 4, 3, 0]
        payload["projects"] = [{"project": f"/tmp/widget-root/task-{i}/sub", "total_tokens": amount,
                                "request_records": int(amount > 0), "thread_ids": [f"thread-{i}"]}
                               for i, amount in enumerate(numbers)]
        payload["project_daily"] = [dict(row, day="2026-10-13") for row in payload["projects"]]
        payload["totals"]["request_records"] = 6
        metadata = {"page_id": "shared", "display_name": "同一管理页", "project_goal": "愿景",
                    "development_progress": 65, "progress_source": "ai", "current_goal": "交付已验证成果"}
        with tempfile.TemporaryDirectory() as temporary, patch("widget_backend.scan_usage", return_value=payload), \
             patch("notion_sync.NotionWorkspace") as constructor:
            workspace = constructor.return_value
            workspace.status.return_value = {"connected": True}
            workspace.project_for_task.return_value = metadata
            workspace.cached_projects.return_value = []
            snapshot = build_snapshot(Path(temporary), Path("/tmp/widget-root"), Path(temporary), False, self.NOW)
            self.assertEqual(snapshot["task_count"], 6)
            self.assertEqual(snapshot["request_records"], 6)
            self.assertEqual([row["total_tokens"] for row in snapshot["all_usage_tasks"]], numbers[:-1])
            self.assertEqual(len({row["task_id"] for row in snapshot["all_usage_tasks"]}), 6)
            self.assertEqual(sum(row["total_tokens"] for row in snapshot["all_usage_tasks"]), 100)
            self.assertEqual(sum(row["percent"] for row in snapshot["all_usage_tasks"]), 100)
            self.assertTrue(all(row["development_progress"] == 65 for row in snapshot["all_usage_tasks"]))
            # Existing Notion accounting may merge folders; that cannot change
            # the capsule's folder count or its shares.
            self.assertEqual(len(snapshot["tasks"]), 1)


class InvestmentTests(unittest.TestCase):
    def test_week_reference_and_all_project_shares_conserve_amount(self):
        reference = estimated_investment({"status": "estimated", "usage_quality": "estimated",
                                          "observed_total_percent": 140})
        self.assertEqual(reference["estimated_cost_usd"], 7)
        rows = [{"total_tokens": 882}, {"total_tokens": 100}, {"total_tokens": 18}]
        attach_project_investment(rows, reference, 1000)
        self.assertAlmostEqual(rows[0]["estimated_cost_usd"], 6.174)
        self.assertAlmostEqual(sum(row["estimated_cost_usd"] for row in rows), 7)
        self.assertTrue(all(not row["estimated_cost_lower_bound"] for row in rows))

    def test_partial_observations_keep_explicit_lower_bound_and_unknown_stays_unknown(self):
        reference = estimated_investment({"status": "incomplete", "usage_quality": "lower_bound",
                                          "observed_total_percent": 60})
        self.assertEqual(reference["estimated_cost_usd"], 3)
        self.assertTrue(reference["estimated_cost_lower_bound"])
        for budget in [{}, {"status": "incomplete", "observed_total_percent": 70},
                       {"usage_quality": "unavailable", "observed_total_percent": 130},
                       {"usage_quality": "estimated", "observed_total_percent": float("nan")}]:
            with self.subTest(budget=budget):
                self.assertIsNone(estimated_investment(budget)["estimated_cost_usd"])
        rows = [{"total_tokens": 0, "usage_linked": False}]
        attach_project_investment(rows, reference, 1000)
        self.assertIsNone(rows[0]["estimated_cost_usd"])
        self.assertEqual(rows[0]["estimated_cost_status"], "unavailable")


class WidgetTests(unittest.TestCase):
    def test_nested_tasks_merge_before_five_percent_threshold(self):
        root = Path("/tmp/widget-task-root").resolve()
        def row(path, n):
            return {"project": str(path), "total_tokens": n, "request_records": 1, "thread_ids": [str(path)]}
        major, minor = group_tasks([
            row(root / "main", 90), row(root / "nested" / "a", 3),
            row(root / "nested" / "b", 2), row(root / "small", 4),
            row("/tmp/external-work", 1)], root)
        self.assertEqual([(r["name"],r["total_tokens"]) for r in major],
                         [("main",90),("nested",5),("其他",5)])
        self.assertEqual(sum(r["total_tokens"] for r in major),100)
        self.assertEqual(sum(r["percent"] for r in major),100)
        self.assertEqual(len(minor),2)

    def test_threshold_uses_exact_values_not_display_rounding(self):
        root = Path("/tmp/widget-task-root").resolve()
        major, minor = group_tasks([
            {"project": str(root / "a"), "total_tokens": 95001, "request_records": 1},
            {"project": str(root / "b"), "total_tokens": 4999, "request_records": 1}], root)
        self.assertEqual(minor[0]["name"],"b")
        self.assertEqual(major[-1]["name"],"其他")

    def test_empty_scan_does_not_invent_usage(self):
        self.assertEqual(group_tasks([],Path("/tmp")),([],[]))

    def test_official_multi_bucket_fields_are_whitelisted(self):
        raw={"accountId":"private", "rateLimits":{"primary":{"usedPercent":99}},
             "rateLimitsByLimitId":{"codex":{"planType":"plus", "credits":{"secret":"private"},
                "primary":{"usedPercent":47,"windowDurationMins":300,"resetsAt":2000},
                "secondary":{"usedPercent":19,"windowDurationMins":10080,"resetsAt":3000}}}}
        result=normalize_limits(raw,datetime.fromtimestamp(1000,timezone.utc),"test")
        self.assertEqual(result["windows"][0]["remaining_percent"],53)
        self.assertEqual(result["windows"][1]["remaining_percent"],81)
        self.assertNotIn("private",str(result))
        self.assertNotIn("credits",result)

    def test_unavailable_official_counter_is_not_zero_usage(self):
        with self.assertRaises(ValueError):
            normalize_limits({"rateLimits":{"primary":{"usedPercent":None}}},
                             datetime.now(timezone.utc),"test")

    def test_cycle_rollover_preserves_last_observation_not_exact_unused_claim(self):
        db=open_history(Path(":memory:"))
        try:
            for observed,used,reset in [(1000,20,2000),(1800,75,2000),(2100,5,3000)]:
                q=normalize_limits({"rateLimits":{"primary":{"usedPercent":used,
                         "windowDurationMins":300,"resetsAt":reset}}},
                         datetime.fromtimestamp(observed,timezone.utc),"test")
                save_observation(db,q)
            history=cycle_history(db,datetime.fromtimestamp(2200,timezone.utc))
            self.assertEqual(len(history),1)
            self.assertEqual(history[0]["last_remaining_percent"],25)
            self.assertEqual(history[0]["observations"],2)
            self.assertEqual(history[0]["status"],"last_observed_only")
            self.assertAlmostEqual(history[0]["minutes_before_reset"],200/60,places=1)
        finally:db.close()

    def test_stale_snapshot_cannot_become_observation_of_new_cycle(self):
        db=open_history(Path(":memory:"))
        try:
            q=normalize_limits({"rateLimits":{"primary":{"usedPercent":75,
                     "windowDurationMins":300,"resetsAt":2000}}},
                     datetime.fromtimestamp(2100,timezone.utc),"test")
            save_observation(db,q)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM observations").fetchone()[0],0)
        finally:db.close()

    def test_reset_timestamp_jitter_does_not_create_fake_cycles(self):
        db=open_history(Path(":memory:"))
        try:
            for observed,reset,used in [(1000,2000,20),(1800,2001,75)]:
                q=normalize_limits({"rateLimits":{"primary":{"usedPercent":used,
                         "windowDurationMins":300,"resetsAt":reset}}},
                         datetime.fromtimestamp(observed,timezone.utc),"test")
                save_observation(db,q)
            history=cycle_history(db,datetime.fromtimestamp(2200,timezone.utc))
            self.assertEqual(len(history),1)
            self.assertEqual(history[0]["observations"],2)
        finally:db.close()

    def test_early_window_change_is_not_claimed_as_scheduled_unused_allowance(self):
        db=open_history(Path(":memory:"))
        try:
            for observed,reset,used in [(1000,2500,60),(1800,6000,0)]:
                q=normalize_limits({"rateLimits":{"primary":{"usedPercent":used,
                         "windowDurationMins":300,"resetsAt":reset}}},
                         datetime.fromtimestamp(observed,timezone.utc),"test")
                save_observation(db,q)
            history=cycle_history(db,datetime.fromtimestamp(1900,timezone.utc))
            self.assertEqual(len(history),1)
            self.assertEqual(history[0]["reason"],"window_changed")
            self.assertEqual(history[0]["ended_at"],1800)
            self.assertEqual(history[0]["last_remaining_percent"],40)
            self.assertEqual(history[0]["status"],"last_observed_only")
        finally:db.close()


class WeeklyBudgetTests(unittest.TestCase):
    START = int(datetime(2026, 10, 5, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())

    def setUp(self):
        self.db = open_history(Path(":memory:"))
        self.add(0, 0)

    def tearDown(self):
        self.db.close()

    def add(self, offset, used, *, reset_offset=None, plan="plus", duration=10080, key="secondary"):
        reset = self.START + (WEEK_SECONDS if reset_offset is None else reset_offset)
        quota = {"observed_at": datetime.fromtimestamp(self.START + offset, timezone.utc).isoformat(),
                 "source": "test", "plan": plan, "limit_id": "codex",
                 "windows": [{"key": key, "duration_mins": duration,
                              "resets_at": reset, "used_percent": used}]}
        save_observation(self.db, quota)

    def summary(self, offset, **kwargs):
        return weekly_budget(self.db, datetime.fromtimestamp(self.START + offset, timezone.utc), **kwargs)

    def test_snapshots_count_increments_once_and_ignore_five_hour_bucket(self):
        self.add(60, 30)
        self.add(120, 30)
        self.add(180, 60)
        self.add(180, 100, duration=300, key="primary", reset_offset=300)
        result = self.summary(180)
        self.assertEqual(result["observed_total_percent"], 60)
        self.assertEqual(result["excess_percent"], 0)
        self.assertEqual(result["status"], "estimated")
        self.assertEqual(result["usage_quality"], "estimated")

    def test_full_early_reset_retains_consumption_and_original_deadline(self):
        self.add(60, 100)
        self.add(180, 0, reset_offset=WEEK_SECONDS + 180)
        self.add(240, 100, reset_offset=WEEK_SECONDS + 180)
        result = self.summary(240)
        self.assertEqual(result["observed_total_percent"], 200)
        self.assertEqual(result["excess_percent"], 100)
        self.assertEqual(result["reset_count"], 1)
        self.assertEqual(result["period_end"], self.START + WEEK_SECONDS)

    def test_reset_is_not_itself_an_extra_hundred_percent(self):
        self.add(60, 80)
        self.add(180, 0, reset_offset=WEEK_SECONDS + 180)
        self.add(240, 70, reset_offset=WEEK_SECONDS + 180)
        result = self.summary(240)
        self.assertEqual(result["observed_total_percent"], 150)
        self.assertEqual(result["excess_percent"], 50)
        self.assertEqual(result["status"], "incomplete")

    def test_idle_zero_deadlines_are_not_extra_resets_or_new_consumption(self):
        self.add(60, 0, reset_offset=WEEK_SECONDS + 1000)
        self.add(120, 0, reset_offset=WEEK_SECONDS + 2000)
        result = self.summary(120)
        self.assertEqual(result["reset_count"], 0)
        self.assertEqual(result["observed_total_percent"], 0)
        self.assertEqual(result["usage_quality"], "estimated")

    def test_idle_deadline_rolls_preserve_positive_109_percent_without_167_resets(self):
        self.db.execute("DELETE FROM observations")
        self.db.execute("DELETE FROM observation_context")
        self.add(60, 32, reset_offset=WEEK_SECONDS - 3600)
        self.add(120, 58, reset_offset=WEEK_SECONDS - 3600)
        offset = 3000
        self.add(offset, 0, reset_offset=WEEK_SECONDS + offset)
        for _ in range(166):
            offset += 180
            self.add(offset, 0, reset_offset=WEEK_SECONDS + offset)
        self.add(offset + 60, 83, reset_offset=WEEK_SECONDS + offset)
        before = self.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        result = self.summary(offset + 60)
        self.assertEqual(result["observed_total_percent"], 109)
        self.assertEqual(result["reset_count"], 1)
        self.assertEqual(result["usage_quality"], "lower_bound")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0], before)

    def test_first_post_reset_observation_can_already_contain_usage(self):
        self.add(60, 100)
        self.add(180, 40, reset_offset=WEEK_SECONDS + 180)
        result = self.summary(180)
        self.assertEqual(result["observed_total_percent"], 140)
        self.assertEqual(result["excess_percent"], 40)

    def test_multiple_early_resets_accumulate_and_do_not_move_anchor(self):
        self.add(60, 100)
        self.add(180, 10, reset_offset=WEEK_SECONDS + 180)
        self.add(240, 100, reset_offset=WEEK_SECONDS + 180)
        self.add(360, 30, reset_offset=WEEK_SECONDS + 360)
        result = self.summary(360)
        self.assertEqual(result["observed_total_percent"], 230)
        self.assertEqual(result["excess_percent"], 130)
        self.assertEqual(result["reset_count"], 2)
        self.assertEqual(result["period_start"], self.START)

    def test_midweek_start_is_not_claimed_as_complete_no_overage(self):
        self.db.execute("DELETE FROM observations")
        self.db.execute("DELETE FROM observation_context")
        self.add(3600, 21)
        self.add(3660, 0, reset_offset=WEEK_SECONDS + 3660)
        self.add(3720, 1, reset_offset=WEEK_SECONDS + 3660)
        result = self.summary(3720)
        self.assertEqual(result["observed_total_percent"], 22)
        self.assertIsNone(result["excess_percent"])
        self.assertEqual(result["status"], "incomplete")

    def test_gap_and_stale_or_failed_reads_do_not_report_zero_excess(self):
        self.add(3600, 60)
        result = self.summary(3900)
        self.assertIsNone(result["excess_percent"])
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["usage_quality"], "lower_bound")
        self.assertTrue(any("采集间隔" in n for n in result["notes"]))
        self.assertTrue(any("过期" in n for n in result["notes"]))
        self.add(3960, 70)
        self.assertIsNone(self.summary(3960, read_failed=True)["excess_percent"])

    def test_original_week_rolls_over_without_following_gift_deadline(self):
        self.add(60, 100)
        self.add(120, 0, reset_offset=WEEK_SECONDS + 120)
        self.add(WEEK_SECONDS - 60, 50, reset_offset=WEEK_SECONDS + 120)
        self.summary(WEEK_SECONDS - 60)
        self.add(WEEK_SECONDS + 60, 60, reset_offset=WEEK_SECONDS + 120)
        self.add(WEEK_SECONDS + 180, 5, reset_offset=2 * WEEK_SECONDS + 120)
        result = self.summary(WEEK_SECONDS + 180)
        # The unobserved boundary interval is not guessed or attributed twice.
        self.assertEqual(result["period_start"], self.START + WEEK_SECONDS)
        self.assertEqual(result["period_end"], self.START + 2 * WEEK_SECONDS)
        self.assertEqual(result["observed_total_percent"], 5)
        self.assertEqual(result["reset_count"], 0)
        self.assertIsNone(result["excess_percent"])

    def test_natural_reset_starts_new_week_without_previous_usage(self):
        self.add(60, 100)
        self.summary(60)
        self.add(WEEK_SECONDS + 60, 5, reset_offset=2 * WEEK_SECONDS)
        result = self.summary(WEEK_SECONDS + 60)
        self.assertEqual(result["observed_total_percent"], 5)
        self.assertEqual(result["excess_percent"], 0)
        self.assertEqual(result["reset_count"], 0)

    def test_plan_change_prevents_percentages_of_different_capacities_being_added(self):
        self.add(60, 100)
        self.add(120, 90, plan="pro", reset_offset=WEEK_SECONDS + 120)
        result = self.summary(120)
        self.assertIsNone(result["observed_total_percent"])
        self.assertIsNone(result["excess_percent"])

    def test_counter_drop_and_deadline_shift_do_not_fabricate_full_resets(self):
        self.add(60, 60)
        self.add(120, 20)  # possible partial reset / correction
        self.add(180, 30)
        result = self.summary(180)
        self.assertEqual(result["observed_total_percent"], 70)
        self.assertIsNone(result["excess_percent"])
        self.assertEqual(result["usage_quality"], "unavailable")
        self.add(240, 30, reset_offset=WEEK_SECONDS + 1000)
        result = self.summary(240)
        self.assertEqual(result["reset_count"], 0)
        self.assertEqual(result["observed_total_percent"], 70)

    def test_ambiguous_correction_cannot_prove_excess_even_if_positive_deltas_exceed_100(self):
        self.add(60, 90)
        self.add(120, 40)
        self.add(180, 80)
        result = self.summary(180)
        self.assertEqual(result["observed_total_percent"], 130)
        self.assertIsNone(result["excess_percent"])

    def test_database_migration_preserves_old_counters_and_calendar_period(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            legacy = sqlite3.connect(path)
            legacy.execute("CREATE TABLE observations (observed REAL NOT NULL,window_key TEXT NOT NULL,duration INTEGER NOT NULL,reset INTEGER NOT NULL,used REAL NOT NULL,source TEXT NOT NULL,PRIMARY KEY(observed,window_key))")
            legacy.execute("INSERT INTO observations VALUES (?,?,?,?,?,?)",
                           (self.START + 60,"secondary",10080,self.START+WEEK_SECONDS,21,"official_app_server"))
            legacy.commit()
            legacy.close()
            migrated = open_history(path)
            result = weekly_budget(migrated, datetime.fromtimestamp(self.START + 60,timezone.utc))
            self.assertEqual(result["observed_total_percent"],21)
            self.assertEqual(result["period_end"],self.START+WEEK_SECONDS)
            migrated.close()
            # Reopen after migration, then record a different official deadline.
            reopened = open_history(path)
            save_observation(reopened, {"observed_at": datetime.fromtimestamp(self.START+3600,timezone.utc).isoformat(),
                "source": "test", "windows": [{"key":"secondary","duration_mins":10080,
                    "resets_at":self.START+3600+WEEK_SECONDS,"used_percent":100}]})
            after = weekly_budget(reopened,datetime.fromtimestamp(self.START+3600,timezone.utc))
            self.assertEqual(after["period_end"],self.START+WEEK_SECONDS)
            self.assertEqual(after["observed_total_percent"],121)
            self.assertEqual(reopened.execute("SELECT COUNT(*) FROM observations").fetchone()[0],2)
            reopened.close()

    def test_calendar_week_uses_shanghai_monday_and_sunday(self):
        start, end = calendar_week(datetime(2026,10,7,5,tzinfo=timezone.utc))
        self.assertEqual(start.isoformat(), "2026-10-05T00:00:00+08:00")
        self.assertEqual(end.isoformat(), "2026-10-12T00:00:00+08:00")
        sunday_start, _ = calendar_week(datetime(2026,10,11,15,59,59,tzinfo=timezone.utc))
        monday_start, _ = calendar_week(datetime(2026,10,11,16,tzinfo=timezone.utc))
        self.assertEqual(sunday_start, start)
        self.assertEqual(monday_start, end)

    def test_counter_from_previous_calendar_week_is_not_imported(self):
        self.db.execute("DELETE FROM observations")
        self.db.execute("DELETE FROM observation_context")
        self.add(60, 70, reset_offset=WEEK_SECONDS - 3600)
        self.add(120, 75, reset_offset=WEEK_SECONDS - 3600)
        result = self.summary(120)
        self.assertEqual(result["observed_total_percent"],5)
        self.assertIsNone(result["excess_percent"])

    def test_weekly_window_start_inside_calendar_week_can_seed_observed_use(self):
        self.db.execute("DELETE FROM observations")
        self.db.execute("DELETE FROM observation_context")
        self.add(3600,21,reset_offset=WEEK_SECONDS+1800)
        result = self.summary(3600)
        self.assertEqual(result["observed_total_percent"],21)
        self.assertEqual(result["period_start"],self.START)
        self.assertIsNone(result["excess_percent"])

    def test_saved_old_anchor_cannot_override_calendar_week(self):
        self.db.execute("INSERT INTO budget_anchor VALUES (1,?,?)",(self.START+1800,"secondary"))
        result=self.summary(60)
        self.assertEqual(result["period_start"],self.START)
        self.assertEqual(result["period_end"],self.START+WEEK_SECONDS)

    def test_future_observations_are_excluded(self):
        self.add(60, 50)
        self.add(120, 100)
        result = self.summary(60)
        self.assertEqual(result["observed_total_percent"], 50)

    def test_missing_weekly_data_is_unavailable(self):
        self.db.execute("DELETE FROM observations")
        result = self.summary(0)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["usage_quality"], "unavailable")
        self.assertEqual(result["period_start"], self.START)
        self.assertIsNone(result["excess_percent"])


if __name__ == "__main__":
    unittest.main()
