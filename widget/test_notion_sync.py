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
        second = self.ws.create_project("other-task", "新项目")
        self.ws.bind("task-a", second["page_id"])
        with self.ws._locked():
            self.ws.state["synced"] = {}
        result = self.ws.sync([self.row()], force=True, complete_dataset_ids=[dataset])
        self.assertIsNone(result["error"])
        self.assertEqual(sum(p["properties"]["Counted Tokens"]["number"] for p in self.api.summaries()), 100)

    def test_rebinding_moves_allocation_without_counting_old_project_twice(self):
        first = self.connect()
        second = self.ws.create_project("other-task", "另一个项目")
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
        second = self.ws.create_project("other-project", "另一个项目")
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
        project = self.ws.create_project("select-task", "选择状态项目")
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
        return self.ws.create_project("task-a", "用量小助手")

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

    def test_two_database_setup_uses_current_schema_and_shanghai_week(self):
        self.ws.setup(PARENT)
        self.assertEqual(self.ws.status()["sync_since"], "2026-10-12")
        self.assertEqual(len(self.api.databases), 2)
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
        self.assertEqual(len(self.api.databases), 2)
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
            self.ws.create_project(f"task-{n}", f"项目{n}")
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
        self.assertEqual(len(self.api.databases), 2)

    def test_project_create_timeout_recovers_stable_binding(self):
        self.ws.setup(PARENT)
        self.api.schedule(lambda m, p, b: p == "/pages", TimeoutError("secret"), after=True)
        meta = self.ws.create_project("task-a", "A")
        self.assertEqual(self.ws.status()["bindings"]["task-a"], meta["page_id"])
        self.assertEqual(self.new_workspace().create_project("task-a", "Renamed")["page_id"], meta["page_id"])

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
        self.ws.create_project("task-a", "A")
        self.assertEqual(self.token_reads, 2)
        self.ws.sync([self.row()], force=True)
        self.assertEqual(self.token_reads, 3)
        self.assertIsNone(self.ws._active_token)


if __name__ == "__main__":
    unittest.main()
