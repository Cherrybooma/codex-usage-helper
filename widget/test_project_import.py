"""Offline forward integration tests: cloud effects, retries and local attribution."""
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_notion_sync import FakeNotion, NOW, PARENT
import notion_sync as ns
import project_import as pi
from project_store import ProjectRegistry, aggregate


def manifest():
    return {"version": 1, "project": {"name": "测试项目", "icon": {"type": "emoji", "emoji": "🧭"},
        "vision": "将已有素材整理成可使用的目录。", "status": "开发中", "health": "正常",
        "current_goal": "完成真实素材的目录验收。", "next_step": "检查目录入口与分类准确度。", "summary": "索引已生成，等待验收。",
        "progress": {"percent": 65, "evidence": "索引交付已通过，内容验收仍待完成。", "reviewed_at": NOW.isoformat(),
            "milestones": [{"name": "生成索引", "weight": 50, "completion": 100, "evidence": "产出索引存在"},
                           {"name": "目录验收", "weight": 50, "completion": 30, "evidence": "抽样验证完成"}]},
        "artifacts": [{"title": "目录索引", "relative_path": "index.md", "result": "生成分组与文件入口。"}],
        "manual_actions": [{"id": "accept-index", "name": "验收目录", "status": "待确认", "decision": "确认分类是否可用",
                            "recommendation": "先检查三个分组", "impact": "决定是否开始正式使用"}],
        "history": [{"id": "index-first", "title": "生成目录索引", "result": "已生成可浏览索引，原完成时间缺少可靠记录。"}]}}


class ImportAPI(FakeNotion):
    def __init__(self):
        super().__init__()
        self.blocks = {}
        self.hidden_key = None

    def add_blocks(self, parent, children):
        result = []
        for child in children:
            obj = deepcopy(child)
            obj["id"] = self.uid()
            obj["parent"] = {"block_id": parent}
            kind = obj["type"]
            nested = obj[kind].pop("children", [])
            self.blocks[obj["id"]] = obj
            if nested:
                self.add_blocks(obj["id"], nested)
            result.append(obj)
        return result

    def _apply(self, method, path, payload, url):
        if method == "POST" and path == "/databases":
            database_id, source_id = self.uid(), self.uid()
            obj = {"id": database_id, "parent": payload["parent"], "description": payload["description"],
                   "title": payload["title"], "properties": payload["initial_data_source"]["properties"],
                   "data_sources": [{"id": source_id}], "url": "https://app.notion.com/p/" + database_id}
            self.databases[database_id] = obj
            return 200, {}, obj
        if method == "PATCH" and path.startswith("/data_sources/"):
            source_id = path.split("/")[-1]
            db = next(d for d in self.databases.values() if d["data_sources"][0]["id"] == source_id)
            db["properties"].update(deepcopy(payload["properties"]))
            return 200, {}, {"id": source_id, "properties": db["properties"]}
        if method == "POST" and path == "/pages" and payload["parent"]["type"] == "page_id":
            page_id = self.uid()
            page = {"id": page_id, "parent": payload["parent"], "properties": payload["properties"],
                    "icon": payload.get("icon"), "url": "https://app.notion.com/p/" + page_id}
            self.pages[page_id] = page
            self.add_blocks(page_id, payload.get("children", []))
            return 200, {}, page
        if method == "GET" and path.endswith("/children"):
            parent = path.split("/")[2]
            result = [deepcopy(b) for b in self.blocks.values()
                      if b["parent"]["block_id"] == parent and not b.get("archived")]
            result.extend({"id": p["id"], "type": "child_page", "child_page": {"title": ns._plain(p["properties"]["title"]["title"])}}
                          for p in self.pages.values() if p.get("parent", {}).get("page_id") == parent)
            result.extend({"id": d["id"], "type": "child_database"} for d in self.databases.values()
                          if d.get("parent", {}).get("page_id") == parent)
            return self._paginate(result, url)
        if method == "PATCH" and path.endswith("/children"):
            parent = path.split("/")[2]
            return 200, {}, {"results": self.add_blocks(parent, payload["children"])}
        if method == "PATCH" and path.startswith("/blocks/"):
            block_id = path.split("/")[-1]
            self.blocks[block_id].update(deepcopy(payload))
            return 200, {}, self.blocks[block_id]
        if method == "POST" and path.endswith("/query") and self.hidden_key:
            filt = payload.get("filter", {})
            if filt.get("rich_text", {}).get("equals") == self.hidden_key:
                return 200, {}, {"results": [], "has_more": False}
        result = super()._apply(method, path, payload, url)
        if method == "PATCH" and path.startswith("/pages/") and "icon" in payload:
            self.pages[path.split("/")[-1]]["icon"] = payload["icon"]
        return result


class ProjectImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.task_root = self.root / "Documents"
        self.folder = self.task_root / "项目"
        self.folder.mkdir(parents=True)
        (self.folder / "index.md").write_text("目录索引", encoding="utf-8")
        self.api = ImportAPI()
        self.ws = pi.ProjectImportWorkspace(self.root / "data", token_provider=lambda: "TEST_SECRET_DO_NOT_PRINT",
                                            transport=self.api, clock=lambda: NOW, sleeper=lambda _: None)

    def import_one(self, value=None):
        value, task_id = pi.prepare_local(value or manifest(), self.folder, self.task_root,
                                         self.root / "data", self.root / "codex", save=True)
        return self.ws.import_project(value, task_id, management_home=PARENT)

    def test_complete_import_is_idempotent_and_preserves_history(self):
        first = self.import_one()
        original_event = next(p for p in self.api.pages.values() if "Event Key" in p["properties"])
        first_counts = len(self.api.pages), len(self.api.databases), len(self.api.blocks)
        changed = manifest()
        changed["project"]["history"][0]["result"] = "不能覆盖旧事实"
        changed["project"]["history"][0]["title"] = "不能覆盖旧标题"
        changed["project"]["history"][0]["occurred_at"] = "2026-01-01T00:00:00Z"
        second = self.import_one(changed)
        self.assertEqual(first["page_id"], second["page_id"])
        # A changed manifest adds one import event. Both the original Events
        # fact and its managed preview preserve the original result.
        self.assertEqual((first_counts[0] + 1, *first_counts[1:]),
                         (len(self.api.pages), len(self.api.databases), len(self.api.blocks)))
        changed_counts = len(self.api.pages), len(self.api.databases), len(self.api.blocks)
        self.import_one(changed)
        self.assertEqual(changed_counts,
                         (len(self.api.pages), len(self.api.databases), len(self.api.blocks)))
        self.assertIn("本次回填，原完成时间未核实", ns._plain(original_event["properties"]["Result"]["rich_text"]))
        self.assertNotIn("不能覆盖", ns._plain(original_event["properties"]["Result"]["rich_text"]))
        active = self.ws.state["project_imports"][first["page_id"]]["layout_ids"]
        history_preview = ns._plain(self.api.blocks[active[6]]["paragraph"]["rich_text"])
        self.assertNotIn("不能覆盖", history_preview)
        self.assertIn("已生成可浏览索引", history_preview)
        self.assertIn("本次回填，原完成时间未核实", history_preview)
        self.assertNotIn("2026-01-01", history_preview)
        self.assertEqual(self.ws.cached_projects()[0]["human_open_tasks"], 1)
        self.assertEqual(self.ws.cached_projects()[0]["development_progress"], 65)
        self.assertEqual(self.ws.cached_projects()[0]["progress_source"], "ai")

    def test_missing_home_creates_one_shared_management_page_and_four_databases(self):
        value, task_id = pi.prepare_local(manifest(), self.folder, self.task_root, self.root / "data", self.root / "codex", save=True)
        first = self.ws.import_project(value, task_id, parent_page=PARENT)
        second = self.ws.import_project(value, task_id, parent_page=PARENT)
        self.assertEqual(first["management_home_id"], second["management_home_id"])
        homes = [p for p in self.api.pages.values() if p.get("parent", {}).get("page_id") == PARENT]
        self.assertEqual(len(homes), 1)
        self.assertEqual(len(self.api.databases), 4)
        self.assertTrue(all(db["parent"]["page_id"] == first["management_home_id"] for db in self.api.databases.values()))

    def test_existing_events_source_is_reused(self):
        self.import_one()
        original = self.ws.config["events_data_source_id"]
        self.import_one()
        self.assertEqual(self.ws.config["events_data_source_id"], original)
        self.assertEqual(len(self.api.databases), 4)

    def test_existing_dashboard_views_and_user_blocks_are_preserved(self):
        self.ws.setup(PARENT)
        registry = ProjectRegistry(self.root / "data", self.root / "codex")
        task_id = registry.identify("folder:" + str(self.folder), self.folder.name)
        registry.save()
        page = self.ws.create_project(task_id, "原有项目", {"type": "emoji", "emoji": "🧭"})
        old = self.api.add_blocks(page["page_id"], [pi._block("paragraph", "用户原有愿景"),
            {"object": "block", "type": "child_database", "child_database": {"title": "已存在原生视图"}}])
        result = self.import_one()
        self.assertTrue(result["layout_preserved_existing"])
        self.assertTrue(all(not self.api.blocks[b["id"]].get("archived") for b in old))
        self.assertEqual(sum(b.get("type") == "heading_2" for b in self.api.blocks.values()), 0)

    def test_missing_progress_does_not_invent_an_ai_estimate(self):
        value = manifest()
        value["project"]["progress"] = None
        self.import_one(value)
        self.assertIsNone(self.ws.cached_projects()[0]["development_progress"])

    def test_page_body_exposes_goal_progress_and_real_ledger_links(self):
        value = manifest()
        imported = self.import_one(value)
        active = self.ws.state["project_imports"][imported["page_id"]]["layout_ids"]
        top = [self.api.blocks[block_id] for block_id in active]
        self.assertEqual([ns._plain(b["heading_2"]["rich_text"]) for b in top if b["type"] == "heading_2"],
                         ["待人工确认", "当前进展与下一步", "历史开发记录", "Token 投入"])
        column_list = next(b for b in top if b["type"] == "column_list")
        left = next(b for b in self.api.blocks.values() if b["parent"]["block_id"] == column_list["id"])
        left_blocks = [b for b in self.api.blocks.values() if b["parent"]["block_id"] == left["id"]]
        self.assertEqual(ns._plain(left_blocks[0]["heading_3"]["rich_text"]), "当前目标与进展")
        self.assertEqual(ns._plain(left_blocks[1]["paragraph"]["rich_text"]),
                         "当前目标：" + value["project"]["current_goal"] + "\n进展：" + value["project"]["summary"])
        history = top[6]["paragraph"]["rich_text"]
        self.assertIn("生成目录索引", ns._plain(history))
        self.assertIn("本次回填，原完成时间未核实", ns._plain(history))
        self.assertEqual(history[0]["text"]["link"]["url"], self.ws.config["events_url"])
        usage = top[8]["paragraph"]["rich_text"]
        self.assertIn("本机扫描", ns._plain(usage))
        self.assertIn("按当前项目查看", ns._plain(usage))
        self.assertNotIn("0", ns._plain(usage))
        self.assertNotIn("额度", ns._plain(usage))
        self.assertEqual(usage[0]["text"]["link"]["url"], self.ws.config["usage_url"])

    def test_history_preview_is_bounded_dated_and_keeps_full_events(self):
        value = manifest()
        value["project"]["history"] = [
            {"id": "old", "title": "较早事件", "result": "较早结果", "occurred_at": "2026-01-01T00:00:00Z"},
            {"id": "recent", "title": "最新事件", "result": "结果" * 200, "occurred_at": "2026-01-03T00:00:00Z"},
            {"id": "middle", "title": "中间事件", "result": "中间结果", "occurred_at": "2026-01-02T00:00:00Z"},
            {"id": "unknown", "title": "未知日期事件", "result": "日期未核实"}]
        imported = self.import_one(value)
        active = self.ws.state["project_imports"][imported["page_id"]]["layout_ids"]
        history = ns._plain(self.api.blocks[active[6]]["paragraph"]["rich_text"])
        self.assertLess(history.index("最新事件"), history.index("中间事件"))
        self.assertLess(history.index("中间事件"), history.index("较早事件"))
        self.assertNotIn("未知日期事件", history)
        self.assertIn("2026-01-03T00:00:00Z", history)
        self.assertIn("…", history)
        events = [page for page in self.api.pages.values() if "Event Key" in page["properties"]]
        self.assertEqual(len(events), 5)  # All four facts plus the actual import.
        recent = next(page for page in events if ns._plain(page["properties"]["Name"]["title"]) == "最新事件")
        self.assertEqual(ns._plain(recent["properties"]["Result"]["rich_text"]), "结果" * 200)
        counts = len(self.api.pages), len(self.api.blocks)
        self.import_one(value)
        self.assertEqual((len(self.api.pages), len(self.api.blocks)), counts)

    def test_full_goal_and_summary_fit_rich_text_and_tail_changes_refresh_layout(self):
        value = manifest()
        value["project"]["current_goal"] = "目" * 1000
        value["project"]["summary"] = "进" * 1000
        imported = self.import_one(value)
        expected = "当前目标：" + value["project"]["current_goal"] + "\n进展：" + value["project"]["summary"]
        paragraphs = [b["paragraph"]["rich_text"] for b in self.api.blocks.values() if b["type"] == "paragraph"]
        rich = next(r for r in paragraphs if "".join(item["text"]["content"] for item in r) == expected)
        self.assertTrue(all(len(item["text"]["content"]) <= 2000 for item in rich))
        before = self.ws.state["project_imports"][imported["page_id"]]["layout_fingerprint"]
        value["project"]["summary"] = "进" * 999 + "尾"
        self.import_one(value)
        after = self.ws.state["project_imports"][imported["page_id"]]["layout_fingerprint"]
        self.assertNotEqual(before, after)

    def test_empty_history_does_not_fabricate_facts_or_dates(self):
        value = manifest()
        value["project"]["history"] = []
        imported = self.import_one(value)
        active = self.ws.state["project_imports"][imported["page_id"]]["layout_ids"]
        history = ns._plain(self.api.blocks[active[6]]["paragraph"]["rich_text"])
        self.assertEqual(history, "本次未提供历史摘要。\n打开完整开发记录")
        self.assertEqual(sum("Event Key" in page["properties"] for page in self.api.pages.values()), 1)

    def test_compact_page_keeps_large_manual_list_in_tasks(self):
        value = manifest()
        value["project"]["manual_actions"] = [{"id": str(i), "name": "人工确认" + str(i),
            "recommendation": "推荐" * 300, "impact": "影响" * 300} for i in range(30)]
        self.import_one(value)
        self.assertEqual(self.ws.cached_projects()[0]["human_open_tasks"], 30)
        self.assertTrue(all(len(ns._plain(b.get(b["type"], {}).get("rich_text"))) <= 2000
                            for b in self.api.blocks.values()))

    def test_layout_timeout_recovers_without_duplicate_blocks(self):
        self.api.schedule(lambda m, p, _: m == "PATCH" and p.endswith("/children"), (503, {}, {}), after=True)
        with self.assertRaises(ns.NotionError):
            self.import_one()
        count = len(self.api.blocks)
        self.import_one()
        self.assertEqual(len(self.api.blocks), count)
        self.assertFalse(self.ws.state["uncertain_creates"])

    def test_layout_recovery_checks_content_and_never_claims_same_shape_external_blocks(self):
        self.api.schedule(lambda m, p, _: m == "PATCH" and p.endswith("/children"), (503, {}, {}))
        with self.assertRaises(ns.NotionError): self.import_one()
        page = next(p for p in self.api.pages.values() if "Binding Key" in p["properties"])
        fake = [pi._block("paragraph", "外部内容"), pi._block("heading_2", "待人工确认"),
                pi._block("callout", "外部内容"), pi._block("heading_2", "当前进展与下一步"),
                {"object": "block", "type": "column_list", "column_list": {"children": []}},
                pi._block("heading_2", "历史开发记录"), pi._block("paragraph", "外部内容"),
                pi._block("heading_2", "Token 投入"), pi._block("paragraph", "外部内容")]
        added = self.api.add_blocks(page["id"], fake)
        with self.assertRaises(ns.NotionError) as caught: self.import_one()
        self.assertEqual(caught.exception.code, "write_uncertain")
        self.assertTrue(all(not self.api.blocks[b["id"]].get("archived") for b in added))

    def test_changed_manifest_after_timeout_recovers_old_layout_then_updates_it(self):
        self.api.schedule(lambda m, p, _: m == "PATCH" and p.endswith("/children"), (503, {}, {}), after=True)
        with self.assertRaises(ns.NotionError): self.import_one()
        value = manifest(); value["project"]["summary"] = "新增核对已完成，等待验收。"
        result = self.import_one(value)
        blocks = [b for b in self.api.blocks.values() if b["parent"]["block_id"] == result["page_id"] and not b.get("archived")]
        self.assertEqual(sum(b["type"] == "heading_2" for b in blocks), 4)
        self.assertFalse(self.ws.state["uncertain_creates"])
        self.assertTrue(any(ns._plain(b.get("paragraph", {}).get("rich_text")) ==
                            "当前目标：" + value["project"]["current_goal"] + "\n进展：" + value["project"]["summary"]
                            and not b.get("archived") for b in self.api.blocks.values()))

    def test_old_manifest_retry_does_not_skip_pending_new_layout_recovery(self):
        original = self.import_one()
        changed = manifest(); changed["project"]["summary"] = "中间版本B"
        self.api.schedule(lambda m, p, _: m == "PATCH" and p.endswith("/children"), (503, {}, {}), after=True)
        with self.assertRaises(ns.NotionError): self.import_one(changed)
        self.import_one()
        self.assertFalse(self.ws.state["uncertain_creates"])
        active = self.ws.state["project_imports"][original["page_id"]]["layout_ids"]
        column_list = next(self.api.blocks[b] for b in active if self.api.blocks[b]["type"] == "column_list")
        columns = [b for b in self.api.blocks.values() if b["parent"]["block_id"] == column_list["id"]]
        paragraphs = [b for b in self.api.blocks.values() if b["parent"]["block_id"] in [c["id"] for c in columns]]
        texts = [ns._plain(b.get("paragraph", {}).get("rich_text")) for b in paragraphs]
        self.assertIn("当前目标：" + manifest()["project"]["current_goal"] + "\n进展：" + manifest()["project"]["summary"], texts)
        self.assertFalse(any("中间版本B" in text for text in texts))

    def test_committed_id_must_match_requested_parent_and_pending_is_retained(self):
        with self.ws._locked():
            self.ws.state["uncertain_creates"]["guard"] = {"committed_id": PARENT}
            with self.assertRaises(ns.NotionError):
                self.ws._create_unique("guard", lambda: [], {"parent": {"type": "page_id", "page_id": PARENT},
                    "properties": {"title": {"title": ns._text("目标")}}})
            self.assertIn("guard", self.ws.state["uncertain_creates"])
        self.assertFalse(any(m == "POST" and url.endswith("/pages") for m, url, _ in self.api.calls))

    def test_found_history_clears_old_uncertain_marker_without_overwriting_fact(self):
        self.import_one()
        event = next(p for p in self.api.pages.values() if "Event Key" in p["properties"])
        key = ns._plain(event["properties"]["Event Key"]["rich_text"])
        before = deepcopy(event["properties"])
        with self.ws._locked(): self.ws.state["uncertain_creates"]["import-row:" + key] = {"kind": "project_import"}
        self.import_one()
        self.assertNotIn("import-row:" + key, self.ws.state["uncertain_creates"])
        self.assertEqual(event["properties"], before)

    def test_uncertain_not_yet_visible_page_stops_creation_until_recovered(self):
        self.ws.setup(PARENT)
        self.api.hide_created = False
        # Hide the first new project by its Binding Key during recovery queries.
        registry = ProjectRegistry(self.root / "data", self.root / "codex")
        task_id = registry.identify("folder:" + str(self.folder), self.folder.name)
        import hashlib
        key = hashlib.sha256((self.ws.config["dataset_id"] + ":" + task_id).encode()).hexdigest()
        self.api.hidden_key = key
        self.api.schedule(lambda m, p, v: m == "POST" and p == "/pages" and "Binding Key" in v.get("properties", {}),
                          (503, {}, {}), after=True)
        with self.assertRaises(ns.NotionError) as first:
            self.ws.import_project(manifest(), task_id)
        self.assertEqual(first.exception.code, "write_uncertain")
        count = len(self.api.pages)
        with self.assertRaises(ns.NotionError):
            self.ws.import_project(manifest(), task_id)
        self.assertEqual(len(self.api.pages), count)
        self.api.hidden_key = None
        self.ws.import_project(manifest(), task_id)
        self.assertEqual(sum("Binding Key" in p["properties"] for p in self.api.pages.values()), 1)

    def test_manifest_rejects_private_fields_missing_icon_and_fake_progress(self):
        cases = []
        value = manifest(); value["project"]["auth_token"] = "secret"; cases.append(value)
        value = manifest(); value["project"].pop("icon"); cases.append(value)
        value = manifest(); value["project"]["vision"] = "/Users/private/source.py"; cases.append(value)
        value = manifest(); value["project"]["progress"]["percent"] = 90; cases.append(value)
        value = manifest(); value["project"]["progress"]["percent"] = True; cases.append(value)
        value = manifest(); value["project"]["history"] *= 2; cases.append(value)
        value = manifest(); value["project"]["artifacts"][0]["url"] = "http://127.0.0.1/private"; cases.append(value)
        for value in cases:
            with self.subTest(value=str(list(value["project"].keys()))), self.assertRaises(ns.NotionError):
                pi.validate_manifest(value, self.folder)

    def test_local_output_must_exist_and_cannot_escape_project(self):
        for path in ("missing.md", "../outside.md", "/tmp/private.md"):
            value = manifest(); value["project"]["artifacts"][0]["relative_path"] = path
            with self.subTest(path=path), self.assertRaises(ns.NotionError):
                pi.validate_manifest(value, self.folder)
        outside = self.root / "outside.md"; outside.write_text("private")
        (self.folder / "escape.md").symlink_to(outside)
        value = manifest(); value["project"]["artifacts"][0]["relative_path"] = "escape.md"
        with self.assertRaises(ns.NotionError): pi.validate_manifest(value, self.folder)

    def test_verified_https_outputs_are_supported_without_a_local_copy(self):
        value = manifest()
        value["project"]["artifacts"] = [{"title": "正式产出", "url": "https://example.com/output", "result": "已发布验收页"}]
        value["project"]["manual_actions"][0]["evidence_url"] = "https://example.com/output"
        validated = pi.validate_manifest(value, self.folder)
        self.assertEqual(validated["project"]["artifacts"][0]["url"], "https://example.com/output")
        self.import_one(value)
        self.assertEqual(self.ws.cached_projects()[0]["latest_output_url"], "https://example.com/output")

    def test_dry_run_never_reads_credentials_or_writes_configuration(self):
        command = {"manifest": manifest(), "folder": str(self.folder), "task_root": str(self.task_root)}
        stream = io.TextIOWrapper(io.BytesIO(json.dumps(command).encode()))
        output = io.StringIO()
        with patch.object(sys, "stdin", stream), patch("sys.stdout", output), patch.object(ns, "configured_token", side_effect=AssertionError("credential read")):
            result = pi.main(["--data-dir", str(self.root / "dry"), "--dry-run"])
        self.assertEqual(result, 0)
        self.assertFalse((self.root / "dry").exists())
        self.assertFalse(json.loads(output.getvalue())["result"]["cloud_written"])
        self.assertNotIn(str(self.folder), output.getvalue())

    def test_folder_selection_must_match_direct_child_accounting_unit(self):
        nested = self.folder / "repo"; nested.mkdir()
        for folder in (self.task_root, nested, self.root):
            with self.subTest(folder=folder), self.assertRaises(ns.NotionError):
                pi.prepare_local(manifest(), folder, self.task_root, self.root / "data", self.root / "codex")

    def test_cli_cannot_change_existing_accounting_root_while_importing(self):
        registry = ProjectRegistry(self.root / "data", self.root / "codex")
        registry.data["task_root"] = str(self.root / "different-root")
        registry.save()
        before = registry.path.read_bytes()
        with self.assertRaises(ns.NotionError):
            pi.prepare_local(manifest(), self.folder, self.task_root, self.root / "data", self.root / "codex", save=True)
        self.assertEqual(registry.path.read_bytes(), before)

    def test_binding_uses_same_registry_as_scanner_and_never_sends_absolute_paths(self):
        result = self.import_one()
        registry = ProjectRegistry(self.root / "data", self.root / "codex")
        self.assertEqual(registry.identify("folder:" + str(self.folder), self.folder.name), result["task_id"])
        for _, _, payload in self.api.calls:
            serialized = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn(str(self.folder), serialized)
            self.assertNotIn("TEST_SECRET_DO_NOT_PRINT", serialized)
        from test_project_store import fact, report
        source = fact(self.folder / "repo", 123, ["local-session"])
        rows, daily, _ = aggregate(report([source]), self.task_root, registry,
                                  {result["task_id"]: self.ws.project_for_task(result["task_id"])})
        self.assertEqual(rows[0]["task_id"], "notion:" + result["page_id"])
        self.assertEqual(rows[0]["total_tokens"], 123)


if __name__ == "__main__":
    unittest.main()
