"""Projection namespace migration preserves local identities and existing notes.

All project cards, scanner registries and counters here are synthetic fixtures;
no real Codex logs, credentials or central-vault notes are read or modified.
"""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from markdown_store import MarkdownError, MarkdownWorkspace, management_snapshot, write_note
from project_store import ProjectRegistry
from publication_identity import publication_payloads


def payloads(dataset="local-a", task="task-original"):
    cutoff = "2026-10-25T10:00:00+00:00"
    counters = {"input_tokens": 70, "output_tokens": 30, "total_tokens": 100,
                "cached_input_tokens": 20, "reasoning_output_tokens": 10,
                "request_records": 1}
    daily = {"dataset_id": dataset, "statistics_until": cutoff, "rows": [{
        **counters, "dataset_id": dataset, "task_id": task, "day": "2026-10-25",
        "statistics_until": cutoff, "sessions_count": 1, "session_keys": ["synthetic-session"],
        "quality": ["response_records_verified"]}]}
    period = {"start": "2026-10-19T00:00:00+08:00", "end": "2026-10-26T00:00:00+08:00",
              "observed_until": cutoff, "state": "open", "totals": counters, "sessions_count": 1,
              "projects": [{"name": "示例工程", "total_tokens": 100, "sessions_count": 1}],
              "models": [{"model": "fixture-model", "total_tokens": 100, "sessions_count": 1}],
              "coverage": {"status": "observed", "accepted_records": 1,
                           "quality": ["response_records_verified"]}}
    accounting = {"dataset_id": dataset, "generated_at": cutoff, "weekly": {"periods": [period]},
                  "monthly": {"status": "needs_anchor", "periods": []},
                  "quota": {"statistics_until": cutoff, "windows": []}}
    return daily, accounting


class PublicationIdentityChecks(unittest.TestCase):
    def test_empty_or_missing_alias_preserves_values_and_returns_detached_payloads(self):
        for config in ({}, {"publication_dataset_aliases": {}}):
            saved, accounting = payloads()
            original = deepcopy((config, saved, accounting))
            projected_saved, projected_accounting = publication_payloads(config, saved, accounting)
            self.assertEqual((config, saved, accounting), original)
            self.assertEqual(projected_saved, saved)
            self.assertEqual(projected_accounting, accounting)
            projected_saved["rows"][0]["total_tokens"] = 777
            projected_accounting["weekly"]["periods"][0]["totals"]["total_tokens"] = 999
            self.assertEqual(saved["rows"][0]["total_tokens"], 100)
            self.assertEqual(accounting["weekly"]["periods"][0]["totals"]["total_tokens"], 100)

    def test_explicit_alias_only_changes_projection_dataset_namespace(self):
        saved, accounting = payloads()
        originals = deepcopy((saved, accounting))
        config = {"publication_dataset_aliases": {"local-a": "central-kept"}}
        projected_saved, projected_accounting = publication_payloads(config, saved, accounting)
        self.assertEqual((saved, accounting), originals)
        self.assertEqual(projected_saved["dataset_id"], "central-kept")
        self.assertEqual(projected_saved["rows"][0]["dataset_id"], "central-kept")
        self.assertEqual(projected_saved["rows"][0]["task_id"], "task-original")
        self.assertEqual(projected_accounting["dataset_id"], "central-kept")
        projected_saved["dataset_id"] = "local-a"
        projected_saved["rows"][0]["dataset_id"] = "local-a"
        projected_accounting["dataset_id"] = "local-a"
        self.assertEqual((projected_saved, projected_accounting), originals)

    def test_identity_alias_and_unrelated_alias_do_not_change_source(self):
        saved, accounting = payloads()
        for aliases in ({"local-a": "local-a"}, {"unused-a": "unused-b"}):
            self.assertEqual(publication_payloads({"publication_dataset_aliases": aliases}, saved, accounting), (saved, accounting))

    def test_chains_and_cycles_are_rejected_before_mutation(self):
        saved, accounting = payloads()
        originals = deepcopy((saved, accounting))
        for aliases in ({"local-a": "next", "next": "final"},
                        {"local-a": "next", "next": "local-a"}):
            with self.subTest(aliases=aliases), self.assertRaises(MarkdownError) as caught:
                publication_payloads({"publication_dataset_aliases": aliases}, saved, accounting)
            self.assertEqual(caught.exception.code, "invalid_alias")
            self.assertEqual((saved, accounting), originals)

    def test_overlapping_active_sources_cannot_collapse_into_one_dataset(self):
        saved, accounting = payloads()
        saved["rows"].append({**saved["rows"][0], "dataset_id": "local-b", "task_id": "other-task"})
        for aliases in ({"local-a": "central", "local-b": "central"},
                        {"local-a": "local-b"}):
            originals = deepcopy((saved, accounting))
            with self.subTest(aliases=aliases), self.assertRaises(MarkdownError) as caught:
                publication_payloads({"publication_dataset_aliases": aliases}, saved, accounting)
            self.assertEqual(caught.exception.code, "ambiguous_alias")
            self.assertEqual((saved, accounting), originals)

    def test_invalid_alias_configuration_is_rejected(self):
        saved, accounting = payloads()
        invalid = (None, [], "a", {1: "central"}, {"local-a": None}, {"local-a": True},
                   {"": "central"}, {"local-a": ""}, {"local-a": "../central"},
                   {"local-a": "contains space"}, {"local-a": "a" * 201},
                   {"local-a": "target:invalid"}, {"local-a": "a" * 161})
        for aliases in invalid:
            with self.subTest(aliases=aliases), self.assertRaises(MarkdownError) as caught:
                publication_payloads({"publication_dataset_aliases": aliases}, saved, accounting)
            self.assertEqual(caught.exception.code, "invalid_alias")

    def test_takeover_updates_existing_notes_without_replacing_local_registry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            vault, documents = root / "vault", root / "Documents"
            vault.mkdir(); documents.mkdir()
            folder = documents / "示例工程"; folder.mkdir()
            project = "p-existing"
            write_note(vault / "项目/示例工程/项目.md",
                       {"type": "project", "id": project, "name": "示例工程", "status": "开发中",
                        "phase": "完成核心使用流程", "icon": "📚", "revision": 1},
                       "# 示例工程\n\n## 总目标\n\n一个可以使用的成果。\n\n## 目前要做的事\n\n检查真实操作。\n")
            reference = MarkdownWorkspace(root / "reference-data", vault)
            original = MarkdownWorkspace(root / "original-data", vault)
            for workspace in (reference, original):
                workspace.setup(vault, documents)
            registries = []
            task_ids = []
            for workspace in (reference, original):
                registry = ProjectRegistry(workspace.data_dir, root / "synthetic-codex-home")
                task = registry.identify("folder:" + str(folder), folder.name)
                registry.save()
                workspace.bind({"task_id": task, "project_id": project})
                registries.append(registry)
                task_ids.append(task)
            self.assertNotEqual(registries[0].dataset_id, registries[1].dataset_id)
            self.assertNotEqual(*task_ids)
            assignment = vault / ".ledger-publisher.json"
            assignment.write_text(json.dumps({"owner_data_dir": str(reference.data_dir.resolve())}))
            old_daily, old_accounting = payloads(registries[0].dataset_id, task_ids[0])
            reference.sync(old_daily["rows"], now=old_daily["statistics_until"],
                           complete_dataset_ids=[old_daily["dataset_id"]], complete_until=old_daily["statistics_until"])
            reference.sync_periods(old_accounting)
            before = management_snapshot(vault)["usage"]
            original.config["publication_dataset_aliases"] = {registries[1].dataset_id: registries[0].dataset_id}
            original.config_path.write_text(json.dumps(original.config))
            new_daily, new_accounting = payloads(registries[1].dataset_id, task_ids[1])
            source_daily, source_periods = original.data_dir / "markdown-daily.json", original.data_dir / "accounting-fixture.json"
            source_daily.write_text(json.dumps(new_daily)); source_periods.write_text(json.dumps(new_accounting))
            immutable_sources = {path: path.read_bytes() for path in (source_daily, source_periods, registries[1].path)}
            original_inputs = deepcopy((new_daily, new_accounting))
            assignment.write_text(json.dumps({"owner_data_dir": str(original.data_dir.resolve())}))
            projected_daily, projected_accounting = publication_payloads(original.config, new_daily, new_accounting)
            original.sync(projected_daily["rows"], now=projected_daily["statistics_until"],
                          complete_dataset_ids=[projected_daily["dataset_id"]], complete_until=projected_daily["statistics_until"])
            original.sync_periods(projected_accounting)
            # A repeat remains an absolute update; it cannot add a second copy.
            original.sync(projected_daily["rows"], now=projected_daily["statistics_until"])
            original.sync_periods(projected_accounting)
            after = management_snapshot(vault)["usage"]
            for family in ("daily", "periods"):
                self.assertEqual(len(before[family]), 1)
                self.assertEqual(len(after[family]), 1)
                self.assertEqual(after[family][0]["id"], before[family][0]["id"])
                self.assertEqual(after[family][0]["path"], before[family][0]["path"])
                self.assertEqual(after[family][0]["counted_tokens"], 100)
                self.assertEqual(after[family][0]["total_tokens"], 100)
                self.assertEqual(after[family][0]["dataset_id"], registries[0].dataset_id)
            self.assertEqual(after["daily"][0]["task_ids"], [task_ids[1]])
            self.assertEqual((new_daily, new_accounting), original_inputs)
            self.assertEqual(immutable_sources, {path: path.read_bytes() for path in immutable_sources})
            local_registry = ProjectRegistry(original.data_dir, root / "synthetic-codex-home")
            self.assertEqual(local_registry.dataset_id, registries[1].dataset_id)
            self.assertEqual(local_registry.identify("folder:" + str(folder), folder.name), task_ids[1])
            with self.assertRaises(MarkdownError) as caught:
                reference.sync(old_daily["rows"], now=old_daily["statistics_until"])
            self.assertEqual(caught.exception.code, "publisher_not_owner")


if __name__ == "__main__":
    unittest.main()
