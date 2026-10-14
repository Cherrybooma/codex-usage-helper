"""Network-free integration tests against a stateful Notion API double."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlparse
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import notion_sync as ns

NOW = datetime(2026, 10, 13, 8, tzinfo=timezone.utc)
PARENT = str(uuid.UUID(int=1))
PROJECT_ICON = {"type": "emoji", "emoji": "📦"}


class FakeNotion:
    """Stores remote state; the client cannot inspect it except via endpoints."""
    def __init__(self):
        self.sequence = 100
        self.databases = {}
        self.pages = {PARENT: {"id": PARENT, "properties": {}, "parent": {"type": "workspace"}}}
        self.calls = []
        self.failures = []
        self.page_size = 100
        self.hide_created = False

    def uid(self):
        self.sequence += 1
        return str(uuid.UUID(int=self.sequence))

    def schedule(self, predicate, response, *, after=False):
        self.failures.append((predicate, response, after))

    def __call__(self, method, url, headers, payload):
        self.calls.append((method, url, deepcopy(payload)))
        assert url.startswith(ns.API_BASE)
        assert headers["Notion-Version"] == "2026-03-11"
        assert headers["Authorization"] == "Bearer TEST_SECRET_DO_NOT_PRINT"
        path = urlparse(url).path.removeprefix("/v1")
        failure = None
        for index, (predicate, response, after) in enumerate(self.failures):
            if predicate(method, path, payload):
                failure = (response, after)
                self.failures.pop(index)
                break
        if failure and not failure[1]:
            return self._failure(failure[0])
        result = self._apply(method, path, payload, url)
        if failure:
            return self._failure(failure[0])
        return deepcopy(result)

    def _failure(self, response):
        if isinstance(response, Exception):
            raise response
        return deepcopy(response)

    def _apply(self, method, path, payload, url):
        if method == "POST" and path == "/databases":
            # Contract is current data_sources API, not pre-2025 properties.
            assert "properties" not in payload
            assert payload["parent"] == {"type": "page_id", "page_id": PARENT}
            schema = payload["initial_data_source"]["properties"]
            database_id, source_id = self.uid(), self.uid()
            obj = {"id": database_id, "parent": payload["parent"], "description": payload["description"],
                   "title": payload["title"], "properties": schema, "data_sources": [{"id": source_id}],
                   "url": "https://app.notion.com/p/" + database_id}
            self.databases[database_id] = obj
            return 200, {}, obj
        if method == "GET" and path.startswith("/databases/"):
            return 200, {}, self.databases[path.split("/")[-1]]
        if method == "GET" and path.startswith("/data_sources/"):
            source_id = path.split("/")[-1]
            database = next(db for db in self.databases.values() if db["data_sources"][0]["id"] == source_id)
            properties = deepcopy(database["properties"])
            for prop in properties.values():
                prop["type"] = next(iter(prop))
            return 200, {}, {"id": source_id, "properties": properties}
        if method == "GET" and path.endswith("/children"):
            values = [{"id": db["id"], "type": "child_database"} for db in self.databases.values()]
            return self._paginate(values, url)
        if method == "POST" and path.endswith("/query"):
            data_source = path.split("/")[2]
            pages = [p for p in self.pages.values() if p.get("parent", {}).get("data_source_id") == data_source
                     and not p.get("archived") and not p.get("in_trash")]
            if self.hide_created:
                pages = [p for p in pages if "Summary Key" not in p.get("properties", {})]
            filt = payload.get("filter")
            if filt:
                expected = filt["rich_text"]["equals"]
                pages = [p for p in pages if ns._plain(p["properties"][filt["property"]]["rich_text"]) == expected]
            start = int(payload.get("start_cursor", "0"))
            selected = pages[start:start + self.page_size]
            cursor = start + len(selected)
            return 200, {}, {"results": selected, "has_more": cursor < len(pages),
                             "next_cursor": str(cursor) if cursor < len(pages) else None}
        if method == "POST" and path == "/pages":
            assert payload["parent"]["type"] == "data_source_id"
            page_id = self.uid()
            page = {"id": page_id, "parent": payload["parent"], "properties": payload["properties"],
                    "icon": payload.get("icon"), "url": "https://app.notion.com/p/" + page_id,
                    "last_edited_time": NOW.isoformat()}
            self.pages[page_id] = page
            return 200, {}, page
        if method == "PATCH" and path.startswith("/pages/"):
            page_id = path.split("/")[-1]
            self.pages[page_id]["properties"].update(payload["properties"])
            return 200, {}, self.pages[page_id]
        if method == "GET" and path.startswith("/pages/"):
            page_id = path.split("/")[-1]
            if page_id not in self.pages:
                return 404, {}, {"message": "Private URL / secret", "code": "object_not_found"}
            return 200, {}, self.pages[page_id]
        raise AssertionError((method, path))

    def _paginate(self, values, url):
        from urllib.parse import parse_qs
        start = int(parse_qs(urlparse(url).query).get("start_cursor", ["0"])[0])
        selected = values[start:start + self.page_size]
        cursor = start + len(selected)
        return 200, {}, {"results": selected, "has_more": cursor < len(values),
                         "next_cursor": str(cursor) if cursor < len(values) else None}

    def summaries(self):
        return [p for p in self.pages.values() if "Summary Key" in p.get("properties", {})]


class NotionTests(unittest.TestCase):
    def test_complete_snapshot_detects_duplicate_even_if_counts_are_unchanged(self):
        self.connect()
        self.ws.sync([self.row()], force=True)
        duplicate = deepcopy(self.api.summaries()[0])
        duplicate["id"] = self.api.uid()
        self.api.pages[duplicate["id"]] = duplicate
        result = self.ws.sync([self.row()], force=True, complete_dataset_ids=[self.ws.config["dataset_id"]])
        self.assertEqual(result["error"]["code"], "duplicate_summary")

    def test_rebinding_reconciles_server_after_local_sync_state_is_lost(self):
        self.connect()
        dataset = self.ws.config["dataset_id"]
        self.ws.sync([self.row()], force=True)
        second = self.ws.create_project("other-task", "新项目", icon=PROJECT_ICON)
        self.ws.bind("task-a", second["page_id"])
        with self.ws._locked():
            self.ws.state["synced"] = {}
        result = self.ws.sync([self.row()], force=True, complete_dataset_ids=[dataset])
        self.assertIsNone(result["error"])
        self.assertEqual(sum(p["properties"]["Counted Tokens"]["number"] for p in self.api.summaries()), 100)

    def test_rebinding_moves_allocation_without_counting_old_project_twice(self):
        first = self.connect()
        second = self.ws.create_project("other-task", "另一个项目", icon=PROJECT_ICON)
        dataset = self.ws.config["dataset_id"]
        scope = {"force": True, "complete_dataset_ids": [dataset], "complete_until": self.time.isoformat()}
        self.ws.sync([self.row()], **scope)
        self.ws.bind("task-a", second["page_id"])
        self.ws.sync([self.row()], **scope)
        pages = self.api.summaries()
        self.assertEqual(len(pages), 2)
        self.assertEqual(sum(p["properties"]["Counted Tokens"]["number"] for p in pages), 100)
        old = next(p for p in pages if p["properties"]["Project"]["relation"][0]["id"] == first["page_id"])
        self.assertEqual(old["properties"]["Total Tokens"]["number"], 100)
        self.assertFalse(old["properties"]["Included"]["checkbox"])
        self.ws.bind("task-a", first["page_id"])
        self.ws.sync([self.row()], **scope)
        self.assertTrue(old["properties"]["Included"]["checkbox"])
        self.assertEqual(sum(p["properties"]["Counted Tokens"]["number"] for p in pages), 100)

    def test_complete_unbinding_keeps_audit_but_excludes_current_allocation(self):
        self.connect()
        dataset = self.ws.config["dataset_id"]
        self.ws.sync([self.row()], force=True, complete_dataset_ids=[dataset])
        self.ws.unbind("task-a")
        self.ws.sync([self.row()], force=True, complete_dataset_ids=[dataset])
        page = self.api.summaries()[0]["properties"]
        self.assertEqual(page["Total Tokens"]["number"], 100)
        self.assertEqual(page["Counted Tokens"]["number"], 0)
        self.assertFalse(page["Included"]["checkbox"])

    def test_delayed_complete_snapshot_cannot_retire_newer_allocation(self):
        self.connect()
        dataset = self.ws.config["dataset_id"]
        self.ws.sync([self.row()], force=True, complete_dataset_ids=[dataset], complete_until=self.time.isoformat())
        self.ws.sync([], force=True, complete_dataset_ids=[dataset], complete_until=(self.time - timedelta(minutes=1)).isoformat())
        page = self.api.summaries()[0]["properties"]
        self.assertTrue(page["Included"]["checkbox"])
        self.assertEqual(page["Counted Tokens"]["number"], 100)

    def test_incremental_batch_does_not_retire_absent_historical_days(self):
        self.connect()
        self.ws.sync([self.row(day="2026-10-12"), self.row()], force=True)
        self.ws.sync([self.row(total_tokens=120)], force=True)
        self.assertEqual(sum(p["properties"]["Counted Tokens"]["number"] for p in self.api.summaries()), 220)

    def test_rebinding_one_of_merged_tasks_preserves_other_task_allocation(self):
        first = self.connect()
        second = self.ws.create_project("other-project", "另一个项目", icon=PROJECT_ICON)
        self.ws.bind("task-b", first["page_id"])
        rows = [self.row(session_keys=["a"]), self.row(task_id="task-b", total_tokens=50, session_keys=["b"])]
        self.ws.sync(rows, force=True, complete_dataset_ids=[self.ws.config["dataset_id"]])
        self.ws.bind("task-a", second["page_id"])
        self.ws.sync(rows, force=True, complete_dataset_ids=[self.ws.config["dataset_id"]])
        self.assertEqual(sorted(p["properties"]["Counted Tokens"]["number"] for p in self.api.summaries()), [50, 100])

    def test_existing_project_select_status_is_detected_before_creating_a_project(self):
        self.ws.setup(PARENT)
        database = self.api.databases[self.ws.config["projects_database_id"]]
        database["properties"]["Status"] = {"select": {"options": [{"name": "计划中"}]}}
        self.ws.setup(PARENT)
        project = self.ws.create_project("select-task", "选择状态项目", icon=PROJECT_ICON)
        self.assertEqual(self.ws.config["status_property_type"], "select")
        self.assertEqual(self.api.pages[project["page_id"]]["properties"]["Status"], {"select": {"name": "计划中"}})

    def test_existing_database_wrong_counter_schema_is_rejected(self):
        self.ws.setup(PARENT)
        database = self.api.databases[self.ws.config["usage_database_id"]]
        database["properties"]["Total Tokens"] = {"rich_text": {}}
        with self.assertRaises(ns.NotionError) as context:
            self.ws.setup(PARENT)
        self.assertEqual(context.exception.code, "schema_mismatch")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.api = FakeNotion()
        self.time = NOW
        self.sleeps = []
        self.token_reads = 0

        def provider():
            self.token_reads += 1
            return "TEST_SECRET_DO_NOT_PRINT"
        self.provider = provider
        self.ws = self.new_workspace()

    def new_workspace(self):
        return ns.NotionWorkspace(self.temp.name, self.provider, self.api,
                                 clock=lambda: self.time, sleeper=self.sleeps.append)

    def connect(self):
        self.ws.setup(PARENT)
        return self.ws.create_project("task-a", "用量小助手", icon=PROJECT_ICON)

    def row(self, **changes):
        result = {"task_id": "task-a", "day": "2026-10-13", "total_tokens": 100,
                  "input_tokens": 60, "output_tokens": 25, "cached_input_tokens": 30,
                  "reasoning_output_tokens": 10, "request_records": 2, "legacy_updates": 0,
                  "sessions_count": 1, "quality": "response_records_verified",
                  "statistics_until": self.time.isoformat()}
        result.update(changes)
        return result

    def test_default_status_is_offline_and_never_reads_keychain(self):
        with patch.object(ns.subprocess, "run", side_effect=AssertionError("Keychain forbidden")):
            local = ns.NotionWorkspace(Path(self.temp.name) / "offline")
            self.assertFalse(local.status()["connected"])
            self.assertEqual(local.projects(), [])
            self.assertIsNone(local.project_for_task("x"))
        self.assertEqual(self.token_reads, 0)
        self.assertEqual(self.api.calls, [])

    def test_three_database_setup_uses_current_schema_and_shanghai_week(self):
        self.ws.setup(PARENT)
        self.assertEqual(self.ws.status()["sync_since"], "2026-10-12")
        self.assertEqual(len(self.api.databases), 3)
        schemas = [d["properties"] for d in self.api.databases.values()]
        project = next(s for s in schemas if "Status" in s)
        self.assertEqual([o["name"] for o in project["Status"]["status"]["options"]],
                         ["计划中", "开发中", "已发布", "暂停", "归档"])
        daily = next(s for s in schemas if "Summary Key" in s)
        self.assertEqual(daily["Project"]["relation"]["data_source_id"], self.ws.config["projects_data_source_id"])
        self.assertNotIn("database_id", daily["Project"]["relation"])
        self.assertEqual(self.ws.status()["state"], "ready")

    def test_setup_twice_and_restart_do_not_create_more_databases(self):
        self.ws.setup(PARENT)
        dataset = self.ws.status()["dataset_id"]
        self.new_workspace().setup(PARENT)
        self.assertEqual(len(self.api.databases), 3)
        self.assertEqual(self.new_workspace().status()["dataset_id"], dataset)

    def test_status_and_metadata_reads_do_not_query_credentials_or_network(self):
        self.connect()
        calls, reads = len(self.api.calls), self.token_reads
        for _ in range(3):
            self.new_workspace().status()
            self.ws.project_for_task("task-a")
        self.assertEqual(len(self.api.calls), calls)
        self.assertEqual(self.token_reads, reads)

    def test_unknown_projects_are_not_automatically_created(self):
        self.ws.setup(PARENT)
        result = self.ws.sync([self.row()], force=True)
        self.assertEqual(result["changed"], 0)
        self.assertEqual(self.api.summaries(), [])

    def test_same_daily_snapshot_is_idempotent_and_no_subtotals_double_count(self):
        self.connect()
        self.assertEqual(self.ws.sync([self.row()], force=True)["changed"], 1)
        self.time += timedelta(minutes=6)
        self.assertEqual(self.new_workspace().sync([self.row()], force=True)["changed"], 0)
        pages = self.api.summaries()
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0]["properties"]["Total Tokens"]["number"], 100)
        self.assertNotEqual(60 + 25 + 30 + 10, 100)

    def test_revised_counts_replace_previous_summary_instead_of_adding(self):
        self.connect()
        self.ws.sync([self.row()], force=True)
        self.time += timedelta(minutes=6)
        self.ws.sync([self.row(total_tokens=80, legacy_updates=1, quality="legacy_estimated")], force=True)
        self.assertEqual(len(self.api.summaries()), 1)
        summary = self.api.summaries()[0]["properties"]
        self.assertEqual(summary["Total Tokens"]["number"], 80)
        self.assertEqual(ns._plain(summary["Quality"]["rich_text"]), "含旧累计估算")

    def test_older_scan_cannot_roll_back_later_counts(self):
        self.connect()
        self.ws.sync([self.row(total_tokens=120)], force=True)
        self.ws.sync([self.row(total_tokens=30, statistics_until=(NOW - timedelta(minutes=1)).isoformat())], force=True)
        self.assertEqual(self.api.summaries()[0]["properties"]["Total Tokens"]["number"], 120)

    def test_pre_week_rows_are_not_uploaded(self):
        self.connect()
        self.ws.sync([self.row(day="2026-10-11")], force=True)
        self.assertEqual(self.api.summaries(), [])

    def test_multiple_task_bindings_merge_daily_usage_and_union_sessions(self):
        project = self.connect()
        self.ws.bind("task-b", project["page_id"])
        a = self.row(session_keys=["HASH_A", "HASH_SHARED"], sessions_count=2)
        b = self.row(task_id="task-b", total_tokens=50, session_keys=["HASH_B", "HASH_SHARED"], sessions_count=2)
        self.ws.sync([a, b], force=True)
        properties = self.api.summaries()[0]["properties"]
        self.assertEqual(properties["Total Tokens"]["number"], 150)
        self.assertEqual(properties["Sessions"]["number"], 3)
        disk = self.ws.state_path.read_text()
        requests = json.dumps(self.api.calls)
        self.assertNotIn("HASH_SHARED", disk + requests)

    def test_multiple_tasks_without_session_evidence_do_not_sum_sessions(self):
        project = self.connect()
        self.ws.bind("task-b", project["page_id"])
        self.ws.sync([self.row(), self.row(task_id="task-b")], force=True)
        properties = self.api.summaries()[0]["properties"]
        self.assertIsNone(properties["Sessions"]["number"])
        self.assertIn("会话数未跨任务去重", ns._plain(properties["Quality"]["rich_text"]))

    def test_datasets_do_not_overwrite_one_another(self):
        self.connect()
        self.ws.sync([self.row(dataset_id=str(uuid.UUID(int=300)))], force=True)
        self.ws.sync([self.row(dataset_id=str(uuid.UUID(int=301)), total_tokens=40)], force=True)
        self.assertEqual(len(self.api.summaries()), 2)

    def test_rename_and_status_changes_preserve_binding_and_counts(self):
        project = self.connect()
        self.ws.sync([self.row()], force=True)
        page = self.api.pages[project["page_id"]]
        page["properties"]["Name"] = {"title": ns._text("一个非常长的改名项目")}
        page["properties"]["Status"] = {"status": {"name": "归档"}}
        page["icon"] = {"type": "emoji", "emoji": "📚"}
        meta = self.ws.projects(force=True)[0]
        self.assertEqual(meta["display_name"], "一个非常长的改名项目")
        self.assertEqual(meta["status"], "归档")
        self.assertEqual(meta["icon_value"], "📚")
        self.assertFalse(meta["stale"])
        self.assertEqual(self.ws.project_for_task("task-a")["page_id"], project["page_id"])
        self.assertEqual(self.api.summaries()[0]["properties"]["Total Tokens"]["number"], 100)

    def test_project_query_paginates(self):
        self.ws.setup(PARENT)
        for n in range(5):
            self.ws.create_project(f"task-{n}", f"项目{n}", icon=PROJECT_ICON)
        self.api.page_size = 2
        self.assertEqual(len(self.ws.projects(force=True)), 5)
        query_calls = [call for call in self.api.calls if call[2] and call[2].get("start_cursor")]
        self.assertTrue(query_calls)

    def test_network_failure_keeps_outbox_and_previous_metadata(self):
        self.connect()
        self.api.schedule(lambda m, p, b: p.endswith("/query"), TimeoutError("TEST_SECRET_DO_NOT_PRINT"))
        self.api.schedule(lambda m, p, b: p.endswith("/query"), TimeoutError("PRIVATE URL"))
        self.api.schedule(lambda m, p, b: p.endswith("/query"), TimeoutError("PRIVATE URL"))
        self.api.schedule(lambda m, p, b: p.startswith("/pages/"), (403, {}, {"message": "private content"}))
        result = self.ws.sync([self.row()], force=True)
        self.assertEqual(result["pending_count"], 1)
        self.assertTrue(self.ws.project_for_task("task-a")["stale"])
        self.assertNotIn("TEST_SECRET", json.dumps(result))
        self.time += timedelta(minutes=6)
        self.assertEqual(self.new_workspace().sync([self.row()], force=True)["changed"], 1)

    def test_401_is_not_retried_and_error_never_echoes_server_message(self):
        self.api.schedule(lambda m, p, b: True,
                          (401, {}, {"message": "Token TEST_SECRET_DO_NOT_PRINT at privateURL"}))
        with self.assertRaises(ns.NotionError) as result:
            self.ws.setup(PARENT)
        self.assertEqual(len(self.api.calls), 1)
        self.assertEqual(result.exception.code, "unauthorized")
        self.assertEqual(self.sleeps, [])
        self.assertNotIn("TEST_SECRET", self.ws.state_path.read_text())
        self.assertFalse(self.ws.status()["credential_available"])

    def test_403_is_not_retried(self):
        self.api.schedule(lambda m, p, b: True, (403, {}, {}))
        with self.assertRaises(ns.NotionError):
            self.ws.setup(PARENT)
        self.assertEqual(len(self.api.calls), 1)

    def test_retry_after_429_and_529_is_respected_with_bounded_attempts(self):
        self.api.schedule(lambda m, p, b: True, (429, {"Retry-After": "4"}, {}))
        self.api.schedule(lambda m, p, b: True, (529, {"retry-after": "6"}, {}))
        self.ws.setup(PARENT)
        self.assertGreaterEqual(self.sleeps[0], 4)
        self.assertGreaterEqual(self.sleeps[1], 6)
        self.assertLess(self.sleeps[1], 6.2)

    def test_long_retry_after_defers_without_early_retry_even_force(self):
        self.api.schedule(lambda m, p, b: True, (429, {"Retry-After": "120"}, {}))
        with self.assertRaises(ns.NotionError):
            self.ws.setup(PARENT)
        calls = len(self.api.calls)
        with self.assertRaises(ns.NotionError):
            self.ws.setup(PARENT)
        self.assertEqual(len(self.api.calls), calls)
        self.assertEqual(self.sleeps, [])
        self.time += timedelta(seconds=121)
        self.ws.setup(PARENT)

    def test_blocked_429_never_retries(self):
        self.api.schedule(lambda m, p, b: True,
                          (429, {"Retry-After": "1"}, {"additional_data": {"rate_limit_reason": "public_api_request_blocked"}}))
        with self.assertRaises(ns.NotionError) as result:
            self.ws.setup(PARENT)
        self.assertEqual(result.exception.code, "api_access_blocked")
        self.assertEqual(len(self.api.calls), 1)

    def test_blocked_workspace_stays_paused_across_background_rounds(self):
        self.connect()
        self.api.schedule(lambda m, p, b: p.endswith("/query"),
                          (429, {}, {"additional_data": {"rate_limit_reason": "public_api_request_blocked"}}))
        result = self.ws.sync([self.row()], force=True)
        self.assertTrue(result["automatic_sync_paused"])
        self.assertEqual(result["changed"], 0)
        calls, reads = len(self.api.calls), self.token_reads
        self.time += timedelta(seconds=600)
        self.ws.projects(force=False)
        self.ws.sync([self.row()], force=False)
        self.assertEqual(len(self.api.calls), calls)
        self.assertEqual(self.token_reads, reads)
        self.assertEqual(self.ws.sync([self.row()], force=True)["changed"], 1)
        self.assertFalse(self.ws.status()["automatic_sync_paused"])

    def test_precise_cutoff_is_retained_as_iso_text(self):
        self.connect()
        self.time = datetime(2026, 10, 13, 9, tzinfo=timezone.utc)
        row = self.row(statistics_until="2026-10-13T08:00:23.123456+00:00")
        self.ws.sync([row], force=True)
        properties = self.api.summaries()[0]["properties"]
        self.assertEqual(ns._plain(properties["Statistics Until ISO"]["rich_text"]), row["statistics_until"])

    def test_create_timeout_after_commit_requeries_without_duplicate(self):
        self.connect()
        self.api.schedule(lambda m, p, b: m == "POST" and p == "/pages",
                          TimeoutError("secret"), after=True)
        result = self.ws.sync([self.row()], force=True)
        self.assertEqual(result["changed"], 1)
        self.assertEqual(len(self.api.summaries()), 1)
        creates = [c for c in self.api.calls if c[0] == "POST" and c[1].endswith("/pages") and "Summary Key" in c[2]["properties"]]
        self.assertEqual(len(creates), 1)

    def test_unconfirmed_create_is_frozen_across_restart_until_read_recovers(self):
        self.connect()
        self.api.hide_created = True
        self.api.schedule(lambda m, p, b: m == "POST" and p == "/pages", TimeoutError("secret"), after=True)
        result = self.ws.sync([self.row()], force=True)
        self.assertEqual(result["error"]["code"], "write_uncertain")
        self.assertEqual(len(self.api.summaries()), 1)
        self.new_workspace().sync([self.row(total_tokens=140)], force=True)
        self.assertEqual(len(self.api.summaries()), 1)
        self.api.hide_created = False
        result = self.new_workspace().sync([self.row(total_tokens=140)], force=True)
        self.assertEqual(result["changed"], 1)
        self.assertEqual(len(self.api.summaries()), 1)
        self.assertEqual(self.api.summaries()[0]["properties"]["Total Tokens"]["number"], 140)

    def test_503_committed_resource_id_is_read_instead_of_recreated(self):
        self.connect()
        committed = str(uuid.UUID(int=self.api.sequence + 1))
        self.api.schedule(lambda m, p, b: m == "POST" and p == "/pages", (503, {}, {
            "additional_data": {"committed_resource_id": committed, "retry_guidance": ["Do not repeat write."]}}), after=True)
        result = self.ws.sync([self.row()], force=True)
        self.assertEqual(result["changed"], 1)
        self.assertEqual(len(self.api.summaries()), 1)

    def test_database_create_timeout_can_resume_without_more_databases(self):
        self.api.schedule(lambda m, p, b: p == "/databases", TimeoutError("secret"), after=True)
        self.ws.setup(PARENT)
        self.new_workspace().setup(PARENT)
        self.assertEqual(len(self.api.databases), 3)

    def test_project_create_timeout_recovers_stable_binding(self):
        self.ws.setup(PARENT)
        self.api.schedule(lambda m, p, b: p == "/pages", TimeoutError("secret"), after=True)
        meta = self.ws.create_project("task-a", "A", icon=PROJECT_ICON)
        self.assertEqual(self.ws.status()["bindings"]["task-a"], meta["page_id"])
        self.assertEqual(self.new_workspace().create_project("task-a", "Renamed", icon=PROJECT_ICON)["page_id"], meta["page_id"])

    def test_duplicate_remote_key_stops_instead_of_overwriting_arbitrary_page(self):
        self.connect()
        self.ws.sync([self.row()], force=True)
        page = deepcopy(self.api.summaries()[0])
        page["id"] = self.api.uid()
        self.api.pages[page["id"]] = page
        result = self.ws.sync([self.row(total_tokens=200)], force=True)
        self.assertEqual(result["error"]["code"], "duplicate_summary")
        self.assertEqual([p["properties"]["Total Tokens"]["number"] for p in self.api.summaries()], [100, 100])

    def test_unbinding_removes_pending_write_but_leaves_cloud_history(self):
        self.connect()
        self.ws.sync([self.row()], force=True)
        self.ws.unbind("task-a")
        self.ws.sync([self.row(total_tokens=200)], force=True)
        self.assertEqual(self.ws.status()["pending_count"], 0)
        self.assertEqual(self.api.summaries()[0]["properties"]["Total Tokens"]["number"], 100)

    def test_archived_notion_page_blocks_writes_without_resetting_cloud_totals(self):
        project = self.connect()
        self.ws.sync([self.row()], force=True)
        self.api.pages[project["page_id"]]["in_trash"] = True
        self.ws.projects(force=True)
        result = self.ws.sync([self.row(total_tokens=200)], force=True)
        self.assertEqual(result["error"]["code"], "object_not_found")
        self.assertEqual(self.api.summaries()[0]["properties"]["Total Tokens"]["number"], 100)

    def test_observed_history_is_explicitly_not_current_recomputable_records(self):
        self.connect()
        self.ws.sync([self.row(observed_history=True)], force=True)
        scope = ns._plain(self.api.summaries()[0]["properties"]["Data Scope"]["rich_text"])
        self.assertIn("部分历史本次未重新复算", scope)
        self.assertIn("不是当前保留记录的全量统计", scope)

    def test_payload_whitelist_drops_paths_raw_ids_conversations_and_custom_quality(self):
        self.connect()
        self.ws.sync([self.row(thread_ids=["SECRET_THREAD"], project="/Users/private/path",
                              conversation="PRIVATE CONVERSATION", quality="PRIVATE CODE")], force=True)
        payloads = json.dumps(self.api.calls)
        for private in ("SECRET_THREAD", "/Users/private/path", "PRIVATE CONVERSATION", "PRIVATE CODE"):
            self.assertNotIn(private, payloads)
        self.assertNotIn("TEST_SECRET", self.ws.config_path.read_text() + self.ws.state_path.read_text())
        self.assertEqual(self.ws.config_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.ws.state_path.stat().st_mode & 0o777, 0o600)

    def test_invalid_counts_future_times_duplicate_rows_and_wrong_provider_rejected(self):
        self.connect()
        bad_rows = [[self.row(total_tokens=-1)], [self.row(total_tokens=True)],
                    [self.row(total_tokens=2 ** 53 + 1)], [self.row(day="2026-10-14")],
                    [self.row(statistics_until="not-a-date")], [self.row(), self.row()],
                    [self.row(provider="Claude")]]
        for rows in bad_rows:
            with self.subTest(rows=rows):
                result = self.ws.sync(rows, force=True)
                self.assertEqual(result["error"]["code"], "invalid_input")
        self.assertEqual(self.api.summaries(), [])

    def test_url_id_parser_rejects_non_notion_hosts_and_accepts_page_urls(self):
        self.assertEqual(ns.notion_id("https://www.notion.so/My-Project-" + PARENT.replace("-", "") + "?pvs=4"), PARENT)
        self.assertEqual(ns.notion_id("https://app.notion.com/p/" + PARENT), PARENT)
        with self.assertRaises(ns.NotionError):
            ns.notion_id("https://attacker.test/" + PARENT)

    def test_cli_rejects_token_input_without_printing_it(self):
        inp = io.StringIO(json.dumps({"command": "status", "token": "VERY_PRIVATE"}))
        out = io.StringIO()
        with patch.object(sys, "stdin", inp), patch.object(sys, "stdout", out):
            code = ns.main(["--data-dir", self.temp.name])
        self.assertEqual(code, 1)
        self.assertNotIn("VERY_PRIVATE", out.getvalue())

    def test_current_task_and_removed_task_history_merge_with_original_cutoffs(self):
        project = self.connect()
        self.ws.bind("task-b", project["page_id"])
        earlier = (NOW - timedelta(hours=2)).isoformat()
        old = self.row(task_id="task-b", total_tokens=70, observed_history=True,
                       statistics_until=earlier, session_keys=["shared"])
        current = self.row(total_tokens=30, session_keys=["shared", "new"])
        result = self.ws.sync([old, current], force=True)
        self.assertIsNone(result["error"])
        summary = self.api.summaries()[0]["properties"]
        self.assertEqual(summary["Total Tokens"]["number"], 100)
        self.assertEqual(summary["Sessions"]["number"], 2)
        self.assertEqual(summary["Statistics Until"]["date"]["start"], NOW.isoformat())
        self.assertIn("部分历史本次未重新复算", ns._plain(summary["Data Scope"]["rich_text"]))
        self.assertEqual(old["statistics_until"], earlier)

    def test_current_recomputable_tasks_must_have_same_cutoff_before_merge(self):
        project = self.connect()
        self.ws.bind("task-b", project["page_id"])
        result = self.ws.sync([self.row(), self.row(task_id="task-b",
                              statistics_until=(NOW - timedelta(hours=1)).isoformat())], force=True)
        self.assertEqual(result["error"]["code"], "invalid_input")
        self.assertEqual(self.api.summaries(), [])

    def test_cache_write_subtotal_is_stored_without_adding_to_total(self):
        self.connect()
        self.ws.sync([self.row(cache_write_input_tokens=200)], force=True)
        summary = self.api.summaries()[0]["properties"]
        self.assertEqual(summary["Cache Write Input Tokens"]["number"], 200)
        self.assertEqual(summary["Total Tokens"]["number"], 100)

    def test_wrong_database_binding_is_rejected(self):
        self.connect()
        self.ws.sync([self.row()], force=True)
        with self.assertRaises(ns.NotionError) as caught:
            self.ws.bind("task-b", self.api.summaries()[0]["id"])
        self.assertEqual(caught.exception.code, "schema_mismatch")
        self.assertNotIn("task-b", self.ws.status()["bindings"])

    def test_corrupted_state_is_not_silently_replaced(self):
        self.connect()
        self.ws.state_path.write_text("NOT VALID JSON")
        with self.assertRaises(ns.NotionError):
            self.ws.bind("task-b", self.ws.config["bindings"]["task-a"])
        self.assertEqual(self.ws.state_path.read_text(), "NOT VALID JSON")

    def test_retry_limit_keeps_pending_work_instead_of_looping_forever(self):
        self.connect()
        for _ in range(3):
            self.api.schedule(lambda m, p, b: m == "POST" and p == "/pages", (529, {"Retry-After": "1"}, {}))
        result = self.ws.sync([self.row()], force=True)
        self.assertEqual(result["error"]["code"], "service_overload")
        self.assertEqual(result["pending_count"], 1)
        self.assertEqual(self.api.summaries(), [])
        creates = [c for c in self.api.calls if c[0] == "POST" and c[1].endswith("/pages") and "Summary Key" in c[2]["properties"]]
        self.assertEqual(len(creates), 3)

    def test_expired_metadata_and_invalid_icon_are_marked_without_network(self):
        project = self.connect()
        self.api.pages[project["page_id"]]["icon"] = {"type": "external", "external": {"url": "http://unsafe.test/icon.png"}}
        fresh = self.ws.projects(force=True)[0]
        self.assertIsNone(fresh["icon_value"])
        self.time += timedelta(minutes=11)
        calls = len(self.api.calls)
        self.assertTrue(self.ws.project_for_task("task-a")["stale"])
        self.assertEqual(len(self.api.calls), calls)

    def task_source(self):
        if self.ws.config.get("tasks_data_source_id"):
            return self.ws.config["tasks_data_source_id"]
        database, source = self.api.uid(), self.api.uid()
        self.api.databases[database] = {"id": database, "data_sources": [{"id": source}], "properties": {
            "Project": {"relation": {"data_source_id": self.ws.config["projects_data_source_id"]}},
            "Status": {"select": {"options": [{"name": value} for value in ns.TASK_KNOWN_STATUSES]}}}}
        with self.ws._locked():
            self.ws.config["tasks_data_source_id"] = source
        return source

    def add_task(self, source, project_id, status, **changes):
        page_id = self.api.uid()
        page = {"id": page_id, "parent": {"data_source_id": source}, "properties": {
            "Project": {"type": "relation", "relation": [{"id": project_id}]},
            "Status": {"type": "select", "select": {"name": status}}}}
        page.update(changes)
        self.api.pages[page_id] = page
        return page

    def test_project_management_fields_preserve_rich_text_select_and_dates(self):
        project = self.connect()
        props = self.api.pages[project["page_id"]]["properties"]
        props.update({"Goal": {"rich_text": [{"plain_text": "项目"}, {"text": {"content": "目标"}}]},
            "Weekly Goal": {"rich_text": ns._text("完成第一版")}, "Next Step": {"rich_text": ns._text("验证同步")},
            "Priority": {"select": {"name": "高"}}, "Health": {"select": {"name": "受阻"}},
            "Target Date": {"date": {"start": "2026-10-20"}}, "Last Reviewed": {"date": {"start": "2026-10-13T08:00:00Z"}},
            "Summary": {"rich_text": ns._text("已完成本地实现")}})
        meta = self.ws.projects(force=True)[0]
        expected = {"project_goal": "项目目标", "weekly_goal": "完成第一版", "next_step": "验证同步",
            "project_priority": "高", "project_health": "受阻", "target_date": "2026-10-20",
            "last_reviewed": "2026-10-13T08:00:00Z", "project_summary": "已完成本地实现"}
        self.assertEqual({key: meta[key] for key in expected}, expected)
        self.assertEqual(self.ws.project_for_task("task-a")["project_goal"], "项目目标")
        self.assertFalse(meta["icon_missing"])

    def test_absent_management_fields_and_tasks_remain_unknown_not_fake_zero(self):
        self.connect()
        with self.ws._locked():
            self.ws.config.pop("tasks_data_source_id", None)
        meta = self.ws.projects(force=True)[0]
        self.assertTrue(all(meta[field] is None for field in ns.PROJECT_TEXT_PROPERTIES))
        self.assertTrue(all(meta[field] is None for field in ns.PROJECT_SELECT_PROPERTIES))
        self.assertTrue(all(meta[field] is None for field in ns.PROJECT_DATE_PROPERTIES))
        self.assertTrue(all(meta[field] is None for field in ns.TASK_COUNT_FIELDS))

    def test_existing_project_schema_without_additive_fields_needs_no_cloud_migration(self):
        self.ws.setup(PARENT)
        properties = self.api.databases[self.ws.config["projects_database_id"]]["properties"]
        for name in ns.OPTIONAL_PROJECT_PROPERTIES:
            properties.pop(name, None)
        calls = len(self.api.calls)
        self.ws.setup(PARENT)
        self.assertTrue(self.ws.status()["ready"])
        self.assertFalse(any(method == "PATCH" for method, _, _ in self.api.calls[calls:]))
        self.assertEqual(len(self.api.databases), 3)

    def test_create_requires_valid_icon_before_credentials_or_requests(self):
        self.ws.setup(PARENT)
        calls, reads = len(self.api.calls), self.token_reads
        cases = [None, {}, {"type": "emoji", "emoji": ""}, {"type": "emoji", "emoji": "hello"},
                 {"type": "emoji", "emoji": "📦📦"}, {"type": "external", "external": {"url": "http://example.com/a.png"}},
                 {"type": "external", "external": {"url": "https://user:secret@example.com/a.png"}},
                 {"type": "external", "external": {"url": "https://example.com:bad/a.png"}},
                 {"type": "external", "external": {"url": "https://example.com/a\n.png"}}]
        for icon in cases:
            with self.subTest(icon=icon), self.assertRaises(ns.NotionError) as error:
                self.ws.create_project("new-task", "新项目", icon=icon)
            self.assertEqual(error.exception.code, "icon_required")
        self.assertEqual(len(self.api.calls), calls)
        self.assertEqual(self.token_reads, reads)
        self.assertEqual(self.ws.config["bindings"], {})

    def test_project_icon_is_atomic_with_creation_and_not_a_followup_patch(self):
        self.ws.setup(PARENT)
        for task, icon in [("emoji", {"type": "emoji", "emoji": "🧑‍💻"}),
                           ("logo", {"type": "external", "external": {"url": "https://example.com/logo.png"}})]:
            meta = self.ws.create_project(task, "项目", icon=icon)
            self.assertEqual(self.api.pages[meta["page_id"]]["icon"], icon)
            creates = [body for method, url, body in self.api.calls if method == "POST" and url.endswith("/pages")]
            self.assertEqual(creates[-1]["icon"], icon)
            self.assertFalse(meta["icon_missing"])
        self.assertFalse(any(method == "PATCH" for method, _, _ in self.api.calls))

    def test_iconless_bind_refuses_without_mutating_cloud_or_local_binding(self):
        project = self.connect()
        page = self.api.pages[project["page_id"]]
        page["icon"] = None
        remote = deepcopy(self.api.pages)
        bindings = deepcopy(self.ws.config["bindings"])
        calls = len(self.api.calls)
        with self.assertRaises(ns.NotionError) as error:
            self.ws.bind("task-b", project["page_id"])
        self.assertEqual(error.exception.code, "icon_required")
        self.assertEqual(self.ws.config["bindings"], bindings)
        self.assertEqual(self.api.pages, remote)
        self.assertTrue(all(method == "GET" for method, _, _ in self.api.calls[calls:]))
        self.assertTrue(self.ws.projects(force=True)[0]["icon_missing"])

    def test_optional_tasks_counts_use_project_relations_and_statuses(self):
        project = self.connect()
        source = self.task_source()
        for status in ["待处理", "进行中", "待确认", "阻塞", "已完成", "已完成", "取消"]:
            self.add_task(source, project["page_id"], status)
        self.add_task(source, project["page_id"], "阻塞", archived=True)
        self.add_task(source, str(uuid.UUID(int=900)), "待处理")
        self.api.page_size = 2
        calls = len(self.api.calls)
        meta = self.ws.projects(force=True)[0]
        self.assertEqual(tuple(meta[field] for field in ns.TASK_COUNT_FIELDS), (4, 1, 2))
        self.assertTrue(all(not (method == "PATCH" or (method == "POST" and url.endswith("/pages")))
                            for method, url, _ in self.api.calls[calls:]))
        self.assertEqual(self.ws.project_for_task("task-a")["open_tasks"], 4)

    def test_successful_empty_tasks_source_is_the_only_way_to_assert_zero_counts(self):
        self.connect()
        self.task_source()
        meta = self.ws.projects(force=True)[0]
        self.assertEqual(tuple(meta[field] for field in ns.TASK_COUNT_FIELDS), (0, 0, 0))

    def test_tasks_failure_keeps_project_metadata_but_clears_counts_and_explains(self):
        project = self.connect()
        source = self.task_source()
        self.add_task(source, project["page_id"], "阻塞")
        self.assertEqual(self.ws.projects(force=True)[0]["open_tasks"], 1)
        for _ in range(3):
            self.api.schedule(lambda method, path, body: path == f"/data_sources/{source}/query", TimeoutError("private"))
        meta = self.ws.projects(force=True)[0]
        self.assertFalse(meta["stale"])
        self.assertEqual(meta["display_name"], "用量小助手")
        self.assertTrue(all(meta[field] is None for field in ns.TASK_COUNT_FIELDS))
        self.assertEqual(self.ws.status()["tasks_error"]["code"], "network_error")
        self.assertIsNone(self.ws.project_for_task("task-a")["open_tasks"])
        self.assertEqual(self.ws.projects(force=True)[0]["open_tasks"], 1)
        self.assertIsNone(self.ws.status()["tasks_error"])

    def test_unknown_or_truncated_task_shape_never_claims_complete_counts(self):
        project = self.connect()
        source = self.task_source()
        page = self.add_task(source, project["page_id"], "不认识的状态")
        meta = self.ws.projects(force=True)[0]
        self.assertIsNone(meta["open_tasks"])
        self.assertEqual(self.ws.status()["tasks_error"]["code"], "schema_mismatch")
        page["properties"]["Status"]["select"]["name"] = "进行中"
        page["properties"]["Project"]["has_more"] = True
        self.assertIsNone(self.ws.projects(force=True)[0]["open_tasks"])

    def test_management_cache_is_read_only_and_expiration_clears_task_counts(self):
        project = self.connect()
        source = self.task_source()
        self.add_task(source, project["page_id"], "进行中")
        self.ws.projects(force=True)
        calls, reads = len(self.api.calls), self.token_reads
        fresh = self.ws.cached_projects()[0]
        self.assertEqual(fresh["open_tasks"], 1)
        self.time += timedelta(minutes=11)
        stale = self.ws.cached_projects()[0]
        self.assertTrue(stale["stale"])
        self.assertIsNone(stale["open_tasks"])
        self.assertEqual(len(self.api.calls), calls)
        self.assertEqual(self.token_reads, reads)

    def test_empty_wrong_tasks_source_schema_cannot_report_zero(self):
        self.connect()
        source = self.task_source()
        database = next(db for db in self.api.databases.values() if db["data_sources"][0]["id"] == source)
        database["properties"]["Project"]["relation"]["data_source_id"] = str(uuid.UUID(int=999))
        meta = self.ws.projects(force=True)[0]
        self.assertTrue(all(meta[field] is None for field in ns.TASK_COUNT_FIELDS))
        self.assertEqual(self.ws.status()["tasks_error"]["code"], "schema_mismatch")

    def test_task_rate_limit_defers_subsequent_requests_without_marking_false_zero(self):
        self.connect()
        source = self.task_source()
        self.api.schedule(lambda method, path, body: path == f"/data_sources/{source}/query",
                          (429, {"Retry-After": "120"}, {}))
        meta = self.ws.projects(force=True)[0]
        self.assertIsNone(meta["open_tasks"])
        self.assertEqual(self.ws.status()["tasks_error"]["code"], "rate_limited")
        calls = len(self.api.calls)
        self.ws.projects(force=True)
        self.assertEqual(len(self.api.calls), calls)
        self.assertEqual(self.sleeps, [])

    def test_blocked_task_request_pauses_background_without_cloud_writes(self):
        self.connect()
        source = self.task_source()
        self.api.schedule(lambda method, path, body: path == f"/data_sources/{source}/query",
                          (429, {}, {"additional_data": {"rate_limit_reason": "public_api_request_blocked"}}))
        meta = self.ws.projects(force=True)[0]
        self.assertIsNone(meta["open_tasks"])
        self.assertTrue(self.ws.status()["automatic_sync_paused"])
        calls = len(self.api.calls)
        self.time += timedelta(minutes=6)
        self.ws.projects()
        self.assertEqual(len(self.api.calls), calls)

    def test_invalid_rich_text_parts_are_ignored_without_breaking_project_metadata(self):
        project = self.connect()
        self.api.pages[project["page_id"]]["properties"]["Goal"] = {"rich_text": [
            {"plain_text": None, "text": {"content": "有效目标"}}, {"plain_text": 12}, {"text": "invalid"}, None]}
        self.assertEqual(self.ws.projects(force=True)[0]["project_goal"], "有效目标")

    def test_fresh_bootstrap_builds_management_tasks_schema_and_reports_urls(self):
        self.ws.setup(PARENT)
        tasks = self.api.databases[self.ws.config["tasks_database_id"]]["properties"]
        self.assertEqual(set(tasks), {"Name", "Project", "Status", "Priority", "Managed By", "Due", "Evidence", "Acceptance"})
        self.assertEqual([value["name"] for value in tasks["Status"]["select"]["options"]],
                         ["待处理", "进行中", "待确认", "阻塞", "已完成", "取消"])
        self.assertEqual(tasks["Project"]["relation"]["data_source_id"], self.ws.config["projects_data_source_id"])
        self.assertEqual([value["name"] for value in tasks["Priority"]["select"]["options"]], ["高", "中", "低"])
        self.assertEqual([value["name"] for value in tasks["Managed By"]["select"]["options"]], ["AI", "人类确认"])
        self.assertTrue(self.ws.status()["tasks_url"].startswith("https://"))
        project = self.api.databases[self.ws.config["projects_database_id"]]["properties"]
        self.assertTrue(ns.OPTIONAL_PROJECT_PROPERTIES <= project.keys())

    def test_tasks_create_timeout_recovers_without_a_fourth_database(self):
        self.api.schedule(lambda method, path, body: method == "POST" and path == "/databases"
                          and ns._plain(body["title"]) == "任务 Tasks", TimeoutError("private"), after=True)
        self.ws.setup(PARENT)
        source = self.ws.config["tasks_data_source_id"]
        self.new_workspace().setup(PARENT)
        self.assertEqual(len(self.api.databases), 3)
        self.assertEqual(self.ws.config["tasks_data_source_id"], source)
        creates = [body for method, url, body in self.api.calls if method == "POST" and url.endswith("/databases")
                   and ns._plain(body["title"]) == "任务 Tasks"]
        self.assertEqual(len(creates), 1)

    def test_manually_selected_legacy_tasks_source_is_preserved_by_setup(self):
        self.ws.setup(PARENT)
        original = self.ws.config["tasks_data_source_id"]
        with self.ws._locked():
            self.ws.config.pop("tasks_database_id")
        calls = len(self.api.calls)
        self.ws.setup(PARENT)
        self.assertEqual(self.ws.config["tasks_data_source_id"], original)
        self.assertEqual(len(self.api.databases), 3)
        self.assertFalse(any(method == "POST" and url.endswith("/databases") for method, url, _ in self.api.calls[calls:]))

    def test_legacy_tasks_schema_extra_fields_optional_and_relation_checked(self):
        self.ws.setup(PARENT)
        database = self.api.databases[self.ws.config["tasks_database_id"]]
        for name in ns.OPTIONAL_TASK_PROPERTIES:
            database["properties"].pop(name, None)
        calls = len(self.api.calls)
        self.ws.setup(PARENT)
        self.assertTrue(self.ws.status()["ready"])
        self.assertFalse(any(method == "PATCH" for method, _, _ in self.api.calls[calls:]))
        database["properties"]["Project"]["relation"]["data_source_id"] = str(uuid.UUID(int=999))
        with self.assertRaises(ns.NotionError) as error:
            self.ws.setup(PARENT)
        self.assertEqual(error.exception.code, "schema_mismatch")

    def test_old_two_database_workspace_adds_only_tasks_and_preserves_old_ids(self):
        self.ws.setup(PARENT)
        project_id = self.ws.config["projects_database_id"]
        usage_id = self.ws.config["usage_database_id"]
        task_id = self.ws.config["tasks_database_id"]
        self.api.databases.pop(task_id)
        with self.ws._locked():
            for key in ("tasks_database_id", "tasks_data_source_id", "tasks_url"):
                self.ws.config.pop(key, None)
        for name in ns.OPTIONAL_PROJECT_PROPERTIES:
            self.api.databases[project_id]["properties"].pop(name, None)
        calls = len(self.api.calls)
        self.ws.setup(PARENT)
        self.assertEqual((self.ws.config["projects_database_id"], self.ws.config["usage_database_id"]), (project_id, usage_id))
        self.assertEqual(len(self.api.databases), 3)
        creates = [body for method, url, body in self.api.calls[calls:] if method == "POST" and url.endswith("/databases")]
        self.assertEqual([ns._plain(body["title"]) for body in creates], ["任务 Tasks"])
        self.assertFalse(any(method == "PATCH" for method, _, _ in self.api.calls[calls:]))

    def test_uncertain_tasks_create_without_visible_commit_stays_frozen_on_restart(self):
        self.api.schedule(lambda method, path, body: method == "POST" and path == "/databases"
                          and ns._plain(body["title"]) == "任务 Tasks", TimeoutError("private"))
        with self.assertRaises(ns.NotionError) as error:
            self.ws.setup(PARENT)
        self.assertEqual(error.exception.code, "write_uncertain")
        with self.assertRaises(ns.NotionError) as retry:
            self.new_workspace().setup(PARENT)
        self.assertEqual(retry.exception.code, "write_uncertain")
        creates = [body for method, url, body in self.api.calls if method == "POST" and url.endswith("/databases")
                   and ns._plain(body["title"]) == "任务 Tasks"]
        self.assertEqual(len(creates), 1)

    def test_manual_progress_priority_and_tasks_fallback_have_explicit_source(self):
        project = self.connect(); source = self.task_source()
        for _ in range(6): self.add_task(source, project["page_id"], "已完成")
        self.add_task(source, project["page_id"], "待确认")
        page = self.api.pages[project["page_id"]]
        page["properties"]["Development Progress"] = {"number": 35}
        manual = self.ws.projects(force=True)[0]
        self.assertEqual((manual["development_progress"], manual["progress_source"]), (35, "manual"))
        for invalid in [None, -1, 101, True, float('nan')]:
            page["properties"]["Development Progress"] = {"number": invalid}
            result = self.ws.projects(force=True)[0]
            self.assertEqual(result["progress_source"], "tasks")
            self.assertAlmostEqual(result["development_progress"], 600/7)

    def test_stale_task_progress_clears_while_manual_last_known_progress_is_marked_stale(self):
        project = self.connect(); source = self.task_source()
        self.add_task(source, project["page_id"], "已完成")
        self.ws.projects(force=True)
        self.time += timedelta(minutes=11)
        stale = self.ws.project_for_task("task-a")
        self.assertTrue(stale["stale"])
        self.assertIsNone(stale["development_progress"])
        self.assertIsNone(stale["progress_source"])
        self.api.pages[project["page_id"]]["properties"]["Development Progress"] = {"number": 50}
        self.ws.projects(force=True)
        self.time += timedelta(minutes=11)
        manual = self.ws.cached_projects()[0]
        self.assertTrue(manual["stale"])
        self.assertEqual((manual["development_progress"], manual["progress_source"]), (50, "manual"))

    def test_daily_sync_verification_preserves_fresh_task_counts_and_observation_time(self):
        project = self.connect(); source = self.task_source()
        for _ in range(8): self.add_task(source, project["page_id"], "已完成")
        for _ in range(3): self.add_task(source, project["page_id"], "待确认")
        observed = self.ws.projects(force=True)[0]["fetched_at"]
        self.time += timedelta(seconds=47)
        self.api.pages[project["page_id"]]["properties"]["Next Step"] = {"rich_text": ns._text("审阅同步结果")}
        calls = len(self.api.calls)
        result = self.ws.sync([self.row()], force=False)
        self.assertEqual(result["changed"], 1)
        self.assertIsNone(result["error"])
        cached = self.ws.project_for_task("task-a")
        self.assertEqual([cached[field] for field in ns.TASK_COUNT_FIELDS], [3, 0, 8])
        self.assertEqual(cached["fetched_at"], observed)
        self.assertEqual(cached["next_step"], "审阅同步结果")
        self.assertEqual(cached["progress_source"], "tasks")
        self.assertAlmostEqual(cached["development_progress"], 800/11)
        self.assertFalse(any(url.endswith(f"/data_sources/{source}/query")
                             for _, url, _ in self.api.calls[calls:]))
        self.assertEqual(self.ws.cached_projects()[0]["completed_tasks"], 8)

    def test_bind_verification_does_not_extend_task_count_ttl(self):
        project = self.connect(); source = self.task_source()
        self.add_task(source, project["page_id"], "已完成")
        self.add_task(source, project["page_id"], "待确认")
        observed = self.ws.projects(force=True)[0]["fetched_at"]
        for seconds in (300, 590):
            self.time = NOW + timedelta(seconds=seconds)
            bound = self.ws.bind("task-b", project["page_id"])
            self.assertEqual(bound["fetched_at"], observed)
            self.assertEqual(bound["development_progress"], 50)
            self.assertFalse(self.ws.project_for_task("task-b")["stale"])
        self.time = NOW + timedelta(seconds=600)
        expired = self.ws.bind("task-b", project["page_id"])
        self.assertEqual(expired["fetched_at"], observed)
        self.assertTrue(all(expired[field] is None for field in ns.TASK_COUNT_FIELDS))
        self.assertIsNone(expired["development_progress"])
        self.assertTrue(self.ws.project_for_task("task-b")["stale"])
        refreshed = self.ws.projects(force=True)[0]
        self.assertEqual(refreshed["fetched_at"], self.time.isoformat())
        self.assertEqual(refreshed["development_progress"], 50)

    def test_project_verification_rejects_partial_error_stale_or_different_page_task_cache(self):
        project = self.connect(); source = self.task_source()
        self.add_task(source, project["page_id"], "已完成")
        self.add_task(source, project["page_id"], "进行中")
        fresh = self.ws.projects(force=True)[0]
        cases = [({"open_tasks": None}, {}), ({"blocked_tasks": True}, {}),
                 ({"completed_tasks": -1}, {}), ({"stale": True}, {}),
                 ({"available": False}, {}), ({"fetched_at": None}, {}),
                 ({"page_id": str(uuid.UUID(int=9999))}, {}),
                 ({}, {"projects_error": {"code": "network_error"}}),
                 ({}, {"tasks_error": {"code": "network_error"}})]
        for fields, errors in cases:
            with self.subTest(fields=fields, errors=errors):
                with self.ws._locked():
                    self.ws.state["projects"][project["page_id"]] = {**fresh, **fields}
                    self.ws.state.pop("projects_error", None)
                    self.ws.state.pop("tasks_error", None)
                    self.ws.state.update(errors)
                    result = self.ws._verify_project(project["page_id"])
                self.assertTrue(all(result[field] is None for field in ns.TASK_COUNT_FIELDS))
                self.assertIsNone(result["development_progress"])

    def test_project_verification_uses_current_manual_progress_over_preserved_task_counts(self):
        project = self.connect(); source = self.task_source()
        self.add_task(source, project["page_id"], "已完成")
        self.ws.projects(force=True)
        self.api.pages[project["page_id"]]["properties"]["Development Progress"] = {"number": 0}
        bound = self.ws.bind("task-b", project["page_id"])
        self.assertEqual(bound["completed_tasks"], 1)
        self.assertEqual((bound["development_progress"], bound["progress_source"]), (0, "manual"))

    def test_keychain_lookup_is_scoped_to_only_this_application_item(self):
        from types import SimpleNamespace
        with patch.object(sys, "platform", "darwin"), patch.object(ns.subprocess, "run", return_value=
                SimpleNamespace(returncode=0, stdout=b"TEST_SECRET_DO_NOT_PRINT\n")) as run:
            self.assertEqual(ns.keychain_token(), "TEST_SECRET_DO_NOT_PRINT")
            self.assertEqual(run.call_args.args[0], ["/usr/bin/security", "find-generic-password", "-s",
                "local.codex.usage-widget.notion", "-a", "integration", "-w"])
            self.assertEqual(run.call_args.kwargs["stderr"], ns.subprocess.DEVNULL)

    def test_credential_is_read_once_per_operation_and_not_kept_afterward(self):
        self.ws.setup(PARENT)
        self.assertEqual(self.token_reads, 1)
        self.assertIsNone(self.ws._active_token)
        self.ws.create_project("task-a", "A", icon=PROJECT_ICON)
        self.assertEqual(self.token_reads, 2)
        self.ws.sync([self.row()], force=True)
        self.assertEqual(self.token_reads, 3)
        self.assertIsNone(self.ws._active_token)


if __name__ == "__main__":
    unittest.main()
