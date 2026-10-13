"""Regression tests using synthetic Codex rollouts; no personal logs are read."""

from __future__ import annotations

import json
import gzip
import os
import builtins
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from codex_usage import scan_usage


TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def usage(input_tokens=0, output_tokens=0, cached=0, cache_write=0,
          reasoning=0, total=None):
    result = {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": cache_write,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning,
    }
    if total is not None:
        result["total_tokens"] = total
    else:
        result["total_tokens"] = input_tokens + output_tokens
    return result


def event(timestamp, event_type, payload):
    return {"timestamp": timestamp, "type": event_type, "payload": payload}


def session(session_id, cwd, timestamp="2026-10-01T00:00:00Z", **extra):
    return event(timestamp, "session_meta", {
        "id": session_id,
        "cwd": str(cwd),
        "timestamp": timestamp,
        **extra,
    })


def turn(turn_id, cwd, timestamp="2026-10-01T00:01:00Z", model="gpt-5-codex"):
    return event(timestamp, "turn_context", {
        "turn_id": turn_id,
        "cwd": str(cwd),
        "model": model,
    })


def request(timestamp, session_id, turn_id, response_id, token_usage):
    return event(timestamp, "token_usage_record", {
        "thread_id": session_id,
        "turn_id": turn_id,
        "response_id": response_id,
        "usage": token_usage,
    })


def legacy(timestamp, cumulative, last=None):
    return event(timestamp, "event_msg", {
        "type": "token_count",
        "info": {
            "total_token_usage": cumulative,
            "last_token_usage": last,
        },
        "rate_limits": None,
    })


class UsageScannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.codex_home = self.root / "codex"
        self.project_a = self.root / "alpha"
        self.project_b = self.root / "beta"
        self.project_a.mkdir()
        self.project_b.mkdir()

    def write_log(self, name, entries, archived=False):
        directory = self.codex_home / (
            "archived_sessions" if archived else "sessions"
        ) / "1999" / "01" / "01"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text("".join(
            entry + "\n" if isinstance(entry, str)
            else json.dumps(entry) + "\n"
            for entry in entries
        ), encoding="utf-8")
        return path

    def scan(self, **kwargs):
        return scan_usage(self.codex_home, **kwargs)

    def assert_tokens(self, actual, expected):
        for field in TOKEN_FIELDS:
            self.assertEqual(actual[field], expected.get(field, 0), field)

    def project_rows(self, report):
        return {row["project"]: row for row in report["projects"]}

    def assert_project_daily_consistent(self, report):
        rows = report["project_daily"]
        fields = TOKEN_FIELDS + ("request_records", "legacy_updates")
        self.assertEqual(
            [(row["day"], row["project"]) for row in rows],
            sorted((row["day"], row["project"]) for row in rows),
        )
        for row in rows:
            self.assertEqual(
                set(row),
                {"project", "day", "thread_ids", "sessions_count", *fields},
            )
            self.assertEqual(row["thread_ids"], sorted(set(row["thread_ids"])))
            self.assertEqual(row["sessions_count"], len(row["thread_ids"]))
        for field in fields:
            self.assertEqual(sum(row[field] for row in rows), report["totals"][field])
            for project in report["projects"]:
                self.assertEqual(
                    sum(row[field] for row in rows if row["project"] == project["project"]),
                    project[field],
                )
            for day in report["daily"]:
                self.assertEqual(
                    sum(row[field] for row in rows if row["day"] == day["day"]),
                    day[field],
                )
        for project in report["projects"]:
            thread_ids = set().union(*(
                set(row["thread_ids"]) for row in rows
                if row["project"] == project["project"]
            ))
            self.assertEqual(sorted(thread_ids), project["thread_ids"])
            self.assertEqual(len(thread_ids), project["sessions_count"])
        json.dumps(report)

    def test_project_daily_uses_local_midnight_and_distinct_sessions(self):
        self.write_log("cross-midnight.jsonl", [
            session("s", self.project_a),
            turn("a", self.project_a),
            request("2026-10-01T15:59:59Z", "s", "a", "before-midnight", usage(10)),
            request("2026-10-01T16:00:00Z", "s", "a", "after-midnight", usage(20)),
            turn("b", self.project_b, "2026-10-01T16:01:00Z"),
            request("2026-10-01T16:02:00Z", "s", "b", "changed-project", usage(5)),
        ])
        self.write_log("another-session.jsonl", [
            session("a-session", self.project_a),
            request("2026-10-01T16:03:00Z", "a-session", "a", "another", usage(3)),
        ])

        report = self.scan(timezone=timezone(timedelta(hours=8)))
        rows = {(row["day"], row["project"]): row for row in report["project_daily"]}
        project = str(self.project_a.resolve())

        self.assertEqual(rows[("2026-10-01", project)]["total_tokens"], 10)
        self.assertEqual(rows[("2026-10-02", project)]["total_tokens"], 23)
        self.assertEqual(rows[("2026-10-02", project)]["thread_ids"], ["a-session", "s"])
        self.assertEqual(self.project_rows(report)[project]["sessions_count"], 2)
        self.assertEqual(sum(row["sessions_count"] for row in rows.values()
                             if row["project"] == project), 3)
        self.assertEqual(len(report["sessions"]), 2)
        self.assert_project_daily_consistent(report)

    def test_project_daily_preserves_response_owner_across_archive_and_fork(self):
        original = [
            session("parent", self.project_a),
            request("2026-10-01T00:02:00Z", "parent", "t", "shared", usage(10)),
        ]
        self.write_log("original.jsonl", original)
        self.write_log("archive.jsonl", original, archived=True)
        self.write_log("fork.jsonl", [
            session("child", self.project_b, "2026-10-02T00:00:00Z",
                    forked_from_id="parent"),
            request("2026-10-02T00:01:00Z", "parent", "t", "shared", usage(10)),
            request("2026-10-02T00:02:00Z", "child", "t", "new", usage(5)),
        ])

        report = self.scan(timezone=timezone.utc)
        rows = report["project_daily"]

        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[0]["day"], rows[0]["project"], rows[0]["thread_ids"]),
                         ("2026-10-01", str(self.project_a.resolve()), ["parent"]))
        self.assertEqual(rows[0]["total_tokens"], 10)
        self.assertEqual((rows[1]["day"], rows[1]["project"], rows[1]["thread_ids"]),
                         ("2026-10-02", str(self.project_b.resolve()), ["child"]))
        self.assertEqual(rows[1]["total_tokens"], 5)
        self.assertEqual(report["totals"]["request_records"], 2)
        self.assert_project_daily_consistent(report)

    def test_project_daily_keeps_legacy_window_baseline_and_mixed_cutover(self):
        self.write_log("mixed-window.jsonl", [
            session("s", self.project_a),
            legacy("2026-10-01T23:59:00Z", usage(80, 20, cached=40, total=110)),
            legacy("2026-10-02T00:01:00Z", usage(100, 25, cached=50, total=145)),
            turn("new", self.project_b, "2026-10-03T00:00:00Z"),
            request("2026-10-03T00:01:00Z", "s", "new", "native", usage(7, 3, total=12)),
            legacy("2026-10-03T00:01:01Z", usage(107, 28, cached=50, total=157)),
        ])

        report = self.scan(
            since=datetime(2026, 10, 2, tzinfo=timezone.utc),
            until=datetime(2026, 10, 4, tzinfo=timezone.utc),
            timezone=timezone.utc,
        )
        old, new = report["project_daily"]

        self.assert_tokens(old, usage(20, 5, cached=10, total=35))
        self.assertEqual((old["legacy_updates"], old["request_records"]), (1, 0))
        self.assert_tokens(new, usage(7, 3, total=12))
        self.assertEqual((new["legacy_updates"], new["request_records"]), (0, 1))
        self.assertEqual(report["totals"]["total_tokens"], 47)
        self.assertEqual(report["coverage"]["legacy_snapshots_superseded"], 1)
        self.assert_project_daily_consistent(report)

    def test_project_daily_preserves_explicit_total_without_adding_subcategories(self):
        self.write_log("explicit-total-daily.jsonl", [
            session("s", self.project_a),
            request("2026-10-01T00:02:00Z", "s", "t", "r",
                    usage(7, 3, cached=6, cache_write=2, reasoning=2, total=12)),
            event("2026-10-01T00:03:00Z", "thread_token_usage", {"usage": usage(1000)}),
            event("2026-10-01T00:04:00Z", "turn_token_usage", {"usage": usage(1000)}),
        ])

        report = self.scan()
        row = report["project_daily"][0]

        self.assert_tokens(row, usage(7, 3, cached=6, cache_write=2, reasoning=2, total=12))
        self.assertEqual(row["request_records"], 1)
        self.assert_project_daily_consistent(report)

    def test_project_daily_keeps_unknown_project_and_date_without_inventing_dates(self):
        self.write_log("unknown-daily.jsonl", [
            event("2026-10-01T00:00:00Z", "session_meta", {"id": "s"}),
            request(None, "s", "t", "undated", usage(5)),
            request("2026-10-01T00:02:00Z", "s", "t", "dated", usage(3)),
        ])

        complete = self.scan(timezone=timezone.utc)
        bounded = self.scan(until=datetime(2026, 10, 2, tzinfo=timezone.utc),
                            timezone=timezone.utc)

        self.assertEqual([(row["day"], row["project"], row["total_tokens"])
                          for row in complete["project_daily"]],
                         [("2026-10-01", "unknown", 3), ("unknown", "unknown", 5)])
        self.assertEqual(len(bounded["project_daily"]), 1)
        self.assertEqual(bounded["totals"]["total_tokens"], 3)
        self.assert_project_daily_consistent(complete)
        self.assert_project_daily_consistent(bounded)

    def test_new_records_deduplicate_across_archive_copy_and_fork(self):
        shared_usage = usage(100, 20, cached=60, cache_write=5, reasoning=10)
        records = [
            session("parent", self.project_a),
            turn("turn-a", self.project_a),
            request("2026-10-01T00:02:00Z", "parent", "turn-a", "resp-a", shared_usage),
        ]
        self.write_log("parent.jsonl", records)
        self.write_log("archive-copy.jsonl", records, archived=True)
        self.write_log("fork.jsonl", [
            session("child", self.project_a, parent_thread_id="parent"),
            turn("turn-a", self.project_a),
            request("2026-10-01T00:02:00Z", "child", "turn-a", "resp-a", shared_usage),
            request("2026-10-01T00:03:00Z", "child", "turn-a", "resp-b", usage(4, 6)),
        ])

        report = self.scan()

        self.assert_tokens(report["totals"], usage(104, 26, cached=60, cache_write=5, reasoning=10))
        self.assertEqual(report["totals"]["request_records"], 2)
        self.assertEqual(report["totals"]["legacy_updates"], 0)
        self.assertEqual(len(report["projects"]), 1)
        self.assert_tokens(report["projects"][0], report["totals"])
        # The exported report must remain safe for JSON consumers.
        json.dumps(report)

    def test_cached_and_reasoning_subtotals_are_not_added_to_total(self):
        self.write_log("tokens.jsonl", [
            session("s", self.project_a),
            request("2026-10-01T00:02:00Z", "s", "t", "response", {
                "input_tokens": 100,
                "cached_input_tokens": 80,
                "cache_write_input_tokens": 5,
                "output_tokens": 30,
                "reasoning_output_tokens": 20,
                "total_tokens": 130,
            }),
        ])

        report = self.scan()

        self.assertEqual(report["totals"]["total_tokens"], 130)
        self.assertEqual(report["totals"]["cached_input_tokens"], 80)
        self.assertEqual(report["totals"]["reasoning_output_tokens"], 20)

    def test_missing_total_uses_input_plus_output(self):
        token_usage = usage(7, 3, cached=4, reasoning=2)
        del token_usage["total_tokens"]
        self.write_log("missing-total.jsonl", [
            session("s", self.project_a),
            request("2026-10-01T00:02:00Z", "s", "t", "response", token_usage),
        ])

        self.assertEqual(self.scan()["totals"]["total_tokens"], 10)

    def test_explicit_total_is_preserved(self):
        self.write_log("explicit-total.jsonl", [
            session("s", self.project_a),
            request("2026-10-01T00:02:00Z", "s", "t", "r", usage(7, 3, total=12)),
        ])

        self.assertEqual(self.scan()["totals"]["total_tokens"], 12)

    def test_legacy_cumulative_counters_are_differenced(self):
        self.write_log("legacy.jsonl", [
            session("s", self.project_a),
            legacy("2026-10-01T00:02:00Z", usage(8, 2), usage(8, 2)),
            legacy("2026-10-01T00:03:00Z", usage(20, 5), usage(12, 3)),
            legacy("2026-10-01T00:04:00Z", usage(20, 5), usage(12, 3)),
        ])

        report = self.scan()

        self.assert_tokens(report["totals"], usage(20, 5))
        self.assertEqual(report["totals"]["legacy_updates"], 2)
        self.assertEqual(report["totals"]["request_records"], 0)

    def test_mixed_legacy_then_native_records_do_not_drop_history_or_double_count(self):
        self.write_log("migration.jsonl", [
            session("s", self.project_a),
            turn("t", self.project_a),
            legacy("2026-10-01T00:02:00Z", usage(8, 2), usage(8, 2)),
            request("2026-10-01T00:03:00Z", "s", "t", "r", usage(12, 3)),
            legacy("2026-10-01T00:03:01Z", usage(20, 5), usage(12, 3)),
        ])

        report = self.scan()

        self.assert_tokens(report["totals"], usage(20, 5))
        self.assertEqual(report["totals"]["legacy_updates"], 1)
        self.assertEqual(report["totals"]["request_records"], 1)

    def test_copied_native_response_uses_original_owner_time_and_project(self):
        self.write_log("original.jsonl", [
            session("z-parent", self.project_a),
            request("2026-10-01T00:02:00Z", "z-parent", "t", "r", usage(10)),
        ])
        self.write_log("copied.jsonl", [
            session("a-child", self.project_b, "2026-10-03T00:00:00Z",
                    forked_from_id="z-parent"),
            # Fork replay rewrites the outer timestamp, but retains native IDs.
            request("2026-10-03T00:02:00Z", "z-parent", "t", "r", usage(10)),
        ], archived=True)

        window = self.scan(since=datetime(2026, 10, 2, tzinfo=timezone.utc))
        complete = self.scan()

        self.assertEqual(window["totals"]["total_tokens"], 0)
        self.assertEqual(complete["totals"]["total_tokens"], 10)
        self.assertEqual(complete["projects"][0]["project"], str(self.project_a.resolve()))

    def test_legacy_fork_replay_ignores_rewritten_timestamps(self):
        self.write_log("parent.jsonl", [
            session("parent", self.project_a),
            legacy("2026-10-01T00:02:00Z", usage(8, 2)),
            legacy("2026-10-01T00:03:00Z", usage(20, 5)),
        ])
        self.write_log("fork.jsonl", [
            session("child", self.project_b, "2026-10-03T00:00:00Z",
                    forked_from_id="parent"),
            legacy("2026-10-03T00:01:00Z", usage(8, 2)),
            legacy("2026-10-03T00:02:00Z", usage(20, 5)),
            legacy("2026-10-03T00:03:00Z", usage(32, 8)),
        ])

        report = self.scan()
        rows = self.project_rows(report)

        self.assert_tokens(report["totals"], usage(32, 8))
        self.assertEqual(rows[str(self.project_a.resolve())]["total_tokens"], 25)
        self.assertEqual(rows[str(self.project_b.resolve())]["total_tokens"], 15)

    def test_fork_copied_session_meta_cannot_replace_canonical_legacy_owner(self):
        self.write_log("parent.jsonl", [
            session("parent", self.project_a),
            turn("parent-turn", self.project_a),
            legacy("2026-10-01T00:02:00Z", usage(8, 2)),
            legacy("2026-10-01T00:03:00Z", usage(20, 5)),
        ])
        copied_meta = session("parent", self.project_a)
        copied_meta["timestamp"] = "2026-10-03T00:00:01Z"
        self.write_log("child.jsonl", [
            session("child", self.project_b, "2026-10-03T00:00:00Z",
                    forked_from_id="parent"),
            copied_meta,
            turn("parent-turn", self.project_a, "2026-10-03T00:00:02Z"),
            legacy("2026-10-03T00:00:03Z", usage(8, 2)),
            legacy("2026-10-03T00:00:04Z", usage(20, 5)),
            turn("child-turn", self.project_b, "2026-10-03T00:01:00Z"),
            legacy("2026-10-03T00:02:00Z", usage(32, 8)),
        ])

        report = self.scan()
        sessions = {row["thread_id"]: row for row in report["sessions"]}

        self.assertEqual(report["totals"]["total_tokens"], 40)
        self.assertEqual(sessions["parent"]["total_tokens"], 25)
        self.assertEqual(sessions["child"]["total_tokens"], 15)
        self.assertEqual(sessions["child"]["projects"], [str(self.project_b.resolve())])

    def test_fork_copied_session_meta_does_not_make_copied_response_native(self):
        self.write_log("parent.jsonl", [
            session("parent", self.project_a),
            turn("parent-turn", self.project_a),
            request("2026-10-01T00:02:00Z", "parent", "parent-turn", "parent-r", usage(10)),
        ])
        copied_meta = session("parent", self.project_a)
        copied_meta["timestamp"] = "2026-10-03T00:00:01Z"
        self.write_log("child.jsonl", [
            session("child", self.project_b, "2026-10-03T00:00:00Z",
                    forked_from_id="parent"),
            copied_meta,
            turn("parent-turn", self.project_a, "2026-10-03T00:00:02Z"),
            request("2026-10-03T00:00:03Z", "parent", "parent-turn", "parent-r", usage(10)),
            turn("child-turn", self.project_b, "2026-10-03T00:01:00Z"),
            request("2026-10-03T00:02:00Z", "child", "child-turn", "child-r", usage(7)),
        ], archived=True)

        report = self.scan(since=datetime(2026, 10, 2, tzinfo=timezone.utc))
        sessions = {row["thread_id"]: row for row in report["sessions"]}

        self.assertEqual(report["totals"]["total_tokens"], 7)
        self.assertEqual(report["projects"][0]["project"], str(self.project_b.resolve()))
        self.assertEqual(sessions["child"]["total_tokens"], 7)
        self.assertFalse(any("only a copied response" in warning for warning in report["warnings"]))

    def test_missing_fork_parent_omits_uncertain_legacy_but_keeps_native_usage(self):
        self.write_log("orphan-fork.jsonl", [
            session("child", self.project_b, forked_from_id="missing-parent"),
            legacy("2026-10-01T00:02:00Z", usage(8, 2)),
            legacy("2026-10-01T00:03:00Z", usage(20, 5)),
            request("2026-10-01T00:04:00Z", "child", "t", "r", usage(3, 1)),
        ])

        report = self.scan()

        self.assert_tokens(report["totals"], usage(3, 1))
        self.assertEqual(report["totals"]["legacy_updates"], 0)
        self.assertTrue(any("fork" in warning.lower() for warning in report["warnings"]))

    def test_context_full_snapshots_only_reset_baseline_even_when_first_or_decreasing(self):
        full_100 = legacy("2026-10-01T00:01:00Z", usage(total=100))
        full_100["payload"]["info"]["model_context_window"] = 100
        full_50 = legacy("2026-10-01T00:03:00Z", usage(total=50))
        full_50["payload"]["info"]["model_context_window"] = 50
        self.write_log("context-full.jsonl", [
            session("s", self.project_a),
            full_100,
            legacy("2026-10-01T00:02:00Z", usage(8, 2, total=110)),
            full_50,
            legacy("2026-10-01T00:04:00Z", usage(8, 2, total=60)),
        ])

        report = self.scan()

        self.assert_tokens(report["totals"], usage(16, 4))
        self.assertEqual(report["totals"]["legacy_updates"], 2)

    def test_legacy_last_usage_is_not_an_additional_billable_counter(self):
        self.write_log("compact-estimate.jsonl", [
            session("s", self.project_a),
            legacy("2026-10-01T00:02:00Z", usage(8, 2), usage(1000, 1000)),
            legacy("2026-10-01T00:03:00Z", usage(20, 5), usage(1000, 1000)),
        ])

        self.assert_tokens(self.scan()["totals"], usage(20, 5))

    def test_missing_legacy_subcategory_does_not_discard_total_or_rebill_when_restored(self):
        absent_cached = usage(16, 4)
        del absent_cached["cached_input_tokens"]
        self.write_log("legacy-counter-gap.jsonl", [
            session("s", self.project_a),
            legacy("2026-10-01T00:02:00Z", usage(8, 2, cached=4)),
            legacy("2026-10-01T00:03:00Z", absent_cached),
            legacy("2026-10-01T00:04:00Z", usage(24, 6, cached=8)),
            legacy("2026-10-01T00:05:00Z", usage(32, 8, cached=10)),
        ])

        report = self.scan()

        self.assert_tokens(report["totals"], usage(32, 8, cached=6))
        self.assertEqual(report["coverage"]["legacy_resets"], 0)
        self.assertEqual(report["coverage"]["missing_fields"]["cached_input_tokens"], 2)
        self.assertTrue(any("legacy counter delta unavailable" in warning
                            for warning in report["warnings"]))

    def test_first_known_legacy_counter_establishes_baseline_after_absent_field(self):
        initial = usage(8, 2)
        del initial["cached_input_tokens"]
        del initial["reasoning_output_tokens"]
        self.write_log("legacy-new-counter.jsonl", [
            session("s", self.project_a),
            legacy("2026-10-01T00:02:00Z", initial),
            legacy("2026-10-01T00:03:00Z", usage(16, 4, cached=8, reasoning=2)),
            legacy("2026-10-01T00:04:00Z", usage(24, 6, cached=12, reasoning=3)),
        ])

        report = self.scan()

        self.assert_tokens(report["totals"], usage(24, 6, cached=4, reasoning=1))
        for field in ("cached_input_tokens", "reasoning_output_tokens"):
            self.assertEqual(report["coverage"]["missing_fields"][field], 2)

    def test_missing_legacy_input_preserves_explicit_total_and_restores_input_baseline(self):
        absent_input = usage(16, 4)
        del absent_input["input_tokens"]
        self.write_log("legacy-input-gap.jsonl", [
            session("s", self.project_a),
            legacy("2026-10-01T00:02:00Z", usage(8, 2)),
            legacy("2026-10-01T00:03:00Z", absent_input),
            legacy("2026-10-01T00:04:00Z", usage(24, 6)),
            legacy("2026-10-01T00:05:00Z", usage(32, 8)),
        ])

        report = self.scan()

        self.assert_tokens(report["totals"], usage(16, 8, total=40))
        self.assertEqual(report["coverage"]["legacy_resets"], 0)
        self.assertEqual(report["coverage"]["missing_fields"]["input_tokens"], 2)

    def test_window_reads_earlier_history_to_establish_legacy_baseline(self):
        self.write_log("legacy-window.jsonl", [
            session("s", self.project_a),
            legacy("2026-10-01T23:00:00Z", usage(8, 2), usage(8, 2)),
            legacy("2026-10-02T01:00:00Z", usage(20, 5), usage(12, 3)),
        ])

        report = self.scan(
            since=datetime(2026, 10, 2, tzinfo=timezone.utc),
            until=datetime(2026, 10, 3, tzinfo=timezone.utc),
        )

        self.assert_tokens(report["totals"], usage(12, 3))

    def test_window_is_inclusive_at_since_exclusive_at_until(self):
        self.write_log("dates.jsonl", [
            session("s", self.project_a),
            request("2026-10-01T23:59:59Z", "s", "t", "before", usage(1)),
            request("2026-10-02T00:00:00Z", "s", "t", "start", usage(2)),
            request("2026-10-02T23:59:59Z", "s", "t", "inside", usage(4)),
            request("2026-10-03T00:00:00Z", "s", "t", "end", usage(8)),
        ])

        report = self.scan(
            since=datetime(2026, 10, 2, tzinfo=timezone.utc),
            until=datetime(2026, 10, 3, tzinfo=timezone.utc),
        )

        self.assertEqual(report["totals"]["total_tokens"], 6)
        self.assertEqual(report["totals"]["request_records"], 2)

    def test_turn_cwd_attributes_usage_to_correct_project(self):
        self.write_log("switch.jsonl", [
            session("s", self.project_a),
            turn("ta", self.project_a),
            request("2026-10-01T00:02:00Z", "s", "ta", "ra", usage(10)),
            turn("tb", self.project_b, "2026-10-01T00:03:00Z"),
            request("2026-10-01T00:04:00Z", "s", "tb", "rb", usage(20)),
            # Explicit turn IDs must win over the most recently seen context.
            request("2026-10-01T00:05:00Z", "s", "ta", "rc", usage(3)),
        ])

        rows = self.project_rows(self.scan())

        self.assertEqual(rows[str(self.project_a.resolve())]["total_tokens"], 13)
        self.assertEqual(rows[str(self.project_b.resolve())]["total_tokens"], 20)

    def test_context_updates_within_same_turn_preserve_each_responses_project_and_model(self):
        self.write_log("same-turn-context-switch.jsonl", [
            session("s", self.project_a),
            turn("t", self.project_a, model="model-one"),
            request("2026-10-01T00:02:00Z", "s", "t", "first-r", usage(10)),
            turn("t", self.project_b, "2026-10-01T00:03:00Z", model="model-two"),
            request("2026-10-01T00:04:00Z", "s", "t", "second-r", usage(20)),
        ])

        report = self.scan()
        projects = self.project_rows(report)
        models = {row["model"]: row for row in report["models"]}

        self.assertEqual(projects[str(self.project_a.resolve())]["total_tokens"], 10)
        self.assertEqual(projects[str(self.project_b.resolve())]["total_tokens"], 20)
        self.assertEqual(models["model-one"]["total_tokens"], 10)
        self.assertEqual(models["model-two"]["total_tokens"], 20)

    def test_delayed_response_uses_latest_context_seen_for_its_own_turn(self):
        self.write_log("delayed-turn-response.jsonl", [
            session("s", self.project_a),
            turn("t", self.project_a, model="model-one"),
            request("2026-10-01T00:02:00Z", "s", "t", "first-r", usage(10)),
            turn("t", self.project_b, "2026-10-01T00:03:00Z", model="model-two"),
            request("2026-10-01T00:04:00Z", "s", "t", "second-r", usage(20)),
            turn("u", self.project_a, "2026-10-01T00:05:00Z", model="model-three"),
            request("2026-10-01T00:06:00Z", "s", "t", "delayed-r", usage(30)),
        ])

        report = self.scan()
        projects = self.project_rows(report)
        models = {row["model"]: row for row in report["models"]}

        self.assertEqual(projects[str(self.project_a.resolve())]["total_tokens"], 10)
        self.assertEqual(projects[str(self.project_b.resolve())]["total_tokens"], 50)
        self.assertEqual(models["model-one"]["total_tokens"], 10)
        self.assertEqual(models["model-two"]["total_tokens"], 50)

    def test_turn_context_does_not_leak_between_threads_with_matching_turn_ids(self):
        self.write_log("a-context.jsonl", [
            session("first-thread", self.project_a),
            turn("t", self.project_a, model="model-one"),
            request("2026-10-01T00:02:00Z", "first-thread", "t", "first-r", usage(10)),
        ])
        self.write_log("b-no-context.jsonl", [
            session("second-thread", self.project_b),
            request("2026-10-01T00:03:00Z", "second-thread", "t", "second-r", usage(20)),
        ])

        report = self.scan()
        projects = self.project_rows(report)
        models = {row["model"]: row for row in report["models"]}

        self.assertEqual(projects[str(self.project_a.resolve())]["total_tokens"], 10)
        self.assertEqual(projects[str(self.project_b.resolve())]["total_tokens"], 20)
        self.assertEqual(models["model-one"]["total_tokens"], 10)
        self.assertEqual(models["unknown"]["total_tokens"], 20)

    def test_cross_file_context_fallback_uses_preceding_context_despite_path_order(self):
        self.write_log("z-earlier-context.jsonl", [
            session("s", self.project_a, "2026-10-01T00:00:00Z"),
            turn("t", self.project_b, model="earlier-model"),
        ])
        self.write_log("a-later-response.jsonl", [
            session("s", self.project_a, "2026-10-02T00:00:00Z"),
            request("2026-10-02T00:02:00Z", "s", "t", "r", usage(20)),
        ])

        report = self.scan()

        self.assertEqual(report["projects"][0]["project"], str(self.project_b.resolve()))
        self.assertEqual(report["models"][0]["model"], "earlier-model")

    def test_future_turn_context_cannot_reattribute_earlier_response(self):
        self.write_log("future-context.jsonl", [
            session("s", self.project_a),
            request("2026-10-01T00:02:00Z", "s", "t", "r", usage(20)),
            turn("t", self.project_b, "2026-10-01T00:03:00Z", model="later-model"),
        ])

        report = self.scan()

        self.assertEqual(report["projects"][0]["project"], str(self.project_a.resolve()))
        self.assertEqual(report["models"][0]["model"], "unknown")

    def test_physical_order_defines_legacy_to_native_transition_during_clock_rollback(self):
        self.write_log("clock-rollback.jsonl", [
            session("s", self.project_a),
            legacy("2026-10-02T00:02:00Z", usage(16, 4)),
            request("2026-10-01T00:02:00Z", "s", "t", "r", usage(8, 2)),
            legacy("2026-10-02T00:03:00Z", usage(24, 6)),
        ])

        report = self.scan()

        self.assert_tokens(report["totals"], usage(24, 6))
        self.assertEqual(report["totals"]["legacy_updates"], 1)
        self.assertEqual(report["totals"]["request_records"], 1)

    def test_undated_initial_legacy_usage_keeps_physical_position_before_native_transition(self):
        initial = legacy(None, usage(16, 4))
        del initial["timestamp"]
        self.write_log("undated-legacy.jsonl", [
            session("s", self.project_a),
            initial,
            request("2026-10-01T00:02:00Z", "s", "t", "r", usage(8, 2)),
            legacy("2026-10-01T00:03:00Z", usage(24, 6)),
        ])

        report = self.scan()

        self.assert_tokens(report["totals"], usage(24, 6))
        self.assertGreater(report["coverage"]["missing_timestamps"], 0)

    def test_same_thread_rewritten_history_in_second_file_does_not_rebill_inherited_usage(self):
        self.write_log("z-original.jsonl", [
            session("s", self.project_a),
            legacy("2026-10-01T00:02:00Z", usage(8, 2)),
            legacy("2026-10-01T00:03:00Z", usage(16, 4)),
        ])
        self.write_log("a-replayed.jsonl", [
            session("s", self.project_a, "2026-10-02T00:00:00Z"),
            legacy("2026-10-02T00:02:00Z", usage(8, 2)),
            legacy("2026-10-02T00:03:00Z", usage(16, 4)),
            legacy("2026-10-02T00:04:00Z", usage(24, 6)),
        ])

        complete = self.scan()
        second_day = self.scan(
            since=datetime(2026, 10, 2, tzinfo=timezone.utc),
            until=datetime(2026, 10, 3, tzinfo=timezone.utc),
        )

        self.assert_tokens(complete["totals"], usage(24, 6))
        self.assert_tokens(second_day["totals"], usage(8, 2))

    def test_default_timezone_uses_iana_tz_environment_with_historical_dst_rules(self):
        self.write_log("timezone.jsonl", [
            session("s", self.project_a, "2026-01-01T00:00:00Z"),
            request("2026-01-02T04:30:00Z", "s", "t", "r", usage(10)),
        ])

        with mock.patch.dict(os.environ, {"TZ": "America/New_York"}):
            report = self.scan()

        self.assertEqual(report["daily"][0]["day"], "2026-01-01")
        self.assertEqual(report["daily"][0]["total_tokens"], 10)

    def test_git_subdirectory_maps_to_repository_root(self):
        if not shutil.which("git"):
            self.skipTest("git is needed to create a repository fixture")
        subprocess.run(["git", "init", "-q", str(self.project_a)], check=True,
                       capture_output=True)
        cwd = self.project_a / "src" / "feature"
        cwd.mkdir(parents=True)
        self.write_log("git.jsonl", [
            session("s", cwd),
            request("2026-10-01T00:02:00Z", "s", "t", "r", usage(9)),
        ])

        report = self.scan()

        self.assertEqual(report["projects"][0]["project"], str(self.project_a.resolve()))

    def test_worktrees_can_be_grouped_or_kept_separate(self):
        if not shutil.which("git"):
            self.skipTest("git is needed to create worktree fixtures")
        subprocess.run(["git", "init", "-q", str(self.project_a)], check=True,
                       capture_output=True)
        subprocess.run([
            "git", "-C", str(self.project_a), "-c", "user.name=Scanner Test",
            "-c", "user.email=scanner-test@example.invalid", "commit", "-q",
            "--allow-empty", "-m", "Fixture",
        ], check=True, capture_output=True)
        worktree = self.root / "alpha-worktree"
        subprocess.run([
            "git", "-C", str(self.project_a), "worktree", "add", "-q", "-b",
            "fixture-worktree", str(worktree),
        ], check=True, capture_output=True)
        self.write_log("main-tree.jsonl", [
            session("main", self.project_a),
            request("2026-10-01T00:02:00Z", "main", "t", "main-r", usage(5)),
        ])
        self.write_log("worktree.jsonl", [
            session("worktree", worktree),
            request("2026-10-01T00:02:00Z", "worktree", "t", "worktree-r", usage(7)),
        ])

        grouped = self.project_rows(self.scan())
        separate = self.project_rows(self.scan(group_worktrees=False))

        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[str(self.project_a.resolve())]["total_tokens"], 12)
        self.assertEqual(separate[str(self.project_a.resolve())]["total_tokens"], 5)
        self.assertEqual(separate[str(worktree.resolve())]["total_tokens"], 7)

    def test_malformed_and_partial_lines_do_not_hide_later_valid_usage(self):
        self.write_log("damaged.jsonl", [
            session("s", self.project_a),
            "this is not json",
            request("2026-10-01T00:02:00Z", "s", "t", "first", usage(5)),
            '{"type": "token_usage_record",',
            request("2026-10-01T00:03:00Z", "s", "t", "second", usage(7)),
        ])

        report = self.scan()

        self.assertEqual(report["totals"]["total_tokens"], 12)
        self.assertTrue(report["warnings"])

    def test_archived_sessions_are_included(self):
        self.write_log("archived.jsonl", [
            session("s", self.project_a),
            request("2026-10-01T00:02:00Z", "s", "t", "r", usage(5)),
        ], archived=True)

        self.assertEqual(self.scan()["totals"]["total_tokens"], 5)

    def test_gzip_rollout_is_scanned(self):
        path = self.write_log("compressed.jsonl", [
            session("s", self.project_a),
            request("2026-10-01T00:02:00Z", "s", "t", "r", usage(5)),
        ])
        compressed = path.with_suffix(".jsonl.gz")
        with gzip.open(compressed, "wb") as stream:
            stream.write(path.read_bytes())
        path.unlink()

        self.assertEqual(self.scan()["totals"]["total_tokens"], 5)

    def test_plain_rollout_takes_precedence_over_same_stem_zstandard(self):
        path = self.write_log("coexisting.jsonl", [
            session("s", self.project_a),
            request("2026-10-01T00:02:00Z", "s", "t", "r", usage(5)),
        ])
        path.with_suffix(".jsonl.zst").write_bytes(b"intentionally invalid zstd")

        report = self.scan()

        self.assertEqual(report["totals"]["total_tokens"], 5)
        self.assertEqual(report["warnings"], [])

    def test_real_zstandard_rollout_is_decoded_when_dependency_is_available(self):
        try:
            import zstandard
        except ImportError:
            self.skipTest("optional zstandard package is not installed")
        plain = self.write_log("compressed.jsonl", [
            session("s", self.project_a),
            request("2026-10-01T00:02:00Z", "s", "t", "r", usage(10)),
        ])
        plain.with_suffix(".jsonl.zst").write_bytes(
            zstandard.ZstdCompressor().compress(plain.read_bytes())
        )
        plain.unlink()

        report = self.scan()

        self.assertEqual(report["totals"]["total_tokens"], 10)
        self.assertEqual(report["coverage"]["files_skipped"], 0)

    def test_bad_zstandard_file_is_warned_without_hiding_valid_rollout(self):
        try:
            import zstandard  # noqa: F401; establishes the optional decoding path
        except ImportError:
            self.skipTest("optional zstandard package is not installed")
        plain = self.write_log("valid.jsonl", [
            session("s", self.project_a),
            request("2026-10-01T00:02:00Z", "s", "t", "r", usage(10)),
        ])
        plain.with_name("damaged.jsonl.zst").write_bytes(b"not a zstd frame")

        report = self.scan()

        self.assertEqual(report["totals"]["total_tokens"], 10)
        self.assertGreater(report["coverage"]["files_skipped"], 0)
        self.assertTrue(report["warnings"])

    def test_unavailable_zstandard_decoders_produce_explicit_warning(self):
        plain = self.write_log("valid.jsonl", [
            session("s", self.project_a),
            request("2026-10-01T00:02:00Z", "s", "t", "r", usage(10)),
        ])
        plain.with_name("unavailable.jsonl.zst").write_bytes(b"a zstd rollout")
        original_import = builtins.__import__
        original_which = shutil.which

        def without_zstd_imports(name, *args, **kwargs):
            if name == "zstandard" or name == "compression" or name.startswith("compression."):
                raise ImportError("optional zstd decoder disabled for this fixture")
            return original_import(name, *args, **kwargs)

        def without_zstd_executable(command, *args, **kwargs):
            return None if command == "zstd" else original_which(command, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=without_zstd_imports), \
                mock.patch("shutil.which", side_effect=without_zstd_executable):
            report = self.scan()

        self.assertEqual(report["totals"]["total_tokens"], 10)
        self.assertGreater(report["coverage"]["files_skipped"], 0)
        self.assertTrue(any("zstd support unavailable" in warning for warning in report["warnings"]))

    def test_missing_usage_timestamp_is_warned_and_excluded_from_window(self):
        undated = request(None, "s", "t", "undated", usage(5))
        del undated["timestamp"]
        self.write_log("undated.jsonl", [session("s", self.project_a), undated])

        report = self.scan(since=datetime(2026, 10, 1, tzinfo=timezone.utc))

        self.assertEqual(report["totals"]["total_tokens"], 0)
        self.assertTrue(report["warnings"])

    def test_missing_usage_timestamp_can_be_counted_for_all_time(self):
        undated = request(None, "s", "t", "undated", usage(5))
        del undated["timestamp"]
        self.write_log("undated.jsonl", [session("s", self.project_a), undated])

        report = self.scan()

        self.assertEqual(report["totals"]["total_tokens"], 5)
        self.assertTrue(report["warnings"])

    def test_cli_honors_codex_home_environment_and_explicit_override(self):
        self.write_log("environment.jsonl", [
            session("env-session", self.project_a),
            request("2026-10-01T00:02:00Z", "env-session", "t", "env-r", usage(5)),
        ])
        override_home = self.root / "override"
        (override_home / "sessions").mkdir(parents=True)
        records = [
            session("override-session", self.project_b),
            request("2026-10-01T00:02:00Z", "override-session", "t", "override-r", usage(13)),
        ]
        (override_home / "sessions" / "override.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
        )
        env = os.environ.copy()
        env["CODEX_HOME"] = str(self.codex_home)
        command = [sys.executable, str(Path(__file__).with_name("codex_usage.py")),
                   "--all-time", "--format", "json"]

        implicit = subprocess.run(command, env=env, check=True, capture_output=True, text=True)
        explicit = subprocess.run(command + ["--codex-home", str(override_home)],
                                  env=env, check=True, capture_output=True, text=True)

        self.assertEqual(json.loads(implicit.stdout)["totals"]["total_tokens"], 5)
        self.assertEqual(json.loads(explicit.stdout)["totals"]["total_tokens"], 13)

    def test_cli_output_writes_json_report(self):
        self.write_log("output.jsonl", [
            session("s", self.project_a),
            request("2026-10-01T00:02:00Z", "s", "t", "r", usage(5)),
        ])
        output = self.root / "report.json"

        subprocess.run([
            sys.executable, str(Path(__file__).with_name("codex_usage.py")),
            "--codex-home", str(self.codex_home), "--all-time", "--format", "json",
            "--output", str(output),
        ], check=True, capture_output=True, text=True)

        self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["totals"]["total_tokens"], 5)

    def test_empty_home_reports_zero_without_creating_files(self):
        self.codex_home.mkdir()
        report = self.scan()

        self.assert_tokens(report["totals"], {})
        self.assertEqual(report["totals"]["request_records"], 0)
        self.assertEqual(report["totals"]["legacy_updates"], 0)
        self.assertEqual(report["projects"], [])
        self.assertEqual(report["sessions"], [])
        self.assertEqual(list(self.codex_home.iterdir()), [])

    def test_missing_home_is_reported_explicitly(self):
        with self.assertRaisesRegex(ValueError, "does not exist"):
            self.scan()


if __name__ == "__main__":
    unittest.main()
