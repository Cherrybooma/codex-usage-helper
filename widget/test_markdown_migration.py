"""Independent takeover checks, entirely within temporary checkouts and vaults."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / "widget"))
from markdown_store import read_note, write_note

_spec = importlib.util.spec_from_file_location("markdown_migration", SOURCE / "scripts/migrate-markdown.py")
migration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migration)


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def load(path):
    return json.loads(path.read_text())


class MarkdownMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.current = self.base / "current-source"
        self.reference = self.base / "reference-source"
        self.vault = self.base / "existing-vault"
        self.data = self.current / "widget/data"
        self.refdata = self.reference / "widget/data"
        self.documents = self.base / "custom-project-root"
        self.folder = self.documents / "项目甲"
        self.folder.mkdir(parents=True)
        self.data.mkdir(parents=True)
        self.refdata.mkdir(parents=True)
        self.vault.mkdir()
        self.folder_key = "folder:" + str(self.folder)
        self.project_id = "p-existing-project"
        self.current_task, self.ref_task = "task-current", "task-reference"
        self.source_key = "retained-home-hash"
        self.registry = {
            "version": 1,
            "datasets": {self.source_key: "dataset-current", "only-current-source": "dataset-extra"},
            "projects": {self.folder_key: {"task_id": self.current_task, "name": "项目甲"}},
        }
        self.prior = {
            "version": 1,
            "datasets": {self.source_key: "dataset-existing-vault"},
            "projects": {self.folder_key: {"task_id": self.ref_task, "name": "旧名称"}},
        }
        save(self.data / "project-registry.json", self.registry)
        save(self.refdata / "project-registry.json", self.prior)
        save(self.refdata / "markdown-config.json", {
            "version": 1, "vault_path": str(self.vault), "task_root": str(self.documents),
            "bindings": {self.ref_task: self.project_id},
        })
        self.settings = {"task_root": str(self.documents), "theme": "pulseDark", "billing_anchor": "2026-10-08T00:00:00+08:00",
                         "billing_anchor_precision": "date", "billing_timezone": "Asia/Shanghai",
                         "notion_sync_enabled": True, "other_preference": "keep"}
        save(self.data / "app-settings.json", self.settings)
        save(self.refdata / "app-settings.json", {"management_enabled": True, "theme": "classicWhite"})
        save(self.data / "notion-config.json", {"bindings": {self.current_task: "old-notion-project-id"}, "preserve": "audit"})
        save(self.data / "notion-state.json", {"audit": ["retained old sync evidence"]})
        self.note = self.vault / "项目/人工选的目录/项目.md"
        write_note(self.note, {"type": "project", "id": self.project_id, "name": "已改名项目甲",
                             "status": "开发中", "icon": "📒", "revision": 7},
                   "# 人工项目正文\n\n## 总目标\n\n保留人工写过的目标。\n\n## 目前要做的事\n\n继续已决定的工作。\n")
        self.daily = self.vault / "账本/每日/已存在的记录.md"
        write_note(self.daily, {"type": "usage_daily", "id": "daily-existing", "project_id": self.project_id,
                               "dataset_id": "dataset-existing-vault", "day": "2026-10-24",
                               "summary_key": "already-published", "total_tokens": 123, "counted_tokens": 123},
                   "# 已经发布的投入\n\n保留既有计量与人的补充。\n")
        for filename in ("daily-ledger.sqlite3", "quota-history.sqlite3"):
            with sqlite3.connect(self.data / filename) as database:
                database.execute("CREATE TABLE preserved (id TEXT PRIMARY KEY, amount INTEGER)")
                database.execute("INSERT INTO preserved VALUES ('stable-original', 917)")

    def plan(self):
        return migration.plan(self.reference, self.vault, root=self.current)

    def apply(self, result=None):
        migration.apply(self.plan() if result is None else result, root=self.current)

    def files(self):
        return {str(path.relative_to(self.base)): path.read_bytes()
                for path in self.base.rglob("*") if path.is_file()}

    def test_dry_run_has_no_filesystem_writes_and_reuses_exact_folder_evidence(self):
        before = self.files()
        result = self.plan()
        self.assertEqual(self.files(), before)
        self.assertEqual(result["unresolved"], [])
        self.assertEqual(result["bindings"][0]["task_id"], self.current_task)
        self.assertEqual(result["bindings"][0]["project_id"], self.project_id)
        self.assertEqual(result["bindings"][0]["old_notion_page_id"], "old-notion-project-id")
        self.assertEqual(result["config"]["publication_dataset_aliases"], {"dataset-current": "dataset-existing-vault"})
        self.assertEqual(result["config"]["task_root"], str(self.documents))
        self.assertNotIn("dataset-extra", result["config"]["publication_dataset_aliases"])

    def test_cli_dry_run_uses_only_temporary_checkout_and_vault(self):
        script = self.current / "scripts/migrate-markdown.py"
        script.parent.mkdir()
        shutil.copy2(SOURCE / "scripts/migrate-markdown.py", script)
        before = self.files()
        result = subprocess.run([sys.executable, str(script), "--reference-root", str(self.reference),
                                 "--vault", str(self.vault)], capture_output=True, text=True,
                                env={"PATH": os.defpath, "PYTHONPATH": str(SOURCE / "widget"), "PYTHONDONTWRITEBYTECODE": "1"}, check=True)
        self.assertFalse(json.loads(result.stdout)["applied"])
        # Python may create import caches beside source; test fixtures remain unchanged.
        self.assertEqual(self.files(), before)

    def test_apply_preserves_original_registry_databases_notion_audit_and_markdown_bytes(self):
        immutable = [self.data / "project-registry.json", self.data / "daily-ledger.sqlite3",
                     self.data / "quota-history.sqlite3", self.data / "notion-config.json",
                     self.data / "notion-state.json", self.note, self.daily,
                     self.refdata / "project-registry.json", self.refdata / "markdown-config.json"]
        before = {path: path.read_bytes() for path in immutable}
        self.apply()
        self.assertEqual({path: path.read_bytes() for path in immutable}, before)
        self.assertEqual(read_note(self.note)[0]["revision"], 7)
        self.assertEqual(load(self.data / "markdown-config.json")["bindings"], {self.current_task: self.project_id})

    def test_apply_preserves_billing_theme_and_custom_task_root(self):
        self.apply()
        settings = load(self.data / "app-settings.json")
        for field in ("billing_anchor", "billing_anchor_precision", "billing_timezone", "theme", "other_preference", "task_root"):
            self.assertEqual(settings[field], self.settings[field])
        self.assertTrue(settings["management_enabled"])
        self.assertEqual(settings["management_provider"], "markdown")
        self.assertFalse(settings["notion_sync_enabled"])

    def test_takeover_disables_reference_only_and_assigns_current_publisher(self):
        self.apply()
        reference_settings = load(self.refdata / "app-settings.json")
        self.assertFalse(reference_settings["management_enabled"])
        self.assertEqual(reference_settings["theme"], "classicWhite")
        owner = load(self.vault / ".ledger-publisher.json")
        self.assertEqual(owner["owner_data_dir"], str(self.data))
        self.assertEqual(owner["source_root"], str(self.current))

    def test_repeated_apply_is_idempotent_for_bindings_identities_and_notes(self):
        original = self.plan()
        self.apply(original)
        first = self.files()
        self.apply(original)
        second = self.files()
        owner_key = str((self.vault / ".ledger-publisher.json").relative_to(self.base))
        first.pop(owner_key)
        second.pop(owner_key)
        self.assertEqual(first, second)
        self.assertEqual(len(list(self.vault.rglob("*.md"))), 2)
        self.assertEqual(self.plan()["config"], original["config"])

    def test_unmatched_old_notion_binding_is_listed_and_blocks_all_apply(self):
        save(self.data / "notion-config.json", {"bindings": {self.current_task: "old-id", "unknown-task": "unmatched-id"}})
        result = self.plan()
        self.assertIn({"task_id": "unknown-task", "old_notion_page_id": "unmatched-id"}, result["unresolved"])
        before = self.files()
        with self.assertRaisesRegex(ValueError, "Unresolved"):
            self.apply(result)
        self.assertEqual(self.files(), before)

    def test_reference_project_not_in_vault_stays_unresolved(self):
        prior = load(self.refdata / "markdown-config.json")
        prior["bindings"][self.ref_task] = "p-missing"
        save(self.refdata / "markdown-config.json", prior)
        result = self.plan()
        self.assertTrue(any(row.get("project_id") == "p-missing" for row in result["unresolved"]))
        with self.assertRaises(ValueError):
            self.apply(result)

    def test_conflicting_current_binding_is_rejected(self):
        save(self.data / "markdown-config.json", {"vault_path": str(self.vault), "bindings": {self.current_task: "p-different"}})
        before = self.files()
        with self.assertRaisesRegex(ValueError, "Conflicting existing binding"):
            self.plan()
        self.assertEqual(self.files(), before)

    def test_conflicting_publication_alias_is_rejected(self):
        save(self.data / "markdown-config.json", {"vault_path": str(self.vault), "bindings": {},
                                                 "publication_dataset_aliases": {"dataset-current": "dataset-unrelated"}})
        with self.assertRaisesRegex(ValueError, "Conflicting publication identity"):
            self.plan()

    def test_foreign_reference_and_current_vaults_are_rejected(self):
        for filename in (self.refdata / "markdown-config.json", self.data / "markdown-config.json"):
            with self.subTest(config=filename.name, root=filename.parent.parent.parent.name):
                original = filename.read_bytes() if filename.exists() else None
                save(filename, {"vault_path": str(self.base / "different-vault"), "bindings": {}})
                before = self.files()
                with self.assertRaisesRegex(ValueError, "vault"):
                    self.plan()
                self.assertEqual(self.files(), before)
                if original is None:
                    filename.unlink()
                else:
                    filename.write_bytes(original)

    def test_unrelated_publisher_rejected_without_touching_settings(self):
        save(self.vault / ".ledger-publisher.json", {"owner_data_dir": str(self.base / "third-checkout/data")})
        before_current = (self.data / "app-settings.json").read_bytes()
        before_reference = (self.refdata / "app-settings.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "unrelated publisher"):
            self.apply()
        self.assertEqual((self.data / "app-settings.json").read_bytes(), before_current)
        self.assertEqual((self.refdata / "app-settings.json").read_bytes(), before_reference)
        self.assertFalse((self.data / "markdown-config.json").exists())

    def test_malformed_existing_publisher_does_not_get_silently_replaced(self):
        owner_file = self.vault / ".ledger-publisher.json"
        save(owner_file, {"version": 1, "unexpected": "retained"})
        before_owner = owner_file.read_bytes()
        with self.assertRaisesRegex(ValueError, "malformed"):
            self.apply()
        self.assertEqual(owner_file.read_bytes(), before_owner)
        self.assertTrue(load(self.refdata / "app-settings.json")["management_enabled"])
        self.assertFalse((self.data / "markdown-config.json").exists())

    def test_publication_lock_symlink_is_rejected(self):
        outside = self.base / "not-a-lock.txt"
        outside.write_text("preserve this external file")
        (self.vault / ".ledger-publication.lock").symlink_to(outside)
        with self.assertRaises(OSError):
            self.apply()
        self.assertEqual(outside.read_text(), "preserve this external file")
        self.assertTrue(load(self.refdata / "app-settings.json")["management_enabled"])
        self.assertFalse((self.data / "markdown-config.json").exists())

    def test_reference_publisher_can_be_explicitly_taken_over(self):
        save(self.vault / ".ledger-publisher.json", {"owner_data_dir": str(self.refdata)})
        self.apply()
        self.assertEqual(load(self.vault / ".ledger-publisher.json")["owner_data_dir"], str(self.data))

    def test_binding_changed_after_plan_is_not_overwritten(self):
        result = self.plan()
        config = {"vault_path": str(self.vault), "bindings": {"manually-added": "p-other"}}
        save(self.data / "markdown-config.json", config)
        with self.assertRaisesRegex(ValueError, "inputs changed"):
            self.apply(result)
        self.assertEqual(load(self.data / "markdown-config.json"), config)
        self.assertTrue(load(self.refdata / "app-settings.json")["management_enabled"])

    def test_invalid_current_settings_does_not_retire_reference(self):
        result = self.plan()
        (self.data / "app-settings.json").write_text("[]\n")
        before_reference = (self.refdata / "app-settings.json").read_bytes()
        with self.assertRaises(ValueError):
            self.apply(result)
        self.assertEqual((self.refdata / "app-settings.json").read_bytes(), before_reference)
        self.assertFalse((self.data / "markdown-config.json").exists())

    def test_same_reference_and_source_rejected(self):
        with self.assertRaisesRegex(ValueError, "different directories"):
            migration.plan(self.current, self.vault, root=self.current)


if __name__ == "__main__":
    unittest.main()
