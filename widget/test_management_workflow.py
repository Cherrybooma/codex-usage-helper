"""Isolated workflow and read-only HTTP contract checks for the Markdown vault."""
import hashlib
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
from markdown_store import DEFAULT_RULES, DEFAULT_VAULT, MarkdownError, management_snapshot, read_note, write_note
import management_api as api
import project_manager as pm
import service_control


class VaultFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root / "管理库"
        self.data = self.root / "runtime"
        self.documents = self.root / "Documents"
        self.vault.mkdir()
        self.documents.mkdir()
        self.project_id = "p-test"
        self.project = self.vault / "项目" / "时间项目" / "项目.md"
        self.project_props = {"type": "project", "id": self.project_id, "name": "时间项目",
                              "status": "开发中", "phase": "完成核心使用流程", "priority": "高",
                              "health": "正常", "revision": 1, "icon": "⏱️", "next_step": "提交成果"}
        self.project_body = "# 时间项目\n\n## 总目标\n\n可靠记录时间。\n\n## 目前要做的事\n\n验证一天的记录。\n\n## 人的笔记\n\n保留这段人工原文。\n"
        write_note(self.project, self.project_props, self.project_body)

    def task(self, identity="t-review", *, project=None, kind="验收", owner="人类确认", status="待确认", revision=1):
        path = self.project.parent / "任务" / (identity + ".md")
        props = {"type": "task", "id": identity, "project_id": project or self.project_id,
                 "name": "检查记录功能", "kind": kind, "managed_by": owner, "status": status,
                 "revision": revision, "priority": "高", "phase": "完成核心使用流程"}
        body = "# 检查记录功能\n\n## 事项\n\n检查录入、保存和重新打开。\n\n## 验收标准\n\n记录不丢失。\n\n## 人工补充\n\n这段人写的说明必须保留。\n"
        write_note(path, props, body)
        return path

    def feedback(self, task="t-review", revision=1, result="通过", text="实际检查通过，记录保留。"):
        return pm.submit_feedback(self.vault, task, revision, result, text, "测试中的明确用户意见")

    def files(self):
        return {str(p.relative_to(self.root)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
                for p in self.root.rglob("*") if p.is_file()}


class HumanFeedbackChecks(VaultFixture):
    def test_unprepared_review_cannot_be_accepted(self):
        self.task(status="待处理")
        with self.assertRaises(MarkdownError) as caught:
            self.feedback()
        self.assertEqual(caught.exception.code, "not_submitted")

    def test_feedback_file_rename_keeps_links_valid(self):
        path = self.task()
        feedback = self.feedback()
        original = self.project.parent / "反馈" / (feedback["id"] + ".md")
        renamed = original.with_name("我的实际审阅.md")
        original.rename(renamed)
        pm.apply_feedback(self.vault, feedback["id"])
        self.assertIn("[[项目/时间项目/反馈/我的实际审阅]]", read_note(path)[1])

    def test_feedback_submission_records_target_version_without_closing_task(self):
        path = self.task()
        feedback = self.feedback()
        self.assertEqual(read_note(path)[0]["status"], "待确认")
        props, body = read_note(self.project.parent / "反馈" / (feedback["id"] + ".md"))
        self.assertEqual(props["target_revision"], 1)
        self.assertEqual(props["task_id"], "t-review")
        self.assertEqual(props["status"], "已提交")
        self.assertIn("实际检查通过", body)

    def test_apply_preserves_user_written_body_and_adds_trace(self):
        path = self.task()
        old_body = read_note(path)[1]
        feedback = self.feedback()
        pm.apply_feedback(self.vault, feedback["id"])
        props, body = read_note(path)
        self.assertEqual(props["status"], "已完成")
        self.assertEqual(props["revision"], 2)
        self.assertTrue(body.startswith(old_body.rstrip()))
        self.assertIn("这段人写的说明必须保留。", body)
        self.assertIn(feedback["id"], body)
        self.assertEqual(read_note(self.project)[1], self.project_body)

    def test_replay_does_not_repeat_event_or_change_task_again(self):
        path = self.task()
        feedback = self.feedback()
        first = pm.apply_feedback(self.vault, feedback["id"])
        before = self.files()
        second = pm.apply_feedback(self.vault, feedback["id"])
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        self.assertEqual(self.files(), before)
        self.assertEqual(len(management_snapshot(self.vault)["events"]), 1)
        self.assertEqual(read_note(path)[0]["revision"], 2)

    def test_feedback_for_old_revision_does_not_close_new_task(self):
        path = self.task()
        feedback = self.feedback()
        props, body = read_note(path)
        props["revision"] = 2
        write_note(path, props, body + "\n新成果已替换。\n")
        with self.assertRaises(MarkdownError) as caught:
            pm.apply_feedback(self.vault, feedback["id"])
        self.assertEqual(caught.exception.code, "stale_feedback")
        self.assertEqual(read_note(path)[0]["status"], "待确认")
        self.assertEqual(len(management_snapshot(self.vault)["events"]), 0)

    def test_content_changed_without_revision_must_not_accept_old_feedback(self):
        path = self.task()
        feedback = self.feedback()
        props, body = read_note(path)
        write_note(path, props, body + "\n人工在 Obsidian 替换了成果，尚未修改 revision。\n")
        with self.assertRaises(MarkdownError):
            pm.apply_feedback(self.vault, feedback["id"])
        self.assertEqual(read_note(path)[0]["status"], "待确认")

    def test_boolean_target_revision_is_not_integer_revision(self):
        path = self.task()
        feedback = self.feedback()
        fpath = self.project.parent / "反馈" / (feedback["id"] + ".md")
        props, body = read_note(fpath)
        props["target_revision"] = True
        write_note(fpath, props, body)
        with self.assertRaises(MarkdownError):
            pm.apply_feedback(self.vault, feedback["id"])
        self.assertEqual(read_note(path)[0]["status"], "待确认")

    def test_revise_returns_task_to_work_and_keeps_feedback(self):
        path = self.task()
        feedback = self.feedback(result="需修改", text="日期格式需要调整。")
        pm.apply_feedback(self.vault, feedback["id"])
        props, body = read_note(path)
        self.assertEqual(props["status"], "待处理")
        self.assertEqual(props["human_result"], "需修改")
        self.assertIn("这段人写的说明必须保留。", body)

    def test_choice_only_for_decision_task(self):
        self.task()
        with self.assertRaises(MarkdownError) as caught:
            self.feedback(result="选择")
        self.assertEqual(caught.exception.code, "wrong_kind")

    def test_decision_choice_closes_task(self):
        path = self.task(kind="决策")
        feedback = self.feedback(result="选择", text="采用方案 A。")
        pm.apply_feedback(self.vault, feedback["id"])
        self.assertEqual(read_note(path)[0]["status"], "已完成")

    def test_ai_task_cannot_be_presented_as_human_feedback(self):
        self.task(owner="AI", kind="执行")
        with self.assertRaises(MarkdownError) as caught:
            self.feedback()
        self.assertEqual(caught.exception.code, "not_human_task")

    def test_missing_user_opinion_or_source_rejected(self):
        self.task()
        for text, source in [("", "用户说过"), ("通过", "")]:
            with self.subTest(text=text, source=source), self.assertRaises(MarkdownError):
                pm.submit_feedback(self.vault, "t-review", 1, "通过", text, source)

    def test_apply_requires_explicit_user_submitted_status(self):
        self.task()
        feedback = self.feedback()
        fpath = self.project.parent / "反馈" / (feedback["id"] + ".md")
        props, body = read_note(fpath)
        props["submitted_by"] = "AI推测"
        write_note(fpath, props, body)
        with self.assertRaises(MarkdownError) as caught:
            pm.apply_feedback(self.vault, feedback["id"])
        self.assertEqual(caught.exception.code, "invalid_feedback")

    def test_feedback_project_mismatch_rejected(self):
        self.task()
        other = {**self.project_props, "id": "p-other", "name": "另一个项目"}
        write_note(self.vault / "项目" / "其他" / "项目.md", other, self.project_body)
        feedback = self.feedback()
        fpath = self.project.parent / "反馈" / (feedback["id"] + ".md")
        props, body = read_note(fpath)
        props["project_id"] = "p-other"
        write_note(fpath, props, body)
        with self.assertRaises(MarkdownError) as caught:
            pm.apply_feedback(self.vault, feedback["id"])
        self.assertEqual(caught.exception.code, "invalid_feedback")


class ProjectLifecycleChecks(VaultFixture):
    def test_new_records_stay_beside_their_project_card(self):
        task = pm.add_task(self.vault, self.project_id, "明确范围", "决策", "选定范围")
        self.assertEqual(Path(task["path"]).parent, Path("项目/时间项目/任务"))
        feedback = pm.submit_feedback(self.vault, task["id"], 1, "选择", "沿用范围。", "隔离测试明确意见")
        pm.apply_feedback(self.vault, feedback["id"])
        self.assertEqual(len(list((self.project.parent / "反馈").glob("*.md"))), 1)
        self.assertEqual(len(list((self.project.parent / "历史").glob("*.md"))), 1)
        self.assertFalse((self.vault / "任务").exists())
        self.assertFalse((self.vault / "反馈").exists())
        self.assertFalse((self.vault / "事件").exists())

    def test_renamed_project_directory_resolves_by_identity(self):
        renamed = self.project.parent.with_name("改名后的项目")
        self.project.parent.rename(renamed)
        task = pm.add_task(self.vault, self.project_id, "继续工作", "执行", "有成果")
        self.assertEqual(Path(task["path"]).parent, Path("项目/改名后的项目/任务"))

    def test_task_directory_cannot_redirect_writes_outside_project(self):
        target = self.documents / "不能写到这里"
        target.mkdir()
        (self.project.parent / "任务").symlink_to(target, target_is_directory=True)
        with self.assertRaises(MarkdownError):
            pm.add_task(self.vault, self.project_id, "继续工作", "执行", "有成果")
        self.assertEqual(list(target.iterdir()), [])

    def test_review_task_without_evidence_stays_in_preparation(self):
        created = pm.add_task(self.vault, self.project_id, "以后验收", "验收", "完整流程可用")
        props, _ = read_note(self.vault / created["path"])
        self.assertEqual(props["status"], "待处理")

    def test_review_with_evidence_is_submitted(self):
        created = pm.add_task(self.vault, self.project_id, "检查已交付流程", "验收", "完整流程可用", "[[项目/时间项目/项目]]")
        props, _ = read_note(self.vault / created["path"])
        self.assertEqual(props["status"], "待确认")

    def test_new_project_keeps_code_and_management_separate(self):
        result = pm.create_project(self.vault, self.data, self.documents, "测试工程", "形成可验收成果", "🧪")
        folder = self.documents / "测试工程"
        self.assertEqual(result["folder"], str(folder.resolve()))
        self.assertTrue(folder.is_dir())
        self.assertEqual([p.name for p in folder.iterdir()], ["AGENTS.md"])
        self.assertTrue((self.vault / result["management_path"]).is_file())
        bridge = (folder / "AGENTS.md").read_text()
        self.assertIn(str(self.vault), bridge)
        self.assertIn("AI项目管理约定.md", bridge)
        self.assertIn(str(DEFAULT_RULES / "AI项目管理约定.md"), bridge)
        self.assertIn(str(DEFAULT_RULES / "数据约定.md"), bridge)
        self.assertNotIn(str(self.vault / "规则"), bridge)
        self.assertIn(result["project"]["project_id"], bridge)
        self.assertNotIn("## 总目标", bridge)

    def test_existing_engineering_rules_preserved_and_bridge_idempotent(self):
        folder = self.documents / "已有工程"
        folder.mkdir()
        original = "# 既有规则\n\n每次变更必须验证。\n保留历史归档。\n"
        agents = folder / "AGENTS.md"
        agents.write_text(original)
        result = pm.create_project(self.vault, self.data, self.documents, "已有工程", "继续已有成果", "📦", existing=True)
        first = agents.read_text()
        self.assertTrue(first.startswith(original))
        self.assertEqual(first.count("markdown-project-management:start"), 1)
        pm.install_bridge(folder, self.vault, result["management_path"])
        self.assertEqual(agents.read_text(), first)

    def test_existing_folder_requires_explicit_existing_flag(self):
        (self.documents / "已有工程").mkdir()
        with self.assertRaises(MarkdownError) as caught:
            pm.create_project(self.vault, self.data, self.documents, "已有工程", "继续", "📦")
        self.assertEqual(caught.exception.code, "folder_exists")

    def test_project_name_cannot_escape_documents(self):
        for name in ["../逃逸", "a/b", "a\\b", "..", "a:b", "坏\n名称"]:
            with self.subTest(name=name), self.assertRaises(MarkdownError):
                pm.create_project(self.vault, self.data, self.documents, name, "目标", "📦")

    def test_event_key_replay_reuses_history(self):
        first = pm.append_event(self.vault, self.project_id, "形成成果", "已有演示。", key="round-1")
        second = pm.append_event(self.vault, self.project_id, "形成成果", "已有演示。", key="round-1")
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(management_snapshot(self.vault)["events"]), 1)

    def test_same_event_key_with_changed_content_rejected(self):
        pm.append_event(self.vault, self.project_id, "形成成果", "原始事实。", key="round-1")
        before = self.files()
        with self.assertRaises(MarkdownError) as caught:
            pm.append_event(self.vault, self.project_id, "形成成果", "不同事实。", key="round-1")
        self.assertEqual(caught.exception.code, "immutable_event")
        self.assertEqual(before, self.files())

    def test_engineering_folder_must_not_be_inside_management_vault(self):
        with self.assertRaises(MarkdownError):
            pm.create_project(self.vault, self.data, self.vault, "错误工程", "不能混放", "📦")


class ArtifactReferenceChecks(VaultFixture):
    def setUp(self):
        super().setUp()
        self.engineering = self.documents / "时间工程"
        self.engineering.mkdir()
        self.source = self.engineering / "docs" / "产品说明.md"
        self.source.parent.mkdir()
        self.secret = "这是成果原文，仅保留在工程中。"
        self.source.write_text(self.secret)

    def register(self, **kwargs):
        args = dict(source_root=self.engineering, relative_path="docs/产品说明.md", version="v2",
                    summary="现有产品说明。", scope="只核对文件存在，未执行应用。")
        args.update(kwargs)
        return pm.register_artifact(self.vault, self.project_id, "产品说明", **args)

    def test_local_registration_only_writes_small_reference(self):
        source_before = self.source.read_bytes()
        row = self.register()
        path = self.vault / row["path"]
        props, body = read_note(path)
        self.assertEqual(path.parent, self.project.parent / "成果")
        self.assertTrue(path.name.startswith("产品说明-"))
        self.assertEqual(props["type"], "artifact")
        self.assertEqual(props["target_url"], self.source.resolve().as_uri())
        self.assertEqual(props["relative_path"], "docs/产品说明.md")
        self.assertTrue(props["local_only"])
        self.assertEqual(props["source_version"], "v2")
        self.assertNotIn(self.secret, body)
        self.assertEqual(source_before, self.source.read_bytes())
        self.assertEqual(len(management_snapshot(self.vault)["artifacts"]), 1)
        self.assertEqual(len(management_snapshot(self.vault)["events"]), 0)

    def test_identical_registration_is_idempotent(self):
        one = self.register()
        before = self.files()
        two = self.register()
        self.assertEqual(one["id"], two["id"])
        self.assertFalse(two["created"])
        self.assertFalse(two["changed"])
        self.assertEqual(self.files(), before)

    def test_update_preserves_human_prose_and_stable_identity(self):
        one = self.register()
        path = self.vault / one["path"]
        props, body = read_note(path)
        write_note(path, props, body + "\n## 我的补充\n\n请保留这句人工说明。\n")
        two = self.register(version="v3", summary="修订后的产品说明。")
        self.assertEqual(one["id"], two["id"])
        self.assertEqual(one["path"], two["path"])
        changed, body = read_note(path)
        self.assertEqual(changed["revision"], 2)
        self.assertEqual(changed["source_version"], "v3")
        self.assertIn("请保留这句人工说明。", body)
        self.assertIn("修订后的产品说明。", body)

    def test_reference_can_point_to_code_directory(self):
        row = self.register(relative_path="docs", kind="code")
        props, _ = read_note(self.vault / row["path"])
        self.assertEqual(props["kind"], "code")
        self.assertEqual(props["target_url"], self.source.parent.resolve().as_uri())

    def test_missing_and_escaping_paths_rejected(self):
        outside = self.documents / "别的文件.md"
        outside.write_text("不属于这个工程")
        for value in ["../别的文件.md", str(outside), "missing.md", "docs/../../别的文件.md"]:
            with self.subTest(path=value), self.assertRaises(MarkdownError):
                self.register(relative_path=value)
        self.assertEqual(management_snapshot(self.vault)["artifacts"], [])

    def test_symlink_escape_rejected(self):
        outside = self.documents / "外部.md"
        outside.write_text("不属于这个工程")
        (self.engineering / "链接.md").symlink_to(outside)
        with self.assertRaises(MarkdownError):
            self.register(relative_path="链接.md")

    def test_vault_is_not_an_engineering_root(self):
        with self.assertRaises(MarkdownError):
            self.register(source_root=self.vault, relative_path="项目/时间项目/项目.md")

    def test_artifact_directory_cannot_redirect_metadata_writes(self):
        target = self.documents / "不能写到这里"
        target.mkdir()
        (self.project.parent / "成果").symlink_to(target, target_is_directory=True)
        with self.assertRaises(MarkdownError):
            self.register()
        self.assertEqual(list(target.iterdir()), [])

    def test_external_reference_is_not_falsely_marked_tested(self):
        row = pm.register_artifact(self.vault, self.project_id, "试用入口", url="https://example.com/app", kind="url")
        props, body = read_note(self.vault / row["path"])
        self.assertFalse(props["local_only"])
        self.assertIn("未检查可达性", props["availability"])
        self.assertIn("未验证", body)

    def test_unsafe_or_ambiguous_urls_rejected(self):
        for url in ["javascript:alert(1)", "http://example.com", "https://u:p@example.com", "https://[broken", "https://example.com/a\nb"]:
            with self.subTest(url=url), self.assertRaises(MarkdownError):
                pm.register_artifact(self.vault, self.project_id, "入口", url=url)
        with self.assertRaises(MarkdownError):
            self.register(url="https://example.com")

    def test_cli_exposes_artifact_registration(self):
        import contextlib
        import io
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = pm.main(["--vault", str(self.vault), "--data-dir", str(self.data), "register-artifact",
                            "--project", self.project_id, "--title", "产品说明", "--source-root", str(self.engineering),
                            "--relative-path", "docs/产品说明.md", "--version", "v2"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output.getvalue())["created"])


class ReadOnlyHTTPChecks(VaultFixture):
    def setUp(self):
        super().setUp()
        self.task("t-review")
        self.task("t-decision", kind="决策")
        self.task("t-ai", owner="AI", kind="执行", status="进行中")
        self.server = api.ThreadingHTTPServer(("127.0.0.1", 0), api.make_handler(self.vault, self.data))
        self.server.daemon_threads = True
        self.server.handle_error = lambda request, address: None
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, method="GET", headers=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            raw = response.read()
            return response.status, dict(response.getheaders()), json.loads(raw) if raw else None
        finally:
            connection.close()

    def test_http_get_reads_notes_only_without_mutation(self):
        before = self.files()
        with patch("codex_usage.scan_usage", side_effect=AssertionError("must not scan logs")), \
             patch("widget_backend.fetch_official_limits", side_effect=AssertionError("must not fetch quota")):
            status, _, body = self.request("/api/v1/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(body["provider"], "markdown")
        self.assertEqual(self.files(), before)
        self.assertFalse(self.data.exists())

    def test_project_collection_filter_and_detail(self):
        status, _, body = self.request("/api/v1/projects?project_id=p-test")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["total"], 1)
        self.assertEqual(body["data"]["items"][0]["project_id"], self.project_id)
        status, _, body = self.request("/api/v1/projects/p-test")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["data"]["tasks"]), 3)
        self.assertEqual(body["data"]["project"]["project_goal"], "可靠记录时间。")

    def test_task_filter_kind_status_and_project(self):
        status, _, body = self.request("/api/v1/tasks?project_id=p-test&kind=%E9%AA%8C%E6%94%B6&status=%E5%BE%85%E7%A1%AE%E8%AE%A4")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["total"], 1)
        self.assertEqual(body["data"]["items"][0]["id"], "t-review")

    def test_overview_excludes_future_review(self):
        self.task("t-future-review", status="待处理")
        status, _, body = self.request("/api/v1/overview")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["data"]["needs_review"]), 1)
        self.assertEqual(body["data"]["needs_review"][0]["id"], "t-review")

    def test_current_actions_do_not_include_later_stage_tasks(self):
        later = self.task("t-release", owner="AI", kind="执行", status="待处理")
        props, body = read_note(later)
        write_note(later, {**props, "phase": "发布试用并收集反馈"}, body)
        done = self.task("t-completed", owner="AI", kind="执行", status="已完成")
        status, _, body = self.request("/api/v1/overview")
        self.assertEqual(status, 200)
        self.assertEqual({t["id"] for t in body["data"]["current_actions"]}, {"t-review", "t-decision", "t-ai"})
        self.assertEqual([t["id"] for t in body["data"]["future_tasks"]], ["t-release"])

    def test_artifact_endpoint_returns_reference_without_project_file_contents(self):
        engineering = self.documents / "API工程"
        engineering.mkdir()
        source = engineering / "成果.txt"
        source.write_text("原文留在工程，接口不应返回这些字节。")
        artifact = pm.register_artifact(self.vault, self.project_id, "真实成果", source_root=engineering,
                                        relative_path="成果.txt", version="v1", summary="成果的短说明")
        before = self.files()
        status, _, payload = self.request("/api/v1/artifacts?project_id=p-test")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["total"], 1)
        row = payload["data"]["items"][0]
        self.assertEqual(row["id"], artifact["id"])
        self.assertEqual(row["target_url"], source.resolve().as_uri())
        self.assertTrue(row["local_only"])
        self.assertNotIn(source.read_text(), json.dumps(payload, ensure_ascii=False))
        self.assertEqual(self.files(), before)
        detail = self.request("/api/v1/projects/p-test")[2]["data"]
        self.assertEqual([a["id"] for a in detail["artifacts"]], [artifact["id"]])
        self.assertEqual(self.request("/api/v1/files?path=" + quote(str(source)))[0], 404)

    def test_pagination_and_invalid_query(self):
        status, _, body = self.request("/api/v1/tasks?limit=1&offset=1")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["total"], 3)
        self.assertEqual(len(body["data"]["items"]), 1)
        self.assertEqual(self.request("/api/v1/tasks?limit=bad")[0], 400)

    def test_post_put_patch_delete_are_read_only(self):
        before = self.files()
        with patch("management_api.management_snapshot", side_effect=AssertionError("write verb must not reach provider")):
            for verb in ("POST", "PUT", "PATCH", "DELETE"):
                with self.subTest(verb=verb):
                    status, _, body = self.request("/api/v1/projects", method=verb, body="{}")
                    self.assertEqual(status, 405)
                    self.assertEqual(body["error"]["code"], "read_only")
        self.assertEqual(before, self.files())

    def test_host_rejected(self):
        for host in ["evil.example", "localhost.evil.example", "127.0.0.1.evil.example", ""]:
            with self.subTest(host=host):
                self.assertEqual(self.request("/api/v1/projects", headers={"Host": host})[0], 403)

    def test_external_and_null_origin_rejected(self):
        for origin in ["https://evil.example", "http://localhost.evil.example", "null"]:
            with self.subTest(origin=origin):
                self.assertEqual(self.request("/api/v1/projects", headers={"Origin": origin})[0], 403)

    def test_malformed_origin_is_rejected_with_http_response(self):
        self.assertEqual(self.request("/api/v1/projects", headers={"Origin": "http://[broken"})[0], 403)

    def test_etag_revalidation_and_change(self):
        status, headers, _ = self.request("/api/v1/snapshot")
        self.assertEqual(status, 200)
        etag = headers["ETag"]
        status, _, body = self.request("/api/v1/snapshot", headers={"If-None-Match": etag})
        self.assertEqual(status, 304)
        self.assertIsNone(body)
        props, text = read_note(self.project)
        write_note(self.project, props, text.replace("可靠记录时间。", "可靠记录时间并支持纠错。"))
        status, newer, body = self.request("/api/v1/snapshot", headers={"If-None-Match": etag})
        self.assertEqual(status, 200)
        self.assertNotEqual(newer["ETag"], etag)

    def test_missing_vault_and_invalid_note_return_503(self):
        original = api.management_snapshot
        with patch("management_api.management_snapshot", side_effect=lambda v, d: original(self.root / "不存在", d)):
            self.assertEqual(self.request("/api/v1/projects")[0], 503)
        self.project.write_text("---\ntype: project\nid: p-test\nstatus: invalid\n---\n")
        self.assertEqual(self.request("/api/v1/projects")[0], 503)

    def test_unknown_route_or_project_does_not_serve_file(self):
        self.assertEqual(self.request("/api/v1/projects/missing")[0], 404)
        self.assertEqual(self.request("/../../etc/passwd")[0], 404)
        self.assertEqual(self.request("/api/v1/files?path=/etc/passwd")[0], 404)

    def test_head_is_read_only_without_body(self):
        status, headers, body = self.request("/api/v1/projects", method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertIsNone(body)

    def test_local_service_health_ignores_external_proxy(self):
        import os
        with patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9", "NO_PROXY": "", "no_proxy": ""}):
            result = service_control.health(self.vault, self.server.server_port)
        self.assertTrue(result["ready"])

    def test_service_default_vault_stays_in_central_documents_location(self):
        self.assertEqual(service_control.VAULT, Path.home() / "Documents" / "项目管理obsidian")
        self.assertEqual(service_control.VAULT, DEFAULT_VAULT)
        self.assertEqual(DEFAULT_RULES, Path.home() / "Documents" / "项目管理系统" / "规则")


if __name__ == "__main__":
    unittest.main()
