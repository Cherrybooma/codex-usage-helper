"""Project identity and local summary contracts using only synthetic facts."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from project_store import ProjectRegistry, aggregate, project_location, remember_daily, split_small, management_projects, PROJECT_MANAGEMENT_FIELDS, project_progress


COUNT_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                "output_tokens", "reasoning_output_tokens", "total_tokens",
                "request_records", "legacy_updates")


def fact(project, total, threads, *, day=None, legacy=False, **counts):
    row = {"project": str(project), **dict.fromkeys(COUNT_FIELDS, 0),
           "total_tokens": total, "input_tokens": total,
           "thread_ids": threads, "sessions_count": len(set(threads)),
           "request_records": 0 if legacy else 1, "legacy_updates": 1 if legacy else 0}
    row.update(counts)
    if day is not None:
        row["day"] = day
    return row


def report(projects, daily=(), *, warnings=(), coverage=None):
    return {"projects": projects, "project_daily": list(daily),
            "totals": {"total_tokens": sum(row["total_tokens"] for row in projects)},
            "range": {"since_inclusive": "2026-10-11T16:00:00+00:00",
                      "until_exclusive": "2026-10-13T08:00:00+00:00"},
            "warnings": list(warnings), "coverage": coverage or {}}


class ProjectStoreTests(unittest.TestCase):
    def test_foreign_or_relative_cwd_cannot_charge_current_project(self):
        self.assertEqual(project_location(r"C:\Users\someone\project", self.tasks), ("outside-root", "临时及其他目录"))
        self.assertEqual(project_location("relative/folder", self.tasks), ("outside-root", "临时及其他目录"))

    def test_corrupt_registry_shape_does_not_regenerate_identities(self):
        self.data.mkdir(parents=True, exist_ok=True)
        raw = '{"version":1,"datasets":{},"projects":null}'
        (self.data / "project-registry.json").write_text(raw)
        with self.assertRaises(ValueError):
            ProjectRegistry(self.data, self.root / "codex-one")
        self.assertEqual((self.data / "project-registry.json").read_text(), raw)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.tasks = self.root / "Documents"
        self.tasks.mkdir()
        self.data = self.root / "data"
        self.registry = ProjectRegistry(self.data, self.root / "codex-one")

    def test_project_location_groups_nested_repositories_under_first_folder(self):
        project = self.tasks / "用量小助手"
        self.assertEqual(project_location(str(project / "repo" / "src"), self.tasks),
                         ("folder:" + str(project), "用量小助手"))
        self.assertEqual(project_location(str(project / "other-repo"), self.tasks),
                         ("folder:" + str(project), "用量小助手"))
        other = self.tasks / "个人IP建立"
        self.assertEqual(project_location(str(other / "repo"), self.tasks),
                         ("folder:" + str(other), "个人IP建立"))

    def test_project_location_keeps_root_unknown_and_outside_as_explicit_classes(self):
        self.assertEqual(project_location(str(self.tasks), self.tasks),
                         ("folder:" + str(self.tasks), "文稿根目录"))
        self.assertEqual(project_location("unknown", self.tasks), ("unknown", "未知目录"))
        self.assertEqual(project_location(str(self.root / "external"), self.tasks),
                         ("outside-root", "临时及其他目录"))

    def test_project_identity_survives_reload_and_display_name_change(self):
        key = "folder:" + str(self.tasks / "task")
        original = self.registry.identify(key, "旧项目名称")
        self.registry.save()
        self.assertEqual(self.registry.path.stat().st_mode & 0o777, 0o600)
        reloaded = ProjectRegistry(self.data, self.root / "codex-one")
        self.assertEqual(reloaded.identify(key, "新项目名称"), original)
        self.assertEqual(reloaded.data["projects"][key]["name"], "新项目名称")
        self.assertEqual(reloaded.dataset_id, self.registry.dataset_id)
        self.assertNotEqual(reloaded.identify("folder:" + str(self.tasks / "other"), "新项目名称"),
                            original)

    def test_registry_concurrent_disjoint_additions_merge_without_lost_identity(self):
        self.registry.save()
        one = ProjectRegistry(self.data, self.root / "codex-one")
        two = ProjectRegistry(self.data, self.root / "codex-one")
        first = one.identify("folder:first", "first")
        second = two.identify("folder:second", "second")
        one.save(); two.save()
        current = ProjectRegistry(self.data, self.root / "codex-one")
        self.assertEqual(current.identify("folder:first", "first"), first)
        self.assertEqual(current.identify("folder:second", "second"), second)

    def test_registry_concurrent_same_folder_conflict_never_overwrites_identity(self):
        self.registry.save()
        one = ProjectRegistry(self.data, self.root / "codex-one")
        two = ProjectRegistry(self.data, self.root / "codex-one")
        first = one.identify("folder:same", "same")
        two.identify("folder:same", "same")
        one.save()
        before = self.registry.path.read_bytes()
        with self.assertRaises(ValueError): two.save()
        self.assertEqual(self.registry.path.read_bytes(), before)
        self.assertEqual(ProjectRegistry(self.data, self.root / "codex-one").identify("folder:same", "same"), first)

    def test_registry_retains_root_guard_and_rejects_stale_writer_regrouping(self):
        self.registry.data.update(task_root="/old/root", future_setting="retain")
        self.registry.save()
        one = ProjectRegistry(self.data, self.root / "codex-one")
        two = ProjectRegistry(self.data, self.root / "codex-one")
        one.data["task_root"] = "/new/root"
        one.save()
        with self.assertRaises(ValueError): two.save()
        loaded = json.loads(self.registry.path.read_text())
        self.assertEqual(loaded["task_root"], "/new/root")
        self.assertEqual(loaded["future_setting"], "retain")

    def test_sources_share_task_identity_but_keep_dataset_and_session_keys_distinct(self):
        key = "folder:" + str(self.tasks / "task")
        task_id = self.registry.identify(key, "task")
        self.registry.save()
        other = ProjectRegistry(self.data, self.root / "codex-two")
        self.assertEqual(other.identify(key, "task"), task_id)
        self.assertNotEqual(other.dataset_id, self.registry.dataset_id)
        self.assertNotEqual(other.session_key("same-session"), self.registry.session_key("same-session"))
        self.assertEqual(len(other.session_key("same-session")), 64)
        self.assertNotIn("same-session", other.session_key("same-session"))

    def test_dataset_identity_normalizes_the_same_source_path(self):
        self.registry.save()
        alias = self.root / "codex-one" / ".." / "codex-one"
        same = ProjectRegistry(self.data, alias)
        self.assertEqual(same.dataset_id, self.registry.dataset_id)
        self.assertEqual(same.session_key("session"), self.registry.session_key("session"))

    def test_weekly_and_daily_merge_subfolders_and_union_sessions(self):
        a = self.tasks / "alpha" / "repo-one"
        b = self.tasks / "alpha" / "repo-two"
        facts = [fact(a, 30, ["shared", "a"], request_records=2), fact(b, 20, ["shared", "b"])]
        days = [fact(a, 10, ["shared"], day="2026-10-12"),
                fact(a, 20, ["shared", "a"], day="2026-10-13"),
                fact(b, 20, ["shared", "b"], day="2026-10-13")]

        weekly, daily, sources = aggregate(report(facts, days), self.tasks, self.registry)

        self.assertEqual(len(weekly), 1)
        self.assertEqual((weekly[0]["name"], weekly[0]["total_tokens"], weekly[0]["sessions_count"]),
                         ("alpha", 50, 3))
        self.assertEqual(weekly[0]["percent"], 100)
        self.assertEqual(len(sources), 1)
        self.assertEqual([(row["day"], row["total_tokens"], row["sessions_count"]) for row in daily],
                         [("2026-10-12", 10, 1), ("2026-10-13", 40, 3)])
        self.assertEqual(len(set(daily[0]["session_keys"]) | set(daily[1]["session_keys"])), 3)
        self.assertNotEqual(sum(row["sessions_count"] for row in daily), weekly[0]["sessions_count"])
        self.assertEqual(sum(row["total_tokens"] for row in daily), weekly[0]["total_tokens"])
        for field in COUNT_FIELDS:
            self.assertEqual(sum(row[field] for row in daily), weekly[0][field])
        self.assertNotIn("shared", json.dumps(daily))

    def test_two_folders_bound_to_one_notion_page_merge_weekly_with_session_union(self):
        a = self.tasks / "alpha"
        b = self.tasks / "beta"
        a_id = self.registry.identify("folder:" + str(a), "alpha")
        b_id = self.registry.identify("folder:" + str(b), "beta")
        metadata = {key: {"page_id": "one-page", "display_name": "同一个项目",
                          "status": "暂停", "stale": True} for key in (a_id, b_id)}
        facts = [fact(a, 60, ["same", "a"]), fact(b, 40, ["same", "b"])]
        days = [fact(a, 60, ["same", "a"], day="2026-10-12"),
                fact(b, 40, ["same", "b"], day="2026-10-12")]

        weekly, daily, sources = aggregate(report(facts, days), self.tasks, self.registry, metadata)

        self.assertEqual(len(weekly), 1)
        self.assertEqual((weekly[0]["task_id"], weekly[0]["total_tokens"], weekly[0]["sessions_count"]),
                         ("notion:one-page", 100, 3))
        self.assertEqual(weekly[0]["task_ids"], sorted([a_id, b_id]))
        self.assertEqual(weekly[0]["display_name"], "同一个项目")
        self.assertEqual(weekly[0]["project_status"], "暂停")
        self.assertTrue(weekly[0]["notion_stale"])
        # Daily facts retain local task identity for the sync layer to group.
        self.assertEqual(len(daily), 2)
        self.assertEqual(len(set().union(*(set(row["session_keys"]) for row in daily))), 3)
        self.assertEqual({row["notion_page_id"] for row in sources}, {"one-page"})

    def test_status_binding_and_unknown_never_change_percentage_denominator(self):
        main = self.tasks / "main"
        main_id = self.registry.identify("folder:" + str(main), "main")
        facts = [fact(main, 90, ["main"]), fact(self.tasks / "unbound", 4, ["unbound"]),
                 fact("unknown", 3, ["unknown"]), fact(self.root / "outside", 3, ["outside"])]
        metadata = {main_id: {"page_id": "bound-page", "status": "归档"}}

        weekly, _, _ = aggregate(report(facts), self.tasks, self.registry, metadata)
        major, small = split_small(weekly, 100)

        self.assertEqual(weekly[0]["percent"], 90)
        self.assertEqual(sum(row["percent"] for row in weekly), 100)
        self.assertEqual({row["name"] for row in small}, {"unbound", "未知目录", "临时及其他目录"})
        self.assertEqual([(row["name"], row["total_tokens"], row["percent"]) for row in major],
                         [("main", 90, 90), ("其他", 10, 10)])
        self.assertEqual(major[-1]["members_count"], 3)
        self.assertEqual(sum(row["total_tokens"] for row in major), 100)

    def test_five_percent_threshold_is_exact_and_applied_after_folder_merge(self):
        facts = [fact(self.tasks / "main", 95000, ["m"]),
                 fact(self.tasks / "merged" / "repo-one", 2501, ["a"]),
                 fact(self.tasks / "merged" / "repo-two", 2499, ["b"])]
        weekly, _, _ = aggregate(report(facts), self.tasks, self.registry)
        major, small = split_small(weekly, 100000)
        self.assertEqual(small, [])
        self.assertEqual([(row["name"], row["percent"]) for row in major], [("main", 95), ("merged", 5)])
        under = [{"name": "main", "total_tokens": 95001}, {"name": "minor", "total_tokens": 4999}]
        _, small = split_small(under, 100000)
        self.assertEqual(small[0]["name"], "minor")

    def test_daily_keeps_all_counters_and_explicit_total(self):
        fields = dict(zip(COUNT_FIELDS, (7, 6, 2, 3, 2, 12, 1, 0)))
        a = self.tasks / "alpha"
        raw = fact(a, 12, ["s"], **{key: value for key, value in fields.items() if key != "total_tokens"})
        day = {**raw, "day": "2026-10-12"}
        weekly, daily, _ = aggregate(report([raw], [day]), self.tasks, self.registry)
        for field, value in fields.items():
            self.assertEqual(weekly[0][field], value)
            self.assertEqual(daily[0][field], value)
        self.assertEqual(daily[0]["provider"], "codex")
        self.assertEqual(daily[0]["dataset_id"], self.registry.dataset_id)
        self.assertEqual(daily[0]["statistics_until"], "2026-10-13T08:00:00+00:00")
        self.assertEqual(daily[0]["quality"], ["response_records_verified"])

    def test_legacy_estimate_warning_does_not_claim_files_were_unreadable(self):
        a = self.tasks / "alpha"
        raw = fact(a, 12, ["s"], legacy=True)
        payload = report([raw], [{**raw, "day": "2026-10-12"}],
                         warnings=["Legacy token_count deltas are fallback estimates"],
                         coverage={"files_scanned": 1, "files_skipped": 0, "malformed_lines": 0})
        _, daily, _ = aggregate(payload, self.tasks, self.registry)
        self.assertEqual(daily[0]["quality"], ["legacy_estimated"])

    def test_unreadable_files_are_explicit_in_daily_quality(self):
        a = self.tasks / "alpha"
        raw = fact(a, 12, ["s"])
        payload = report([raw], [{**raw, "day": "2026-10-12"}],
                         coverage={"files_skipped": 1, "malformed_lines": 0})
        _, daily, _ = aggregate(payload, self.tasks, self.registry)
        self.assertIn("partial_unreadable", daily[0]["quality"])

    def test_missing_timestamp_warning_keeps_partial_coverage_explicit(self):
        a = self.tasks / "alpha"
        raw = fact(a, 12, ["s"])
        payload = report([raw], [{**raw, "day": "2026-10-12"}],
                         warnings=["synthetic.jsonl:3: missing/invalid timestamp"],
                         coverage={"missing_timestamps": 1})
        _, daily, _ = aggregate(payload, self.tasks, self.registry)
        self.assertIn("partial_unreadable", daily[0]["quality"])

    def test_missing_subcategory_warning_does_not_claim_unreadable_files(self):
        a = self.tasks / "alpha"
        raw = fact(a, 12, ["s"])
        payload = report([raw], [{**raw, "day": "2026-10-12"}],
                         warnings=["Some counters or legacy deltas were unavailable and are represented as 0"],
                         coverage={"missing_fields": {"cached_input_tokens": 1}})
        _, daily, _ = aggregate(payload, self.tasks, self.registry)
        self.assertEqual(daily[0]["quality"], ["response_records_verified"])

    def test_sqlite_rows_replace_prior_observations_including_downward_corrections(self):
        path = self.data / "daily.sqlite"
        row = {"task_id": "task", "day": "2026-10-12", "total_tokens": 100,
               "session_keys": ["a", "b"], "sessions_count": 2}
        remember_daily(path, [row], "dataset-one")
        remember_daily(path, [row], "dataset-one")
        corrected = {**row, "total_tokens": 70, "session_keys": ["a"], "sessions_count": 1}
        stored = remember_daily(path, [corrected], "dataset-one")
        self.assertEqual(len(stored), 1)
        self.assertEqual((stored[0]["total_tokens"], stored[0]["sessions_count"]), (70, 1))
        self.assertFalse(stored[0]["observed_history"])
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM summaries").fetchone()[0], 1)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_sqlite_omitted_raw_history_remains_explicit_and_is_not_reinvented_as_current(self):
        path = self.data / "daily.sqlite"
        old = {"task_id": "task", "day": "2026-10-12", "total_tokens": 100}
        current = {"task_id": "task", "day": "2026-10-13", "total_tokens": 20}
        remember_daily(path, [old], "dataset-one")
        stored = remember_daily(path, [current], "dataset-one")
        self.assertEqual([(row["day"], row["total_tokens"], row["observed_history"]) for row in stored],
                         [("2026-10-12", 100, True), ("2026-10-13", 20, False)])
        wholly_missing = remember_daily(path, [], "dataset-one")
        self.assertEqual(len(wholly_missing), 2)
        self.assertTrue(all(row["observed_history"] for row in wholly_missing))
        returned = remember_daily(path, [old, current], "dataset-one")
        self.assertTrue(all(not row["observed_history"] for row in returned))

    def test_sqlite_separates_sources_even_with_same_task_and_day(self):
        path = self.data / "daily.sqlite"
        first = {"task_id": "task", "day": "2026-10-12", "total_tokens": 100}
        second = {**first, "total_tokens": 40}
        remember_daily(path, [first], "dataset-one")
        remember_daily(path, [second], "dataset-two")
        stored_one = remember_daily(path, [], "dataset-one")
        stored_two = remember_daily(path, [], "dataset-two")
        self.assertEqual(stored_one[0]["total_tokens"], 100)
        self.assertEqual(stored_two[0]["total_tokens"], 40)
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM summaries").fetchone()[0], 2)

    def test_management_metadata_is_additive_and_never_enters_daily_payload(self):
        path = self.tasks / "alpha"
        task_id = self.registry.identify("folder:" + str(path), "alpha")
        meta = {"page_id": "page-one", "display_name": "管理项目", "status": "开发中", "icon_missing": False,
                "project_goal": "完成产品", "weekly_goal": "验证第一版", "next_step": "验收",
                "project_priority": "高", "project_health": "正常", "target_date": "2026-10-20",
                "last_reviewed": "2026-10-13", "project_summary": "本周已推进", "open_tasks": 4,
                "blocked_tasks": 1, "completed_tasks": 2}
        source = fact(path, 100, ["session"])
        weekly, daily, _ = aggregate(report([source], [{**source, "day": "2026-10-13"}]),
                                     self.tasks, self.registry, {task_id: meta})
        for field in PROJECT_MANAGEMENT_FIELDS:
            self.assertEqual(weekly[0][field], meta.get(field))
            self.assertNotIn(field, daily[0])
        self.assertEqual(weekly[0]["total_tokens"], 100)
        self.assertEqual(weekly[0]["percent"], 100)

    def test_human_actions_and_output_metadata_do_not_change_token_facts(self):
        path = self.tasks / "alpha"
        task_id = self.registry.identify("folder:" + str(path), "alpha")
        meta = {"page_id": "page-one", "current_goal": "确认实际产出", "human_open_tasks": 2,
                "human_blocked_tasks": 1, "human_completed_tasks": 15,
                "latest_output_title": "验收报告", "latest_output_url": "https://example.com/report",
                "latest_progress_summary": "账期已验证", "open_tasks": 10, "completed_tasks": 100}
        source = fact(path, 123, ["session"])
        weekly, daily, _ = aggregate(report([source], [{**source, "day": "2026-10-13"}]),
                                     self.tasks, self.registry, {task_id: meta})
        self.assertEqual(weekly[0]["human_open_tasks"], 2)
        self.assertEqual(weekly[0]["human_blocked_tasks"], 1)
        self.assertEqual(weekly[0]["human_completed_tasks"], 15)
        self.assertIsNone(weekly[0]["development_progress"])
        self.assertEqual(weekly[0]["latest_output_url"], "https://example.com/report")
        self.assertEqual(daily[0]["total_tokens"], 123)
        self.assertNotIn("human_open_tasks", daily[0])

    def test_management_project_without_usage_is_not_added_to_accounting_totals(self):
        usage = [{"task_id": "notion:used", "name": "used", "total_tokens": 100, "percent": 100,
                  "sessions_count": 2, "request_records": 1, "task_ids": ["local-used"]}]
        before = json.dumps(usage, sort_keys=True)
        pages = [{"page_id": "used", "display_name": "有用量", "status": "开发中", "project_priority": "中"},
                 {"page_id": "zero", "display_name": "本周没用", "status": "开发中", "project_priority": "高"},
                 {"page_id": "unbound", "display_name": "云端项目", "status": "计划中"}]
        rows = management_projects(pages, usage, {"local-used": "used", "local-zero": "zero"})
        self.assertEqual([row["task_id"] for row in rows], ["notion:zero", "notion:used", "notion:unbound"])
        zero = rows[0]
        self.assertTrue(zero["usage_linked"])
        self.assertEqual((zero["total_tokens"], zero["sessions_count"]), (0, 0))
        unbound = rows[2]
        self.assertFalse(unbound["usage_linked"])
        self.assertIsNone(unbound["sessions_count"])
        self.assertEqual(json.dumps(usage, sort_keys=True), before)
        self.assertEqual(sum(row["total_tokens"] for row in usage), 100)
        self.assertEqual(usage[0]["percent"], 100)

    def test_management_sort_uses_status_priority_and_stable_identity_not_token_rank(self):
        pages = [{"page_id": "z", "display_name": "发布", "status": "已发布", "project_priority": "高"},
                 {"page_id": "b", "display_name": "同名", "status": "开发中", "project_priority": "低"},
                 {"page_id": "a", "display_name": "同名", "status": "开发中", "project_priority": "低"},
                 {"page_id": "p", "display_name": "计划", "status": "计划中", "project_priority": "高"},
                 {"page_id": "h", "display_name": "重点", "status": "开发中", "project_priority": "高"}]
        rows = management_projects(pages, [], {})
        self.assertEqual([row["task_id"] for row in rows], ["notion:p", "notion:h", "notion:a", "notion:b", "notion:z"])
        self.assertTrue(all(row["total_tokens"] == 0 for row in rows))

    def test_archived_missing_and_paused_management_projects_do_not_appear_with_active_work(self):
        pages = [{"page_id": "active", "display_name": "开发", "status": "开发中"},
                 {"page_id": "paused", "display_name": "暂停", "status": "暂停"},
                 {"page_id": "archive", "display_name": "归档", "status": "归档"},
                 {"page_id": "missing", "display_name": "已删除", "status": "开发中", "available": False}]
        self.assertEqual([row["task_id"] for row in management_projects(pages, [], {})], ["notion:active"])
        self.assertEqual([row["task_id"] for row in management_projects(pages[1:], [], {})], ["notion:paused"])

    def test_multiple_bound_folders_do_not_duplicate_management_page_or_usage(self):
        usage = [{"task_id": "notion:shared", "name": "local-a", "display_name": "管理项目",
                  "total_tokens": 100, "percent": 100, "request_records": 2, "sessions_count": 3,
                  "task_ids": ["local-a", "local-b"]}]
        rows = management_projects([{"page_id": "shared", "display_name": "改名后项目", "status": "开发中"}],
                                   usage, {"local-a": "shared", "local-b": "shared"})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["display_name"], "改名后项目")
        self.assertEqual((rows[0]["total_tokens"], rows[0]["sessions_count"]), (100, 3))
        self.assertTrue(rows[0]["usage_linked"])

    def test_invalid_cached_management_field_types_do_not_break_usage_decoding(self):
        path = self.tasks / "alpha"
        task_id = self.registry.identify("folder:" + str(path), "alpha")
        metadata = {task_id: {"page_id": "page", "project_goal": {"unexpected": "shape"},
                             "open_tasks": True, "blocked_tasks": -1, "completed_tasks": "2"}}
        weekly, _, _ = aggregate(report([fact(path, 100, ["s"])]), self.tasks, self.registry, metadata)
        self.assertEqual(weekly[0]["total_tokens"], 100)
        self.assertEqual(weekly[0]["percent"], 100)
        self.assertTrue(all(weekly[0][field] is None for field in ("project_goal", "open_tasks", "blocked_tasks", "completed_tasks")))

    def test_today_uses_shanghai_cutoff_and_internal_daily_date_without_changing_week(self):
        path = self.tasks / "alpha"
        payload = report([fact(path, 100, ["s"])], [fact(path, 40, ["s"], day="2026-10-13"), fact(path, 60, ["s"], day="2026-10-14")])
        payload["range"]["until_exclusive"] = "2026-10-13T16:05:00Z"
        weekly, daily, _ = aggregate(payload, self.tasks, self.registry)
        self.assertEqual((weekly[0]["total_tokens"], weekly[0]["today_tokens"], weekly[0]["percent"]), (100, 60, 100))
        self.assertEqual(sum(row["total_tokens"] for row in daily), 100)
        self.assertTrue(all("today_tokens" not in row for row in daily))

    def test_today_merges_multiple_folders_of_same_notion_project_once(self):
        a, b = self.tasks / "a", self.tasks / "b"
        metadata = {self.registry.identify("folder:" + str(path), path.name): {"page_id": "shared", "display_name": "项目"} for path in (a, b)}
        payload = report([fact(a, 60, ["a"]), fact(b, 40, ["b"])], [fact(a, 20, ["a"], day="2026-10-13"), fact(b, 40, ["b"], day="2026-10-13")])
        weekly, _, _ = aggregate(payload, self.tasks, self.registry, metadata)
        self.assertEqual(len(weekly), 1)
        self.assertEqual((weekly[0]["total_tokens"], weekly[0]["today_tokens"]), (100, 60))

    def test_absent_daily_coverage_keeps_today_unknown_and_managed_unbound_unknown(self):
        payload = report([fact(self.tasks / "a", 100, ["s"])])
        payload.pop("project_daily")
        weekly, _, _ = aggregate(payload, self.tasks, self.registry)
        self.assertIsNone(weekly[0]["today_tokens"])
        rows = management_projects([{"page_id": "bound", "display_name": "绑定"}, {"page_id": "unbound", "display_name": "未绑定"}], [], {"local": "bound"})
        mapped = {row["task_id"]: row for row in rows}
        self.assertEqual(mapped["notion:bound"]["today_tokens"], 0)
        self.assertIsNone(mapped["notion:unbound"]["today_tokens"])
        unavailable = management_projects([{"page_id": "bound", "display_name": "绑定"}], [], {"local": "bound"}, today_known=False)
        self.assertIsNone(unavailable[0]["today_tokens"])

    def test_explicit_assessment_wins_without_using_action_completion_rate(self):
        self.assertEqual(project_progress({"development_progress": 0, "progress_source": "manual", "open_tasks": 1, "completed_tasks": 6}), {"development_progress": 0.0, "progress_source": "manual"})
        self.assertEqual(project_progress({"development_progress": 65, "progress_source": "ai", "open_tasks": 2, "completed_tasks": 15}), {"development_progress": 65.0, "progress_source": "ai"})
        self.assertEqual(project_progress({"development_progress": 88, "progress_source": "tasks", "open_tasks": 1, "completed_tasks": 6}), {"development_progress": None, "progress_source": None})
        self.assertEqual(project_progress({"open_tasks": 1, "blocked_tasks": 1, "completed_tasks": 6}), {"development_progress": None, "progress_source": None})
        for value in [None, True, -1, 101, float('nan'), float('inf'), 10**400]:
            with self.subTest(value=str(value)):
                self.assertEqual(project_progress({"development_progress": value, "progress_source": "manual"}), {"development_progress": None, "progress_source": None})
        for counts in [{}, {"open_tasks": 0, "completed_tasks": 0}, {"open_tasks": None, "completed_tasks": 6}]:
            self.assertIsNone(project_progress(counts)["development_progress"])

    def test_empty_report_keeps_zero_and_does_not_invent_notion_projects(self):
        weekly, daily, sources = aggregate(report([]), self.tasks, self.registry)
        self.assertEqual((weekly, daily, sources), ([], [], []))
        self.assertEqual(split_small([], 0), ([], []))


if __name__ == "__main__":
    unittest.main()
