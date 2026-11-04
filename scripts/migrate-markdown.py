#!/usr/bin/env python3
"""Compare an existing local Markdown adaptation, then opt in to its bindings.

Does not copy scanner databases, registry IDs, project cards or credentials.
Run without --apply to review. The reference publisher must be stopped before
applying; its existing supported management switch is then disabled.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
import stat
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "widget"))
from markdown_store import DEFAULT_VAULT, MarkdownError, management_snapshot, _save_json


def read(path, default=None):
    if not path.is_file() and default is not None:
        return default
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("Invalid object: " + path.name)
    return value


def plan(reference, vault, root=ROOT):
    reference, vault, root = Path(reference).resolve(), Path(vault).resolve(), Path(root).resolve()
    if reference == root:
        raise ValueError("Reference and current source must be different directories")
    data, old = root / "widget/data", reference / "widget/data"
    registry, prior = read(data / "project-registry.json"), read(old / "project-registry.json")
    config = read(data / "markdown-config.json", {})
    previous = read(old / "markdown-config.json")
    if Path(previous["vault_path"]).resolve() != vault.resolve():
        raise ValueError("Reference vault does not match the chosen existing vault")
    if config.get("vault_path") and Path(config["vault_path"]).resolve() != vault.resolve():
        raise ValueError("Current bindings belong to a different vault")
    projects = {p["project_id"]: p for p in management_snapshot(vault)["projects"]}
    bindings = dict(config.get("bindings", {}))
    rows, unresolved = [], []
    for folder, entry in prior["projects"].items():
        project = previous.get("bindings", {}).get(entry["task_id"])
        if not project:
            continue
        current = registry["projects"].get(folder)
        if not current or project not in projects:
            unresolved.append({"folder": folder, "project_id": project})
            continue
        identity = current["task_id"]
        if bindings.get(identity, project) != project:
            raise ValueError("Conflicting existing binding: " + folder)
        bindings[identity] = project
        rows.append({"folder": folder, "task_id": identity,
                     "project_id": project, "name": projects[project]["display_name"],
                     "evidence": "同一规范工程路径的既有 Markdown 绑定"})
    old_notion = read(data / "notion-config.json", {}).get("bindings", {})
    matched = {r["task_id"] for r in rows}
    for task, page in old_notion.items():
        if task not in matched:
            unresolved.append({"task_id": task, "old_notion_page_id": page})
        else:
            row = next(r for r in rows if r["task_id"] == task)
            row["old_notion_page_id"] = page
    aliases = dict(config.get("publication_dataset_aliases", {}))
    for source_key, dataset in registry["datasets"].items():
        canonical = prior["datasets"].get(source_key)
        if canonical and canonical != dataset:
            if aliases.get(dataset, canonical) != canonical:
                raise ValueError("Conflicting publication identity")
            aliases[dataset] = canonical
    settings = read(data / "app-settings.json", {})
    selected_root = (config.get("task_root") or settings.get("task_root")
                     or previous.get("task_root") or Path.home() / "Documents")
    config.update(version=1, vault_path=str(vault.resolve()),
                  task_root=str(Path(selected_root).expanduser().resolve()), bindings=bindings,
                  publication_dataset_aliases=aliases)
    return {"bindings": rows, "unresolved": unresolved, "config": config,
            "source_root": str(root.resolve()), "reference_root": str(reference.resolve()),
            "vault": str(vault.resolve()), "local_registry_preserved": True,
            "existing_notes_reused": True}


def apply(result, root=ROOT):
    if result["unresolved"]:
        raise ValueError("Unresolved bindings; review before applying")
    data = root / "widget/data"
    reference = Path(result["reference_root"])
    vault = Path(result["vault"])
    # Both publishers share this vault lock after adaptation. The older code is
    # explicitly disabled first; it must not be left running during takeover.
    descriptor = os.open(vault / ".ledger-publication.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a+") as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
            raise ValueError("Publisher lock must be a regular file")
        fcntl.flock(lock, fcntl.LOCK_EX)
        owner_file = vault / ".ledger-publisher.json"
        if owner_file.is_symlink():
            raise ValueError("Publisher marker cannot be a symlink")
        owner = read(owner_file, {})
        if owner_file.exists() and not isinstance(owner.get("owner_data_dir"), str):
            raise ValueError("Existing publisher marker is malformed; no automatic takeover")
        if owner.get("owner_data_dir") not in (None, str(data.resolve()), str((reference / "widget/data").resolve())):
            raise ValueError("Vault has an unrelated publisher; no automatic takeover")
        # A reviewed plan must not overwrite a binding changed before takeover.
        fresh = plan(reference, vault, root)
        if any(fresh[key] != result[key] for key in ("config", "bindings", "unresolved")):
            raise ValueError("Migration inputs changed; review a fresh plan before applying")
        # Validate both settings objects before retiring either publisher.
        retired = read(reference / "widget/data/app-settings.json", {})
        settings = read(data / "app-settings.json", {})
        retired.update(management_enabled=False, publishing_retired_to=str(root.resolve()))
        _save_json(reference / "widget/data/app-settings.json", retired)
        settings.update(vault_path=str(vault), management_provider="markdown", management_enabled=True,
                        task_root=result["config"]["task_root"], notion_sync_enabled=False)
        _save_json(data / "markdown-config.json", result["config"])
        _save_json(data / "app-settings.json", settings)
        _save_json(owner_file, {"version": 1, "owner_data_dir": str(data.resolve()),
                               "source_root": str(root.resolve()),
                               "reference_publisher_disabled": str(reference.resolve()),
                               "updated_at": datetime.now(timezone.utc).isoformat()})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-root", type=Path, required=True)
    parser.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = plan(args.reference_root.resolve(), args.vault.resolve())
        if args.apply:
            apply(result)
        result["applied"] = args.apply
        if args.output:
            _save_json(args.output, result)
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError, TypeError, MarkdownError) as exc:
        # Never echo malformed private configuration content.
        print(json.dumps({"ok": False, "error": type(exc).__name__,
                          "message": "迁移未完成，请保留原数据并检查绑定与发布者。"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
