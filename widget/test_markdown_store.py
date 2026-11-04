import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from markdown_store import (MarkdownError, MarkdownWorkspace, management_snapshot,
                            note_digest, read_note, write_note)


class MarkdownProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root / "项目管理"
        self.data = self.root / "data"
        self.documents = self.root / "Documents"
        self.folder = self.documents / "时间管理app"
        self.vault.mkdir()
        self.folder.mkdir(parents=True)
        self.project_id = "p-shijia-v2"
        self.project = self.vault / "项目" / "时价" / "项目.md"
        self.props = {"type": "project", "id": self.project_id, "name": "时价", "status": "开发中",
                      "phase": "整体验收", "priority": "高", "health": "有风险", "progress": 58,
                      "progress_source": "ai", "progress_reviewed": "2026-10-16T08:26:48+08:00",
                      "icon": "⏱️", "next_step": "核对真实设备证据", "revision": 1}
        write_note(self.project, self.props,
                   "# 时价\n\n## 总目标\n\n可靠记录时间。\n\n## 需要你决策\n\n查看人工事项。"
                   "\n\n## 需要你验收\n\n暂无。\n\n## 目前要做的事\n\n核对V2范围。\n\n### 总体路线\n\n后续计划。\n\n## 人的笔记\n\n不要覆盖。")
        self.workspace = MarkdownWorkspace(self.data, self.vault)
        self.workspace.setup(self.vault, self.documents)

    def task(self, identity, owner="人类确认", status="待确认", kind="决策"):
        path = self.vault / "任务" / (identity + ".md")
        write_note(path, {"type": "task", "id": identity, "project_id": self.project_id,
                          "name": identity, "managed_by": owner, "status": status, "kind": kind, "revision": 1}, "# 事项\n")
        return path

    def row(self, amount=100, until="2026-10-24T10:00:00+00:00", task="folder-a", sessions=None):
        return {"task_id": task, "dataset_id": "dataset-a", "provider": "codex", "day": "2026-10-24",
                "total_tokens": amount, "input_tokens": amount - 10, "output_tokens": 10,
                "sessions_count": 1, "session_keys": sessions if sessions is not None else ["hashed-session"],
                "quality": ["response_records_verified"], "statistics_until": until}

    def bind_tasks(self, *tasks):
        for task in tasks:
            self.workspace.bind({"task_id": task, "project_id": self.project_id})

    def sync(self, rows, until="2026-10-24T10:00:00+00:00", complete=True):
        return self.workspace.sync(rows, now="2026-10-24T12:00:00+00:00",
                                   complete_dataset_ids=["dataset-a"] if complete else [], complete_until=until)

    def daily(self):
        return management_snapshot(self.vault)["usage"]["daily"]

    def test_read_model_is_side_effect_free_and_no_network(self):
        before = {str(p): p.stat().st_mtime_ns for p in self.root.rglob("*") if p.is_file()}
        with patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            model = management_snapshot(self.vault, self.data)
        after = {str(p): p.stat().st_mtime_ns for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(model["schema_version"], 1)
        self.assertEqual(model["projects"][0]["project_id"], self.project_id)

    def test_project_card_sections_and_old_assessment_preserved(self):
        record = self.workspace.projects()[0]
        self.assertEqual(record["project_goal"], "可靠记录时间。")
        self.assertEqual(record["current_goal"], "核对V2范围。")
        self.assertEqual(record["development_progress"], 58)
        self.assertIn("2026-10-16", record["progress_reviewed"])
        self.assertTrue(record["url"].startswith("obsidian://open?"))

    def test_formal_artifact_fallback_reads_existing_note_without_writing(self):
        folder = self.project.parent / "成果"
        for identity, title, times in (("a-old", "旧版说明", {"created_at": "2026-10-21T10:00:00+08:00"}),
                                       ("a-latest", "可运行版本", {"created_at": "2026-10-20T10:00:00+08:00", "updated": "2026-10-24T10:00:00+08:00"}),
                                       ("a-undated", "时间未知的材料", {})):
            write_note(folder / (identity + ".md"), {"type": "artifact", "id": identity,
                       "project_id": self.project_id, "name": title, "revision": 1, **times}, "# 正式成果引用\n")
        before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        snapshot = management_snapshot(self.vault, self.data)
        project = snapshot["projects"][0]
        artifact = next(a for a in snapshot["artifacts"] if a["id"] == "a-latest")
        self.assertEqual(project["latest_output_title"], "可运行版本")
        self.assertEqual(project["latest_output_url"], artifact["url"])
        self.assertTrue(project["latest_output_url"].startswith("obsidian://open?"))
        self.assertEqual(before, {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()})
        self.assertNotIn("latest_output_url", read_note(self.project)[0])

    def test_explicit_artifact_fields_take_priority_over_readonly_fallback(self):
        artifact_path = self.project.parent / "成果/a-auto.md"
        write_note(artifact_path, {"type": "artifact", "id": "a-auto", "project_id": self.project_id,
                   "name": "自动候选", "updated": "2026-10-25T10:00:00+08:00", "revision": 1}, "# 引用\n")
        props, body = read_note(self.project)
        write_note(self.project, {**props, "latest_output_title": "指定正式版本", "latest_output_url": "https://example.com/release"}, body)
        project = self.workspace.projects()[0]
        self.assertEqual(project["latest_output_title"], "指定正式版本")
        self.assertEqual(project["latest_output_url"], "https://example.com/release")

    def test_artifact_fallback_is_project_scoped_and_ignores_events_and_tasks(self):
        other_project = self.vault / "项目/其他工程/项目.md"
        write_note(other_project, {"type": "project", "id": "p-other", "name": "其他工程", "status": "开发中", "revision": 1}, "# 其他工程\n")
        write_note(other_project.parent / "成果/a-other.md", {"type": "artifact", "id": "a-other", "project_id": "p-other",
                   "name": "别人的最新成果", "updated": "2026-10-25T12:00:00+08:00", "revision": 1}, "# 成果引用\n")
        self.task("t-looks-like-output")
        write_note(self.project.parent / "历史/e-completed.md", {"type": "event", "id": "e-completed", "project_id": self.project_id,
                   "name": "任务已完成", "updated": "2026-10-25T12:00:00+08:00", "revision": 1}, "# 完成事件\n")
        projects = {p["project_id"]: p for p in self.workspace.projects()}
        self.assertIsNone(projects[self.project_id]["latest_output_title"])
        self.assertIsNone(projects[self.project_id]["latest_output_url"])
        self.assertEqual(projects["p-other"]["latest_output_title"], "别人的最新成果")

    def test_undated_artifact_fallback_is_stable_without_fabricating_dates(self):
        folder = self.project.parent / "成果"
        for identity in ("a-first", "a-second"):
            write_note(folder / (identity + ".md"), {"type": "artifact", "id": identity,
                       "project_id": self.project_id, "name": identity, "revision": 1}, "# 时间未核实\n")
        first = management_snapshot(self.vault)
        second = management_snapshot(self.vault)
        self.assertEqual(first["projects"][0]["latest_output_title"], second["projects"][0]["latest_output_title"])
        self.assertTrue(all(a.get("created_at") is None and a.get("updated") is None for a in first["artifacts"]))

    def test_human_counts_separate_blocked_subset_cancelled_ignored(self):
        self.task("t-open")
        self.task("t-blocked", status="阻塞")
        self.task("t-done", status="已完成")
        self.task("t-cancel", status="取消")
        self.task("t-ai", owner="AI", kind="执行")
        record = self.workspace.projects()[0]
        self.assertEqual((record["human_open_tasks"], record["human_blocked_tasks"], record["human_completed_tasks"]), (2, 1, 1))
        self.assertEqual(record["open_tasks"], 3)

    def test_invalid_task_owner_does_not_become_false_zero(self):
        self.task("t-bad", owner="unknown")
        with self.assertRaises(MarkdownError):
            self.workspace.projects()
        self.assertEqual(self.workspace.status()["state"], "error")

    def test_duplicate_identity_detected(self):
        write_note(self.vault / "项目" / "其他" / "项目.md", self.props, "# duplicate\n")
        with self.assertRaisesRegex(MarkdownError, "重复"):
            management_snapshot(self.vault)

    def test_dangling_project_is_error(self):
        p = self.task("t-orphan")
        props, body = read_note(p)
        props["project_id"] = "missing"
        write_note(p, props, body)
        with self.assertRaises(MarkdownError):
            management_snapshot(self.vault)

    def test_properties_block_lists_and_unicode(self):
        p = self.root / "note.md"
        p.write_text('---\nname: 时价\ntags:\n  - 时间\n  - "[[一个链接]]"\nprogress: null\nflag: true\n---\n\n正文\n')
        props, body = read_note(p)
        self.assertEqual(props["tags"], ["时间", "[[一个链接]]"])
        self.assertIsNone(props["progress"])
        self.assertIs(props["flag"], True)
        self.assertEqual(body, "正文\n")

    def test_duplicate_yaml_and_nested_properties_rejected(self):
        for text in ('---\nid: a\nid: b\n---\n', '---\ndata:\n  nested: value\n---\n'):
            p = self.root / "bad.md"
            p.write_text(text)
            with self.assertRaises(MarkdownError):
                read_note(p)

    def test_human_edit_same_revision_detected_by_digest(self):
        digest = note_digest(self.project)
        props, body = read_note(self.project)
        self.project.write_text(self.project.read_text().replace("不要覆盖", "人工新增意见"))
        with self.assertRaisesRegex(MarkdownError, "已被修改"):
            write_note(self.project, props, body, expected_revision=1, expected_digest=digest)
        self.assertIn("人工新增意见", self.project.read_text())

    def test_bind_folder_uses_stable_registry(self):
        result = self.workspace.bind({"folder": str(self.folder), "project_id": self.project_id})
        again = self.workspace.bind({"folder": str(self.folder), "project_id": self.project_id})
        self.assertEqual(result["task_id"], again["task_id"])
        self.assertEqual(self.workspace.project_for_task(result["task_id"])["project_id"], self.project_id)
        self.assertTrue((self.data / "project-registry.json").is_file())

    def test_nested_repository_cannot_bind_as_top_project(self):
        nested = self.folder / "inner"
        nested.mkdir()
        with self.assertRaises(MarkdownError):
            self.workspace.bind({"folder": str(nested), "project_id": self.project_id})

    def test_disable_readback_is_explicit(self):
        self.bind_tasks("folder-a")
        p = self.data / "app-settings.json"
        settings = json.loads(p.read_text())
        settings["management_enabled"] = False
        p.write_text(json.dumps(settings))
        self.assertIsNone(self.workspace.project_for_task("folder-a"))
        self.assertFalse(self.workspace.status()["connected"])

    def test_daily_idempotent_absolute_replacement_and_downward_correction(self):
        self.bind_tasks("folder-a")
        self.assertEqual(self.sync([self.row()])["updated"], 1)
        self.assertEqual(self.sync([self.row()])["updated"], 0)
        self.sync([self.row(80, "2026-10-24T11:00:00+00:00")], "2026-10-24T11:00:00+00:00")
        rows = self.daily()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["total_tokens"], 80)
        self.assertEqual(rows[0]["counted_tokens"], 80)

    def test_old_daily_cannot_overwrite_new(self):
        self.bind_tasks("folder-a")
        self.sync([self.row(200, "2026-10-24T11:00:00+00:00")], "2026-10-24T11:00:00+00:00")
        self.sync([self.row(100)])
        self.assertEqual(self.daily()[0]["total_tokens"], 200)

    def test_multiple_folders_session_union(self):
        self.bind_tasks("folder-a", "folder-b")
        self.sync([self.row(task="folder-a"), self.row(50, task="folder-b")])
        row = self.daily()[0]
        self.assertEqual(row["total_tokens"], 150)
        self.assertEqual(row["sessions_count"], 1)

    def test_missing_session_union_is_unknown(self):
        self.bind_tasks("folder-a", "folder-b")
        rows = [self.row(), self.row(50, task="folder-b")]
        rows[1].pop("session_keys")
        self.sync(rows)
        self.assertIsNone(self.daily()[0]["sessions_count"])
        self.assertIn("sessions_union_unverified", self.daily()[0]["quality"])

    def test_rebind_retires_old_allocation_without_losing_original(self):
        self.bind_tasks("folder-a")
        self.sync([self.row()])
        second = self.workspace.create({"name": "第二项目", "project_id": "p-second"})
        self.workspace.bind({"task_id": "folder-a", "project_id": second["project_id"]})
        self.sync([self.row(100, "2026-10-24T11:00:00+00:00")], "2026-10-24T11:00:00+00:00")
        rows = self.daily()
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(r["counted_tokens"] for r in rows), 100)
        retired = next(r for r in rows if not r["included"])
        self.assertEqual(retired["total_tokens"], 100)
        self.assertEqual(retired["counted_tokens"], 0)

    def test_partial_batch_does_not_retire_missing_history(self):
        self.bind_tasks("folder-a")
        self.sync([self.row()])
        self.sync([], complete=False)
        self.assertTrue(self.daily()[0]["included"])

    def test_rename_project_keeps_identity_and_bindings(self):
        self.bind_tasks("folder-a")
        props, body = read_note(self.project)
        props["name"] = "新显示名称"
        write_note(self.project, props, body)
        dest = self.vault / "项目" / "移动后" / "项目.md"
        dest.parent.mkdir()
        self.project.rename(dest)
        self.assertEqual(self.workspace.project_for_task("folder-a")["display_name"], "新显示名称")
        self.assertEqual(self.workspace.project_for_task("folder-a")["project_id"], self.project_id)

    def manifest(self):
        (self.folder / "成果.md").write_text("已有真实文档")
        return {"version": 1, "project": {"name": "时价", "vision": "导入的真实目标", "current_goal": "核对真机证据", "next_step": "准备真机测试", "summary": "已有核心流程，待验证。", "icon": {"type": "emoji", "emoji": "⏱️"}, "status": "开发中", "health": "有风险",
            "progress": {"percent": 50, "evidence": "两个里程碑的证据", "reviewed_at": "2026-10-24T10:00:00Z", "milestones": [{"name": "核心", "weight": 1, "completion": 100, "evidence": "文档"}, {"name": "真机", "weight": 1, "completion": 0, "evidence": "待验收"}]},
            "artifacts": [{"title": "已实现范围", "relative_path": "成果.md", "result": "说明现有实现与限制"}],
            "manual_actions": [{"id": "review", "name": "审阅范围", "status": "待确认", "decision": "是否沿用范围", "recommendation": "先验证现有范围", "impact": "决定下一步", "evidence_url": None}],
            "history": [{"id": "existing", "title": "既有成果", "occurred_at": None, "result": "已有资料，原日期未核实", "evidence_url": None}]}}

    def test_full_manifest_roundtrip_and_retries_preserve_history(self):
        manifest = self.manifest()
        command = {"manifest": manifest, "folder": str(self.folder), "task_root": str(self.documents), "project_id": self.project_id}
        self.workspace.import_manifest(command)
        first = management_snapshot(self.vault)
        self.assertEqual(first["projects"][0]["next_step"], "准备真机测试")
        self.assertEqual(first["projects"][0]["development_progress"], 50)
        self.assertEqual(len(first["tasks"]), 1)
        self.assertEqual(len(first["artifacts"]), 1)
        self.assertEqual(len(first["events"]), 1)
        self.assertTrue(all(row["path"].startswith("项目/时价/") for family in ("tasks", "events", "artifacts") for row in first[family]))
        self.assertFalse(any((self.vault / name).exists() for name in ("任务", "事件", "成果")))
        self.assertIn("未复制成果内容", first["artifacts"][0]["body"])
        self.assertEqual(first["artifacts"][0]["source_root"], str(self.folder.resolve()))
        self.assertEqual(first["artifacts"][0]["relative_path"], "成果.md")
        self.assertTrue(first["artifacts"][0]["target_url"].startswith("file:"))
        self.assertIn("原完成时间未核实", first["events"][0]["body"])
        self.assertIn("不要覆盖", self.project.read_text())
        manifest["project"]["history"][0]["result"] = "不能覆盖旧历史"
        self.workspace.import_manifest(command)
        second = management_snapshot(self.vault)
        self.assertEqual(len(second["events"]), 1)
        self.assertNotIn("不能覆盖旧历史", second["events"][0]["body"])
        self.assertEqual(len(second["tasks"]), 1)

    def test_cli_status_and_projects_json(self):
        script = Path(__file__).with_name("markdown_store.py")
        process = subprocess.run([sys.executable, "-B", str(script), "--data-dir", str(self.data)], input='{"command":"status"}', text=True, capture_output=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertTrue(result["status"]["connected"])
        self.assertIn("tasks_url", result["status"])

    def period_report(self, amount=100, generated="2026-10-24T10:00:00+00:00", schedule=None, windows=None):
        period = {"start": "2026-10-19T00:00:00+08:00", "end": "2026-10-26T00:00:00+08:00",
                  "observed_until": generated, "state": "open", "totals": {"total_tokens": amount, "input_tokens": amount - 10, "output_tokens": 10},
                  "sessions_count": 1, "projects": [{"name": "时价", "total_tokens": amount, "sessions_count": 1}],
                  "models": [{"model": "fixture", "total_tokens": amount, "sessions_count": 1}],
                  "coverage": {"status": "observed", "accepted_records": 1, "quality": ["response_records_verified"]}}
        monthly = {"status": "needs_anchor", "periods": []}
        if schedule:
            monthly = {"status": "configured", "schedule_id": schedule, "anchor_precision": "datetime", "periods": [{**period, "start": "2026-10-01T00:00:00+08:00", "end": "2026-11-01T00:00:00+08:00"}]}
        return {"dataset_id": "dataset-a", "generated_at": generated, "weekly": {"periods": [period]}, "monthly": monthly,
                "quota": {"statistics_until": generated, "windows": windows or []}}

    def test_periods_default_month_unknown_no_fake_month_or_windows(self):
        report = self.period_report()
        self.workspace.sync_periods(report)
        model = management_snapshot(self.vault)
        self.assertEqual(len(model["usage"]["periods"]), 1)
        self.assertEqual(model["usage"]["periods"][0]["kind"], "natural_week")
        self.assertEqual(model["usage"]["periods"][0]["start_iso"], "2026-10-19T00:00:00+08:00")
        self.assertEqual(model["usage"]["windows"], [])
        self.assertIn("| 时价 | 100 | 1 |", model["usage"]["periods"][0]["body"])

    def test_periods_idempotent_and_old_scan_protected_even_when_no_write(self):
        self.workspace.sync_periods(self.period_report())
        result = self.workspace.sync_periods(self.period_report(generated="2026-10-24T11:00:00+00:00"))
        self.assertEqual(result["updated"], 0)
        self.workspace.sync_periods(self.period_report(50, generated="2026-10-24T10:30:00+00:00"))
        self.assertEqual(management_snapshot(self.vault)["usage"]["periods"][0]["total_tokens"], 100)

    def test_periods_downward_correction_allowed(self):
        self.workspace.sync_periods(self.period_report())
        self.workspace.sync_periods(self.period_report(80, generated="2026-10-24T11:00:00+00:00"))
        self.assertEqual(management_snapshot(self.vault)["usage"]["periods"][0]["total_tokens"], 80)

    def test_month_schedule_change_retires_previous_and_old_cannot_revive(self):
        self.workspace.sync_periods(self.period_report(schedule="schedule-1"))
        self.workspace.sync_periods(self.period_report(generated="2026-10-24T11:00:00+00:00", schedule="schedule-2"))
        rows = management_snapshot(self.vault)["usage"]["periods"]
        months = [r for r in rows if r["kind"] == "billing_month"]
        self.assertEqual(len(months), 2)
        self.assertEqual(sum(r["counted_tokens"] for r in months), 100)
        self.assertFalse(next(r for r in months if r["schedule_id"] == "schedule-1")["included"])
        self.workspace.sync_periods(self.period_report(schedule="schedule-1"))
        months = [r for r in management_snapshot(self.vault)["usage"]["periods"] if r["kind"] == "billing_month"]
        self.assertEqual(sum(r["counted_tokens"] for r in months), 100)

    def test_limited_period_list_preserves_older_records(self):
        self.workspace.sync_periods(self.period_report())
        report = self.period_report()
        report["weekly"]["periods"] = []
        self.workspace.sync_periods(report)
        self.assertEqual(len(management_snapshot(self.vault)["usage"]["periods"]), 1)

    def test_quota_windows_opt_in_and_old_open_window_closes(self):
        old = {"kind": "周额度", "start": "2026-10-19T00:00:00+08:00", "end": "2026-10-26T00:00:00+08:00",
               "observed_until": "2026-10-24T10:00:00+00:00", "last_observed_at": "2026-10-24T10:00:00+00:00",
               "duration_mins": 10080, "observations": 3, "used_percent": 80, "remaining_percent": 20, "state": "open", "status": "observed", "notes": ["首次观测之前未知"]}
        self.workspace.sync_periods(self.period_report(windows=[old]))
        self.assertEqual(management_snapshot(self.vault)["usage"]["windows"], [])
        self.workspace.sync_periods(self.period_report(windows=[old]), sync_windows=True)
        new = {**old, "start": "2026-10-24T18:30:00+08:00", "end": "2026-10-31T18:30:00+08:00", "used_percent": 10, "remaining_percent": 90}
        self.workspace.sync_periods(self.period_report(generated="2026-10-24T11:00:00+00:00", windows=[new]), sync_windows=True)
        rows = management_snapshot(self.vault)["usage"]["windows"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(next(r for r in rows if r["start"] == old["start"])["state"], "closed")
        self.workspace.sync_periods(self.period_report(windows=[old]), sync_windows=True)
        rows = management_snapshot(self.vault)["usage"]["windows"]
        self.assertEqual(next(r for r in rows if r["start"] == old["start"])["state"], "closed")

    def test_daily_watermark_remembers_unchanged_newer_observation(self):
        self.bind_tasks("folder-a")
        self.sync([self.row()])
        self.sync([self.row(until="2026-10-24T11:00:00+00:00")], "2026-10-24T11:00:00+00:00")
        self.sync([self.row(50, until="2026-10-24T10:30:00+00:00")], complete=False)
        self.assertEqual(self.daily()[0]["total_tokens"], 100)

    def test_readable_period_name_uses_shanghai_day_without_changing_identity(self):
        report = self.period_report()
        report["weekly"]["periods"][0].update(start="2026-10-18T16:00:00+00:00", end="2026-10-25T16:00:00+00:00")
        self.workspace.sync_periods(report)
        first = management_snapshot(self.vault)["usage"]["periods"][0]
        self.assertEqual(Path(first["path"]).name, "自然周投入-2026-10-19.md")
        self.assertIn("# 自然周投入 · 2026-10-19", first["body"])
        self.assertIn("2026-10-19 00:00", first["body"])
        self.workspace.sync_periods(report)
        second = management_snapshot(self.vault)["usage"]["periods"][0]
        self.assertEqual((first["id"], first["period_key"], first["counted_tokens"]), (second["id"], second["period_key"], second["counted_tokens"]))
        self.assertEqual(len(list((self.vault / "账本/周期").glob("*.md"))), 1)

    def test_hash_ledger_migration_preserves_history_and_is_idempotent(self):
        self.workspace.sync_periods(self.period_report())
        record = management_snapshot(self.vault)["usage"]["periods"][0]
        readable = self.vault / record["path"]
        legacy = readable.with_name(record["id"] + ".md")
        readable.rename(legacy)
        props, body = read_note(legacy)
        props.pop("name", None)
        body = body.replace("# 自然周投入 · 2026-10-19", "# 自然周投入 · 2026-10-18")
        write_note(legacy, props, body)
        before = management_snapshot(self.vault)["usage"]["periods"][0]
        result = self.workspace.rename_ledgers()
        after = management_snapshot(self.vault)["usage"]["periods"][0]
        self.assertEqual(result["renamed"], 1)
        for key in ("id", "period_key", "total_tokens", "counted_tokens", "included", "start", "end"):
            self.assertEqual(before[key], after[key])
        self.assertFalse(legacy.exists())
        self.assertEqual(self.workspace.rename_ledgers(), {"renamed": 0, "rewritten": 0})

    def test_same_week_different_sources_keep_both_without_name_overwrite(self):
        self.workspace.sync_periods(self.period_report())
        other = self.period_report(25)
        other["dataset_id"] = "dataset-b"
        self.workspace.sync_periods(other)
        rows = management_snapshot(self.vault)["usage"]["periods"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(r["counted_tokens"] for r in rows), 125)
        self.assertEqual(len({r["path"] for r in rows}), 2)

    def test_unassigned_publisher_compatibility_does_not_claim_vault(self):
        self.bind_tasks("folder-a")
        self.sync([self.row()])
        self.workspace.sync_periods(self.period_report())
        self.workspace.rename_ledgers()
        self.assertTrue((self.vault / ".ledger-publication.lock").is_file())
        self.assertFalse((self.vault / ".ledger-publisher.json").exists())
        self.assertEqual(self.daily()[0]["counted_tokens"], 100)

    def test_daily_batch_reuses_project_names_without_per_row_vault_scan(self):
        self.bind_tasks("folder-a")
        rows = [{**self.row(), "day": day} for day in ("2026-10-22", "2026-10-23", "2026-10-24")]
        with patch.object(self.workspace, "projects", side_effect=AssertionError("per-row vault scan")):
            self.sync(rows)
        self.assertEqual(len(self.daily()), 3)
        self.assertTrue(all("时价" in row["name"] for row in self.daily()))

    def test_assigned_publisher_rejects_other_runtime_before_any_ledger_change(self):
        self.bind_tasks("folder-a")
        other = MarkdownWorkspace(self.root / "other-runtime", self.vault)
        other.setup(self.vault, self.documents)
        other.bind({"task_id": "folder-a", "project_id": self.project_id})
        marker = self.vault / ".ledger-publisher.json"
        marker.write_text(json.dumps({"owner_data_dir": str(self.data.resolve())}))
        self.sync([self.row()])
        before = {str(p): p.read_bytes() for p in (self.vault / "账本").rglob("*.md")}
        actions = (lambda: other.sync([self.row(150)]),
                   lambda: other.sync_periods(self.period_report(150)),
                   other.rename_ledgers)
        for action in actions:
            with self.subTest(action=action), self.assertRaises(MarkdownError) as caught:
                action()
            self.assertEqual(caught.exception.code, "publisher_not_owner")
        self.assertEqual(before, {str(p): p.read_bytes() for p in (self.vault / "账本").rglob("*.md")})
        self.assertFalse(other.state_path.exists())
        self.workspace.sync_periods(self.period_report())

    def test_invalid_publisher_assignment_fails_closed(self):
        marker = self.vault / ".ledger-publisher.json"
        examples = ("not json", "[]", "{}", '{"owner_data_dir":true}',
                    '{"owner_data_dir":"relative/data"}',
                    json.dumps({"owner_data_dir": str(self.root / "missing")}),
                    json.dumps({"owner_data_dir": str(self.data / ".." / "data")}))
        for raw in examples:
            marker.write_text(raw)
            for action in (lambda: self.sync([]),
                           lambda: self.workspace.sync_periods(self.period_report()),
                           self.workspace.rename_ledgers):
                with self.subTest(raw=raw), self.assertRaises(MarkdownError) as caught:
                    action()
                self.assertEqual(caught.exception.code, "invalid_publisher")
        marker.unlink()
        alias = self.root / "owner.json"
        alias.write_text(json.dumps({"owner_data_dir": str(self.data.resolve())}))
        marker.symlink_to(alias)
        with self.assertRaises(MarkdownError) as caught:
            self.sync([])
        self.assertEqual(caught.exception.code, "invalid_publisher")
        self.assertFalse(self.workspace.state_path.exists())

    def test_management_reads_and_bindings_never_claim_or_lock_ledger(self):
        lock = self.vault / ".ledger-publication.lock"
        marker = self.vault / ".ledger-publisher.json"
        marker.write_text("invalid but unrelated to reads")
        before = marker.read_bytes()
        self.workspace.projects()
        self.workspace.status()
        self.workspace.bind({"task_id": "folder-a", "project_id": self.project_id})
        management_snapshot(self.vault, self.data)
        self.assertFalse(lock.exists())
        self.assertEqual(marker.read_bytes(), before)

    def test_all_publication_paths_hold_same_vault_lock_across_runtimes(self):
        other = MarkdownWorkspace(self.root / "other-runtime", self.vault)
        other.setup(self.vault, self.documents)
        check = ("import fcntl,sys;f=open(sys.argv[1],'a+');\n"
                 "try:\n fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)\n"
                 "except BlockingIOError:\n sys.exit(0)\n"
                 "sys.exit(3)\n")
        lock = self.vault / ".ledger-publication.lock"
        for workspace in (self.workspace, other):
            original = workspace._begin_publication
            def checking_begin(state, family):
                result = subprocess.run([sys.executable, "-c", check, str(lock)], capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                original(state, family)
            with patch.object(workspace, "_begin_publication", side_effect=checking_begin) as begin:
                workspace.sync([])
                workspace.sync_periods(self.period_report())
                workspace.rename_ledgers()
            self.assertEqual(begin.call_count, 3)
        # The shared lock is released after every publication.
        result = subprocess.run([sys.executable, "-c", check, str(lock)], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 3, result.stderr.decode())


if __name__ == "__main__":
    unittest.main()
