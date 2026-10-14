"""Synthetic regression coverage for exact weekly/monthly local accounting."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "widget"))
from codex_usage import COUNT_FIELDS
from period_accounting import (build_period_report, calendar_week_bounds,
                               billing_boundary, billing_month_bounds, parse_billing_anchor)
from project_store import ProjectRegistry, project_location
from test_codex_usage import event, session, turn, request, legacy, usage

ZONE = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc


def dt(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class PeriodBoundsTests(unittest.TestCase):
    def test_week_uses_shanghai_monday_not_utc_date(self):
        start, end = calendar_week_bounds(dt("2026-10-11T16:00:00Z"))
        self.assertEqual(start.isoformat(), "2026-10-12T00:00:00+08:00")
        self.assertEqual(end - start, timedelta(days=7))
        before, _ = calendar_week_bounds(dt("2026-10-11T15:59:59.999999Z"))
        self.assertEqual(before.date().isoformat(), "2026-10-05")

    def test_month_clamps_original_31_without_drifting(self):
        anchor = dt("2025-01-31T13:45:06+08:00")
        self.assertEqual(billing_boundary(anchor, 1).isoformat(), "2025-02-28T13:45:06+08:00")
        self.assertEqual(billing_boundary(anchor, 2).isoformat(), "2025-03-31T13:45:06+08:00")
        self.assertEqual(billing_boundary(anchor, 13).day, 28)
        leap = dt("2024-01-31T13:45:06+08:00")
        self.assertEqual(billing_boundary(leap, 1).day, 29)

    def test_month_exact_boundary_is_new_period(self):
        anchor = dt("2026-01-31T13:45:06+08:00")
        start, end = billing_month_bounds(dt("2026-02-28T13:45:06+08:00"), anchor)
        self.assertEqual(start.day, 28)
        self.assertEqual(end.day, 31)
        before, old_end = billing_month_bounds(start - timedelta(microseconds=1), anchor)
        self.assertEqual(before.month, 1)
        self.assertEqual(old_end, start)

    def test_aware_required_and_invalid_anchor_not_guessed(self):
        with self.assertRaises(ValueError):
            calendar_week_bounds(datetime(2026, 10, 14))
        for value in (None, "", "bad", "2026-10-01", datetime(2026, 10, 1), 31):
            self.assertIsNone(parse_billing_anchor(value))
        self.assertEqual(parse_billing_anchor("2026-10-01T00:00:00Z").hour, 8)


class PeriodAccountingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "codex"
        self.home.mkdir()
        self.tasks = self.root / "Documents"
        self.alpha = self.tasks / "alpha" / "repo"
        self.beta = self.tasks / "beta"
        self.alpha.mkdir(parents=True)
        self.beta.mkdir(parents=True)

    def write(self, name, entries, archive=False):
        path = self.home / ("archived_sessions" if archive else "sessions") / "1900" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join((row if isinstance(row, str) else json.dumps(row)) + "\n"
                               for row in entries), encoding="utf-8")
        return path

    def report(self, now="2026-10-14T00:00:00Z", anchor="2026-09-15T12:30:00+08:00", **kwargs):
        return build_period_report(self.home, None, dt(now), anchor, self.tasks, **kwargs)

    def assert_partitioned(self, period):
        for field in COUNT_FIELDS:
            for dimension in ("projects", "models", "daily"):
                self.assertEqual(sum(row[field] for row in period[dimension]),
                                 period["totals"][field], (dimension, field))

    def test_month_exact_time_is_not_sum_of_weeks_or_days(self):
        self.write("boundaries.jsonl", [
            session("s", self.alpha, "2026-09-01T00:00:00Z"), turn("t", self.alpha),
            request("2026-09-15T04:29:59Z", "s", "t", "before-month", usage(100)),
            request("2026-09-15T04:30:00Z", "s", "t", "at-month", usage(200)),
            request("2026-10-11T15:59:59Z", "s", "t", "before-week", usage(300)),
            request("2026-10-11T16:00:00Z", "s", "t", "at-week", usage(400)),
            request("2026-10-14T00:00:00Z", "s", "t", "at-cutoff", usage(500)),
        ])
        report = self.report()
        self.assertEqual(report["weekly"]["current"]["total_tokens"], 400)
        self.assertEqual(report["weekly"]["periods"][1]["total_tokens"], 300)
        self.assertEqual(report["monthly"]["current"]["total_tokens"], 900)
        self.assertEqual(report["monthly"]["periods"][1]["total_tokens"], 100)
        self.assertEqual(report["source"]["totals"]["total_tokens"], 1000)
        self.assertEqual(report["monthly"]["current"]["start"], "2026-09-15T04:30:00+00:00")
        self.assertEqual(report["monthly"]["current"]["boundary_basis"], "confirmed_anchor")
        self.assertEqual(report["monthly"]["periods"][1]["boundary_basis"], "extrapolated_schedule")
        for period in report["weekly"]["periods"] + report["monthly"]["periods"]:
            self.assert_partitioned(period)

    def test_legacy_baseline_before_window_survives_full_replay(self):
        self.write("old.jsonl", [
            session("old", self.alpha, "2026-01-01T00:00:00Z"), turn("t", self.alpha),
            legacy("2026-01-01T01:00:00Z", usage(1000, 100, total=1200)),
            legacy("2026-10-12T01:00:00Z", usage(1200, 130, total=1450)),
        ])
        report = self.report()
        week = report["weekly"]["current"]
        self.assertEqual(week["total_tokens"], 250)
        self.assertEqual(week["totals"]["input_tokens"], 200)
        self.assertEqual(week["totals"]["output_tokens"], 30)
        self.assertEqual(week["totals"]["legacy_updates"], 1)
        self.assertIn("legacy_estimated", week["coverage"]["quality"])
        self.assertEqual(report["source"]["totals"]["total_tokens"], 1450)

    def test_response_dedup_archives_forks_and_mixed_cutover(self):
        original = [
            session("parent", self.alpha, "2026-10-10T00:00:00Z"), turn("t", self.alpha),
            legacy("2026-10-12T00:00:00Z", usage(10, 2, total=15)),
            request("2026-10-12T01:00:00Z", "parent", "t", "r", usage(100, 20, 60, 7, 8, total=130)),
            legacy("2026-10-12T02:00:00Z", usage(110, 22, total=145)),
            event("2026-10-12T02:01:00Z", "turn_token_usage", {"usage": usage(9000)}),
            event("2026-10-12T02:02:00Z", "thread_token_usage", {"usage": usage(9000)}),
        ]
        self.write("parent.jsonl", original)
        self.write("archive.jsonl", original, archive=True)
        self.write("fork.jsonl", [
            session("fork", self.beta, "2026-10-12T03:00:00Z", forked_from_id="parent"),
            turn("t", self.beta),
            request("2026-10-12T03:01:00Z", "parent", "t", "r", usage(100, 20, 60, 7, 8, total=130)),
            request("2026-10-12T04:00:00Z", "fork", "t", "new", usage(5, 5, total=12)),
        ])
        week = self.report()["weekly"]["current"]
        self.assertEqual(week["total_tokens"], 157)
        self.assertEqual(week["totals"]["request_records"], 2)
        self.assertEqual(week["totals"]["legacy_updates"], 1)
        self.assertEqual(week["totals"]["cached_input_tokens"], 60)
        self.assertEqual(week["totals"]["reasoning_output_tokens"], 8)
        self.assertEqual(week["sessions_count"], 2)
        by_name = {row["name"]: row["total_tokens"] for row in week["projects"]}
        self.assertEqual(by_name, {"alpha": 145, "beta": 12})
        self.assert_partitioned(week)

    def test_context_at_request_and_unknown_preserved(self):
        self.write("contexts.jsonl", [
            session("s", self.alpha), turn("first", self.alpha, model="model-a"),
            request("2026-10-12T01:00:00Z", "s", "first", "first-r", usage(10)),
            turn("second", self.beta, "2026-10-12T02:00:00Z", model="model-b"),
            request("2026-10-12T03:00:00Z", "s", "second", "second-r", usage(20)),
        ])
        self.write("unknown.jsonl", [
            session("unknown", "", "2026-10-12T04:00:00Z"),
            request("2026-10-12T05:00:00Z", "unknown", None, "u-r", usage(30)),
        ])
        week = self.report()["weekly"]["current"]
        self.assertEqual({row["name"]: row["total_tokens"] for row in week["projects"]},
                         {"alpha": 10, "beta": 20, "未知目录": 30})
        self.assertEqual({row["model"]: row["total_tokens"] for row in week["models"]},
                         {"model-a": 10, "model-b": 20, "unknown": 30})
        self.assert_partitioned(week)

    def test_distinct_sessions_union_across_days_and_project_paths(self):
        self.write("days.jsonl", [
            session("s", self.alpha), turn("t", self.alpha),
            request("2026-10-12T15:59:59Z", "s", "t", "r1", usage(10)),
            request("2026-10-12T16:00:00Z", "s", "t", "r2", usage(20)),
            turn("t2", self.tasks / "alpha" / "other", "2026-10-13T00:00:00Z"),
            request("2026-10-13T01:00:00Z", "s", "t2", "r3", usage(30)),
        ])
        week = self.report()["weekly"]["current"]
        self.assertEqual(week["sessions_count"], 1)
        self.assertEqual(week["projects"][0]["sessions_count"], 1)
        self.assertEqual(len(week["projects"]), 1)
        self.assertEqual(sum(row["sessions_count"] for row in week["daily"]), 2)
        self.assertEqual([row["day"] for row in week["daily"]], ["2026-10-12", "2026-10-13"])

    def test_unconfigured_invalid_or_future_month_is_absent_not_zero(self):
        for value in (None, "not-date", "2026-10-01", "2027-01-01T00:00:00+08:00"):
            monthly = self.report(anchor=value)["monthly"]
            self.assertEqual(monthly["status"], "needs_anchor")
            self.assertIsNone(monthly["current"])
            self.assertEqual(monthly["periods"], [])

    def test_empty_vs_unreadable_are_distinct_and_neither_claims_complete(self):
        empty = self.report()["weekly"]["current"]
        self.assertEqual(empty["coverage"]["status"], "no_observed_usage")
        self.assertFalse(empty["coverage"]["complete_subscription_history"])
        self.write("bad.jsonl", ["{not JSON"])
        partial = self.report()["weekly"]["current"]
        self.assertEqual(partial["coverage"]["status"], "partial_unreadable")
        self.assertEqual(partial["total_tokens"], 0)

    def test_registry_dataset_and_schedule_identity_are_stable(self):
        self.write("id.jsonl", [session("s", self.alpha), turn("t", self.alpha),
                              request("2026-10-12T00:00:00Z", "s", "t", "r", usage(1))])
        data = self.root / "data"
        first = build_period_report(self.home, data, dt("2026-10-14T00:00:00Z"),
                                    "2026-09-15T12:30:00+08:00", self.tasks)
        second = build_period_report(self.home, data, dt("2026-10-14T00:00:00Z"),
                                     "2026-09-15T12:30:00+08:00", self.tasks)
        self.assertEqual(first, second)
        self.assertIsNotNone(first["dataset_id"])
        changed = build_period_report(self.home, data, dt("2026-10-14T00:00:00Z"),
                                      "2026-09-15T12:31:00+08:00", self.tasks)
        self.assertNotEqual(first["monthly"]["current"]["key"], changed["monthly"]["current"]["key"])
        self.assertEqual(first["weekly"]["current"]["key"], changed["weekly"]["current"]["key"])

    def test_report_never_creates_or_modifies_management_registry(self):
        self.write("registry.jsonl", [session("s", self.alpha), turn("t", self.alpha),
                                    request("2026-10-12T00:00:00Z", "s", "t", "r", usage(1))])
        data = self.root / "data"
        build_period_report(self.home, data, dt("2026-10-14T00:00:00Z"), task_root=self.tasks)
        self.assertFalse(data.exists())
        registry = ProjectRegistry(data, self.home)
        key, _ = project_location(str(self.alpha.resolve()), self.tasks)
        known_id = registry.identify(key, "User-chosen project name")
        registry.save()
        before = registry.path.read_bytes()
        report = build_period_report(self.home, data, dt("2026-10-14T00:00:00Z"), task_root=self.tasks)
        self.assertEqual(registry.path.read_bytes(), before)
        self.assertEqual(report["dataset_id"], registry.dataset_id)
        self.assertEqual(report["weekly"]["current"]["projects"][0]["task_id"], known_id)

    def test_registry_for_another_home_does_not_assign_random_report_identity(self):
        self.write("unknown-home.jsonl", [session("s", self.alpha), turn("t", self.alpha),
                                        request("2026-10-12T00:00:00Z", "s", "t", "r", usage(1))])
        data = self.root / "data"
        known_registry = ProjectRegistry(data, self.root / "different-codex-home")
        known_registry.save()
        before = known_registry.path.read_bytes()
        first = build_period_report(self.home, data, dt("2026-10-14T00:00:00Z"), task_root=self.tasks)
        second = build_period_report(self.home, data, dt("2026-10-14T00:00:00Z"), task_root=self.tasks)
        self.assertEqual(first, second)
        self.assertTrue(first["dataset_id"].startswith("local:"))
        self.assertEqual(known_registry.path.read_bytes(), before)
        # Even a caller-created registry has a transient random dataset for
        # this home. Only its saved identity is authoritative for a report.
        transient = ProjectRegistry(data, self.home)
        third = build_period_report(self.home, data, dt("2026-10-14T00:00:00Z"),
                                    task_root=self.tasks, registry=transient)
        self.assertEqual(first, third)
        self.assertEqual(known_registry.path.read_bytes(), before)

    def test_inherited_legacy_prefix_is_not_new_usage_or_unreadable(self):
        self.write("parent-old.jsonl", [
            session("parent", self.alpha, "2026-10-10T00:00:00Z"), turn("t", self.alpha),
            legacy("2026-10-10T01:00:00Z", usage(100, 10, total=120)),
        ])
        self.write("fork-old.jsonl", [
            session("fork", self.beta, "2026-10-12T00:00:00Z", forked_from_id="parent"),
            turn("t", self.beta, "2026-10-12T00:01:00Z"),
            legacy("2026-10-12T00:02:00Z", usage(100, 10, total=120)),
            legacy("2026-10-12T00:03:00Z", usage(110, 11, total=135)),
        ])
        report = self.report()
        week = report["weekly"]["current"]
        self.assertEqual(week["total_tokens"], 15)
        self.assertEqual(report["source"]["coverage"]["inherited_legacy_snapshots"], 1)
        self.assertEqual(week["coverage"]["status"], "observed")
        self.assertEqual(week["coverage"]["quality"], ["legacy_estimated"])

    def test_unknown_fork_baseline_remains_estimate_not_unreadable(self):
        self.write("parent-baseline.jsonl", [session("parent", self.alpha)])
        self.write("fork-baseline.jsonl", [
            session("fork", self.beta, "2026-10-12T00:00:00Z", forked_from_id="parent"),
            turn("t", self.beta, "2026-10-12T00:01:00Z"),
            legacy("2026-10-12T00:02:00Z", usage(100, 10, total=120)),
            legacy("2026-10-12T00:03:00Z", usage(110, 11, total=135)),
        ])
        report = self.report()
        self.assertEqual(report["source"]["coverage"]["legacy_fork_records_skipped"], 1)
        self.assertEqual(report["weekly"]["current"]["total_tokens"], 15)
        self.assertEqual(report["weekly"]["current"]["coverage"]["quality"], ["legacy_estimated"])

    def test_clamped_month_keys_retain_original_anchor_identity(self):
        # Both 30th and 31st clamp to February 28, but schedules diverge later.
        a = self.report(now="2025-03-01T00:00:00Z", anchor="2025-01-30T00:00:00+08:00")
        b = self.report(now="2025-03-01T00:00:00Z", anchor="2025-01-31T00:00:00+08:00")
        self.assertEqual(a["monthly"]["current"]["start"], b["monthly"]["current"]["start"])
        self.assertNotEqual(a["monthly"]["current"]["key"], b["monthly"]["current"]["key"])

    def test_date_precision_midnight_is_explicit_not_exact_payment(self):
        report = self.report(anchor="2026-09-15T12:30:00+08:00", billing_anchor_precision="date")
        self.assertEqual(report["monthly"]["anchor"], "2026-09-14T16:00:00+00:00")
        self.assertIn("不是精确支付时刻", report["monthly"]["anchor_note"])
        self.assertEqual(report["monthly"]["anchor_precision"], "date")
        same_date = self.report(anchor="2026-10-14T23:00:00+08:00", billing_anchor_precision="date")
        self.assertEqual(same_date["monthly"]["status"], "configured")
        self.assertEqual(same_date["monthly"]["anchor"], "2026-10-13T16:00:00+00:00")

    def test_one_full_history_scan_only_and_json_safe_metadata(self):
        from period_accounting import scan_usage as scanner
        self.write("private.jsonl", [
            session("s", self.alpha), turn("t", self.alpha),
            event("2026-10-12T00:00:00Z", "response_item", {"content": "PRIVATE SENTINEL"}),
            request("2026-10-12T01:00:00Z", "s", "t", "r", usage(10)),
        ])
        with mock.patch("period_accounting.scan_usage", wraps=scanner) as scan:
            report = self.report()
        self.assertEqual(scan.call_count, 1)
        self.assertNotIn("since", scan.call_args.kwargs)
        self.assertEqual(scan.call_args.kwargs["until"], dt("2026-10-14T00:00:00Z"))
        text = json.dumps(report)
        self.assertNotIn("PRIVATE SENTINEL", text)
        self.assertNotIn("thread_id", text)
        self.assertNotIn("response_id", text)
        self.assertNotIn(str(self.alpha), text)

    def test_history_bounds_validation(self):
        for value in (0, 121, True, 1.5):
            with self.assertRaises(ValueError):
                self.report(week_count=value)
        with self.assertRaises(ValueError):
            self.report(billing_anchor_precision="guessed")


if __name__ == "__main__":
    unittest.main()
