"""Explicit, resumable project import. AI prepares facts; this module never invents them.

Only whitelisted management summaries leave the machine. Folder paths are local
identities and do not appear in Notion payloads. Importing is an explicit action,
not a periodic background rewrite of human edits.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

if __package__:
    from . import notion_sync as ns
    from .project_store import ProjectRegistry, project_location
else:
    import notion_sync as ns
    from project_store import ProjectRegistry, project_location

MAX_MANIFEST_BYTES = 256 * 1024
STATUSES = {"计划中", "开发中", "已发布", "暂停", "归档"}
ACTION_STATUSES = {"待处理", "进行中", "待确认", "阻塞", "已完成", "取消"}
PROJECT_FIELDS = {"name", "icon", "vision", "status", "health", "current_goal", "next_step",
                  "summary", "progress", "artifacts", "manual_actions", "history"}
EXTRA_PROJECT_SCHEMA = {
    "Current Goal": {"rich_text": {}}, "Latest Output": {"rich_text": {}},
    "Latest Output URL": {"url": {}}, "Latest Progress": {"rich_text": {}},
    "Progress Source": {"select": {"options": [{"name": "AI估算"}, {"name": "手动"}]}},
    "Progress Evidence": {"rich_text": {}}, "Progress Reviewed": {"date": {}},
    "Development Progress": {"number": {"format": "number"}},
    "Goal": {"rich_text": {}}, "Next Step": {"rich_text": {}}, "Summary": {"rich_text": {}},
    "Health": {"select": {"options": [{"name": n} for n in ("正常", "有风险", "受阻")]}},
    "Outcome": {"url": {}},
}
EXTRA_TASK_SCHEMA = {n: {"rich_text": {}} for n in ("Decision", "Recommendation", "Impact", "Task Key")}


def _invalid():
    raise ns.NotionError("invalid_input")


def _shape(value, fields):
    if not isinstance(value, dict) or set(value) - set(fields):
        _invalid()


def _text(value, *, limit=1000, required=False):
    if value is None and not required:
        return ""
    if not isinstance(value, str) or len(value) > limit:
        _invalid()
    value = value.strip()
    # The manifest is an allow-list, not a way to send credentials or private paths.
    if (required and not value) or any(ord(c) < 32 and c not in "\n\t" for c in value):
        _invalid()
    if re.search(r"(?:sk-|ntn_|secret_)[A-Za-z0-9_-]{16,}", value):
        _invalid()
    if (re.search(r"(?:/Users/|/home/)", value)
            or re.search(r"(?:^|[\s\"'(])[A-Za-z]:[\\/]", value) or "```" in value):
        _invalid()
    return value


def _url(value):
    if value in (None, ""):
        return None
    value = _text(value, limit=2048)
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            _invalid()
        if parsed.hostname.lower() in ("localhost", "127.0.0.1", "::1"):
            _invalid()
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            _invalid()
    except ValueError:
        _invalid()
    return value


def _time(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            _invalid()
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, AttributeError, TypeError):
        _invalid()


def validate_manifest(value, folder: Path | None = None) -> dict:
    """Reject unexpected fields instead of forwarding arbitrary user JSON."""
    _shape(value, {"version", "project"})
    if value.get("version") != 1 or isinstance(value.get("version"), bool):
        _invalid()
    project = value.get("project")
    _shape(project, PROJECT_FIELDS)
    result = {key: _text(project.get(key), required=key in {"name", "vision", "current_goal", "next_step"},
                         limit=200 if key == "name" else 1000)
              for key in ("name", "vision", "current_goal", "next_step", "summary")}
    result["icon"] = ns.project_icon(project.get("icon"))
    result["status"] = project.get("status", "开发中")
    result["health"] = project.get("health", "正常")
    if result["status"] not in STATUSES or result["health"] not in {"正常", "顺利", "有风险", "受阻", "阻塞"}:
        _invalid()
    progress = project.get("progress")
    if progress is None:
        result["progress"] = None
    else:
        _shape(progress, {"percent", "evidence", "reviewed_at", "milestones"})
        percent = progress.get("percent")
        if (not isinstance(percent, (int, float)) or isinstance(percent, bool)
                or not 0 <= percent <= 100 or not math.isfinite(percent)):
            _invalid()
        milestones = progress.get("milestones")
        if not isinstance(milestones, list) or not 1 <= len(milestones) <= 20:
            _invalid()
        normalized = []
        for item in milestones:
            _shape(item, {"name", "weight", "completion", "evidence"})
            weight, completion = item.get("weight"), item.get("completion")
            if (not isinstance(weight, (int, float)) or isinstance(weight, bool)
                    or not 0 < weight <= 1000 or not math.isfinite(weight)
                    or not isinstance(completion, (int, float)) or isinstance(completion, bool)
                    or not 0 <= completion <= 100 or not math.isfinite(completion)):
                _invalid()
            normalized.append({"name": _text(item.get("name"), limit=120, required=True),
                               "weight": weight, "completion": completion,
                               "evidence": _text(item.get("evidence"), required=True)})
        derived = sum(m["weight"] * m["completion"] for m in normalized) / sum(m["weight"] for m in normalized)
        if abs(percent - round(derived)) > .000001:
            _invalid()
        result["progress"] = {"percent": percent, "evidence": _text(progress.get("evidence"), required=True),
                              "reviewed_at": _time(progress.get("reviewed_at")), "milestones": normalized}
    result["artifacts"] = []
    artifacts = project.get("artifacts", [])
    if not isinstance(artifacts, list) or len(artifacts) > 12:
        _invalid()
    for item in artifacts:
        _shape(item, {"title", "url", "relative_path", "result"})
        path = item.get("relative_path")
        if path:
            path = _text(path, limit=512)
            relative = Path(path)
            if relative.is_absolute() or ".." in relative.parts or "\\" in path:
                _invalid()
            if folder is not None:
                target = (folder / relative).resolve()
                if not target.is_relative_to(folder.resolve()) or not target.is_file():
                    _invalid()
        url = _url(item.get("url"))
        if not path and not url:
            _invalid()
        result["artifacts"].append({"title": _text(item.get("title"), limit=160, required=True),
                                    "url": url, "relative_path": path or None,
                                    "result": _text(item.get("result"), required=True)})
    for field, allowed in (("manual_actions", {"id", "name", "status", "decision", "recommendation", "impact", "evidence_url"}),
                           ("history", {"id", "title", "occurred_at", "result", "evidence_url"})):
        items = project.get(field, [])
        if not isinstance(items, list) or len(items) > 30:
            _invalid()
        result[field], seen = [], set()
        for item in items:
            _shape(item, allowed)
            identity = _text(item.get("id"), limit=160, required=True)
            if identity in seen:
                _invalid()
            seen.add(identity)
            row = {key: _text(item.get(key), limit=1000 if key not in ("id", "name", "title") else 160,
                              required=key in ("id", "name", "title", "result"))
                   for key in allowed - {"status", "occurred_at", "evidence_url"}}
            row["evidence_url"] = _url(item.get("evidence_url"))
            if field == "manual_actions":
                row["status"] = item.get("status", "待确认")
                if row["status"] not in ACTION_STATUSES:
                    _invalid()
            else:
                row["occurred_at"] = _time(item["occurred_at"]) if item.get("occurred_at") else None
            result[field].append(row)
    return {"version": 1, "project": result}


def _block(kind, text, *, url=None):
    # A full goal plus its progress summary may exceed Notion's limit for one
    # rich-text item. Keep the paragraph intact without truncating either fact.
    rich = [item for start in range(0, len(text), 2000) for item in ns._text(text[start:start + 2000])] or ns._text("")
    if url:
        for item in rich:
            item["text"]["link"] = {"url": url}
    return {"object": "block", "type": kind, kind: {"rich_text": rich}}


class ProjectImportWorkspace(ns.NotionWorkspace):
    """A single writer using the existing process lock and creation outbox."""

    def _children(self, parent):
        cursor, result, seen = None, [], set()
        while True:
            suffix = "?page_size=100" + ("&start_cursor=" + cursor if cursor else "")
            page = self._request("GET", f"/blocks/{parent}/children{suffix}", idempotent=True)
            result.extend(page.get("results", []))
            if not page.get("has_more"):
                return result
            cursor = page.get("next_cursor")
            if not cursor or cursor in seen:
                raise ns.NotionError("invalid_response")
            seen.add(cursor)

    def _verify_created(self, page, payload):
        parent = page.get("parent") or {}
        expected = payload["parent"]
        field = expected["type"]
        if (page.get("archived") or page.get("in_trash")
                or parent.get(field) != expected.get(field)):
            raise ns.NotionError("invalid_response")
        properties = page.get("properties") or {}
        identity_fields = set(payload["properties"]) & {"Binding Key", "Task Key", "Event Key"}
        for key in identity_fields:
            if ns._plain((properties.get(key) or {}).get("rich_text")) != ns._plain(payload["properties"][key]["rich_text"]):
                raise ns.NotionError("invalid_response")
        if not identity_fields:
            title = next((p.get("title") for p in properties.values() if isinstance(p, dict) and "title" in p), [])
            desired = next((p.get("title") for p in payload["properties"].values() if "title" in p), [])
            if ns._plain(title) != ns._plain(desired):
                raise ns.NotionError("invalid_response")
        markers = []
        def desired_markers(blocks):
            for block in blocks:
                kind = block.get("type")
                data = block.get(kind) or {}
                text = ns._plain(data.get("rich_text"))
                if text.startswith(("codex-project-management:", "产出记录 ")):
                    markers.append(text)
                desired_markers(data.get("children", []))
        desired_markers(payload.get("children", []))
        if markers:
            observed = self._children(ns.notion_id(page["id"]))
            observed += [child for b in list(observed) if b.get("type") == "toggle"
                         for child in self._children(ns.notion_id(b["id"]))]
            texts = {ns._plain(b.get(b.get("type"), {}).get("rich_text")) for b in observed}
            if not all(marker in texts for marker in markers):
                raise ns.NotionError("invalid_response")
        return page

    def _create_unique(self, operation, discover, payload):
        matches = discover()
        if len(matches) > 1:
            raise ns.NotionError("duplicate_summary")
        pending = self.state["uncertain_creates"].get(operation)
        if matches:
            self._verify_created(matches[0], payload)
            self.state["uncertain_creates"].pop(operation, None)
            return matches[0]
        if pending:
            if pending.get("committed_id"):
                found = self._request("GET", "/pages/" + pending["committed_id"], idempotent=True)
                self._verify_created(found, payload)
                self.state["uncertain_creates"].pop(operation, None)
                return found
            raise ns.NotionError("write_uncertain")
        self.state["uncertain_creates"][operation] = {"kind": "project_import"}
        self._save(self.state_path, self.state)
        try:
            page = self._request("POST", "/pages", payload)
        except ns.NotionError as exc:
            if exc.committed_id:
                self.state["uncertain_creates"][operation]["committed_id"] = exc.committed_id
                page = self._request("GET", "/pages/" + exc.committed_id, idempotent=True)
            elif exc.uncertain:
                found = discover()
                if len(found) != 1:
                    raise ns.NotionError("write_uncertain") from None
                page = found[0]
            else:
                self.state["uncertain_creates"].pop(operation, None)
                raise
        self._verify_created(page, payload)
        self.state["uncertain_creates"].pop(operation, None)
        return page

    def _home(self, parent_page, management_home):
        if self.config.get("parent_page_id"):
            existing = self.config["parent_page_id"]
            if management_home and ns.notion_id(management_home) != existing:
                raise ns.NotionError("invalid_input")
            return existing
        if management_home:
            home = ns.notion_id(management_home)
            self._request("GET", "/pages/" + home, idempotent=True)
            return home
        if not parent_page:
            raise ns.NotionError("not_configured")
        parent = ns.notion_id(parent_page)
        self._request("GET", "/pages/" + parent, idempotent=True)
        marker = "codex-project-management:" + self.config["dataset_id"]
        def discover():
            matches = []
            for block in self._children(parent):
                if block.get("type") != "child_page" or block.get("child_page", {}).get("title") != "项目管理":
                    continue
                children = self._children(ns.notion_id(block["id"]))
                markers = [b for b in children if b.get("type") == "toggle"
                           and ns._plain(b.get("toggle", {}).get("rich_text")) == "连接信息"]
                if any(any(ns._plain(child.get("paragraph", {}).get("rich_text")) == marker
                           for child in self._children(ns.notion_id(b["id"]))) for b in markers):
                    matches.append(self._request("GET", "/pages/" + ns.notion_id(block["id"]), idempotent=True))
            return matches
        page = self._create_unique("import-home:" + parent, discover, {
            "parent": {"type": "page_id", "page_id": parent}, "icon": {"type": "emoji", "emoji": "🗂️"},
            "properties": {"title": {"type": "title", "title": ns._text("项目管理")}},
            "children": [_block("paragraph", "AI 自动维护任务和进展，并在这里展示看板"),
                         {"object": "block", "type": "toggle", "toggle": {"rich_text": ns._text("连接信息"),
                          "children": [_block("paragraph", marker)]}}]})
        return ns.notion_id(page["id"])

    def _migrate(self, source_id, expected):
        source = self._request("GET", "/data_sources/" + source_id, idempotent=True)
        actual = source.get("properties", {})
        missing = {}
        for name, definition in expected.items():
            if name not in actual:
                missing[name] = definition
            elif (name == "Created At" and actual[name].get("type", next(iter(actual[name]), None)) in ("date", "created_time")):
                continue
            elif actual[name].get("type", next(iter(actual[name]), None)) != next(iter(definition)):
                raise ns.NotionError("schema_mismatch")
            elif name == "Project" and actual[name].get("relation", {}).get("data_source_id") != definition["relation"]["data_source_id"]:
                raise ns.NotionError("schema_mismatch")
        if missing:
            self._request("PATCH", "/data_sources/" + source_id, {"properties": missing}, idempotent=True)

    def _ensure_events(self, parent):
        schema = {"Name": {"title": {}}, "Project": {"relation": {"data_source_id": self.config["projects_data_source_id"],
                  "type": "single_property", "single_property": {}}}, "Event Key": {"rich_text": {}},
                  "Occurred At": {"date": {}}, "Created At": {"created_time": {}}, "Result": {"rich_text": {}}, "Evidence": {"url": {}}}
        if self.config.get("events_data_source_id"):
            self._migrate(self.config["events_data_source_id"], schema)
            source = self._request("GET", "/data_sources/" + self.config["events_data_source_id"], idempotent=True)
            self._event_created_type = source["properties"]["Created At"].get("type")
            return
        marker = f"codex-usage-helper:{self.config['dataset_id']}:events:v1"
        database = self._find_database(parent, marker)
        operation = "database:events"
        if not database and operation in self.state["uncertain_creates"]:
            raise ns.NotionError("write_uncertain")
        if not database:
            self.state["uncertain_creates"][operation] = {"kind": "events"}
            self._save(self.state_path, self.state)
            try:
                database = self._request("POST", "/databases", {"parent": {"type": "page_id", "page_id": parent},
                    "title": ns._text("开发记录 Events"), "description": ns._text(marker), "is_inline": True,
                    "initial_data_source": {"properties": schema}})
            except ns.NotionError as exc:
                if exc.committed_id:
                    database = self._request("GET", "/databases/" + exc.committed_id, idempotent=True)
                elif exc.uncertain:
                    database = self._find_database(parent, marker)
                    if not database:
                        raise ns.NotionError("write_uncertain") from None
                else:
                    self.state["uncertain_creates"].pop(operation, None)
                    raise
        self.state["uncertain_creates"].pop(operation, None)
        sources = database.get("data_sources", [])
        if len(sources) != 1:
            raise ns.NotionError("schema_mismatch")
        self.config.update(events_database_id=ns.notion_id(database["id"]),
                           events_data_source_id=ns.notion_id(sources[0]["id"]), events_url=ns._safe_https(database.get("url")))
        self._event_created_type = "created_time"

    def _row(self, source, key_property, key, properties):
        discover = lambda: self._query(source, filter={"property": key_property, "rich_text": {"equals": key}})
        page = self._create_unique("import-row:" + key, discover, {
            "parent": {"type": "data_source_id", "data_source_id": source}, "properties": properties})
        def equal(actual, desired):
            for name, prop in desired.items():
                field = next(iter(prop))
                left, right = (actual.get(name) or {}).get(field), prop[field]
                if field in ("title", "rich_text"):
                    if ns._plain(left) != ns._plain(right): return False
                elif field == "select":
                    if (left or {}).get("name") != (right or {}).get("name"): return False
                elif field == "relation":
                    if sorted(p["id"] for p in left or []) != sorted(p["id"] for p in right or []): return False
                elif left != right:
                    return False
            return True
        if not equal(page.get("properties") or {}, properties):
            page = self._request("PATCH", "/pages/" + ns.notion_id(page["id"]), {"properties": properties}, idempotent=True)
        return page

    def _layout(self, project, page_id):
        """One compact managed section; historical facts live in immutable Events."""
        pending = [a for a in project["manual_actions"] if a["status"] not in ("已完成", "取消")]
        confirm = "\n".join(a["name"] + ("\n推荐：" + a["recommendation"][:160] if a["recommendation"] else "")
                            + (" · 影响：" + a["impact"][:160] if a["impact"] else "") for a in pending[:3]) or "暂无需要确认的事项"
        if len(pending) > 3:
            confirm += f"\n还有 {len(pending) - 3} 项，见任务库。"
        current = "当前目标：" + project["current_goal"]
        if project["summary"]:
            current += "\n进展：" + project["summary"]
        artifacts = project["artifacts"][:3]
        output_blocks = [_block("paragraph", a["title"] + " · " + a["result"]
                              + ("（本机待发布：" + a["relative_path"] + "）" if not a["url"] else ""), url=a["url"])
                         for a in artifacts]
        columns = {"object": "block", "type": "column_list", "column_list": {"children": [
            {"object": "block", "type": "column", "column": {"children": [_block("heading_3", "当前目标与进展"), _block("paragraph", current)] + output_blocks}},
            {"object": "block", "type": "column", "column": {"children": [_block("heading_3", "下一步"), _block("paragraph", project["next_step"])]}}]}}
        # Known dates order the preview; undated backfills never acquire an
        # invented original completion date. The immutable Events rows remain
        # the historical source of truth, rather than these current-state lines.
        recent = sorted(project["history"], key=lambda event: event["occurred_at"] or "", reverse=True)[:3]
        history = []
        for event in recent:
            date = event["occurred_at"].replace("+00:00", "Z") if event["occurred_at"] else "本次回填，原完成时间未核实"
            result = event["result"][:180] + ("…" if len(event["result"]) > 180 else "")
            history.append(date + " · " + event["title"] + "：" + result)
        history_text = "\n".join(history) if history else "本次未提供历史摘要。"
        history_text += "\n打开完整开发记录"
        blocks = [_block("paragraph", project["vision"]), _block("heading_2", "待人工确认"),
                  _block("callout", confirm), _block("heading_2", "当前进展与下一步"), columns,
                  _block("heading_2", "历史开发记录"), _block("paragraph", history_text, url=self.config.get("events_url")),
                  _block("heading_2", "Token 投入"), _block("paragraph", "由本机扫描日志统计；打开用量库，按当前项目查看。", url=self.config.get("usage_url"))]
        state = self.state.setdefault("project_imports", {}).setdefault(page_id, {})
        def canonical(block, *, observed=False):
            kind = block.get("type")
            content = block.get(kind) or {}
            if kind in ("column_list", "column"):
                children = self._children(ns.notion_id(block["id"])) if observed else content.get("children", [])
                return {"type": kind, "children": [canonical(child, observed=observed) for child in children]}
            rich = content.get("rich_text", [])
            urls = sorted({item.get("text", {}).get("link", {}).get("url")
                           for item in rich if isinstance(item, dict) and item.get("text", {}).get("link")})
            return {"type": kind, "text": "".join(ns._plain([item]) for item in rich), "urls": urls}
        def fingerprint(values, *, observed=False):
            return hashlib.sha256(json.dumps([canonical(b, observed=observed) for b in values],
                ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        digest = fingerprint(blocks)
        operation = "import-layout:" + page_id
        if state.get("layout_fingerprint") == digest and operation not in self.state["uncertain_creates"]:
            return
        # Initial append is recoverable from a persisted operation and matching
        # section headings. Never append again while its result is uncertain.
        existing = self._children(page_id)
        own_ids = state.get("layout_ids", [])
        if operation in self.state["uncertain_creates"]:
            pending = self.state["uncertain_creates"][operation]
            old = set(pending.get("preceding_ids", []))
            added = [b for b in existing if b["id"] not in old]
            if (len(added) == len(blocks)
                    and [b.get("type") for b in added] == [b["type"] for b in blocks]
                    and fingerprint(added, observed=True) == pending.get("fingerprint")):
                self.state["uncertain_creates"].pop(operation, None)
                # Only recover IDs from this operation's saved preceding IDs.
                own_ids = [b["id"] for b in added]
                state["layout_ids"] = own_ids
                state["layout_fingerprint"] = pending.get("fingerprint")
                if state["layout_fingerprint"] == digest:
                    return
            else:
                raise ns.NotionError("write_uncertain")
        if not own_ids and any(b.get("type") != "child_page" for b in existing):
            # Existing human-built dashboards and native views belong to the
            # user. Property updates are enough for the capsule; the Skill can
            # reconcile the existing body by reading its actual block IDs.
            state["layout_preserved_existing"] = True
            return
        if own_ids:
            # This is current-state presentation, not the historical ledger.
            # Archive only blocks created by this importer, retaining Events.
            for block_id in own_ids:
                self._request("PATCH", "/blocks/" + ns.notion_id(block_id), {"archived": True}, idempotent=True)
            state["layout_ids"] = []
        self.state["uncertain_creates"][operation] = {"kind": "layout", "preceding_ids": [b["id"] for b in existing], "fingerprint": digest}
        self._save(self.state_path, self.state)
        try:
            result = self._request("PATCH", "/blocks/" + page_id + "/children", {"children": blocks})
        except ns.NotionError as exc:
            if not exc.uncertain:
                self.state["uncertain_creates"].pop(operation, None)
            raise
        state["layout_ids"] = [b["id"] for b in result.get("results", [])]
        if len(state["layout_ids"]) != len(blocks):
            raise ns.NotionError("write_uncertain")
        state["layout_fingerprint"] = digest
        self.state["uncertain_creates"].pop(operation, None)

    def _artifact_index(self, project, page_id):
        """A real Notion entry for local outcomes, explicitly pending publication."""
        artifacts = project["artifacts"]
        if not artifacts or artifacts[0]["url"]:
            return artifacts[0]["url"] if artifacts else None
        digest = hashlib.sha256(json.dumps(artifacts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        title = "阶段产出 · " + artifacts[0]["title"]
        operation = "import-output:" + page_id + ":" + digest
        saved = self.state.setdefault("project_imports", {}).setdefault(page_id, {}).setdefault("outputs", {})
        if digest in saved:
            page = self._request("GET", "/pages/" + saved[digest], idempotent=True)
            return ns._safe_https(page.get("url"))
        marker = "产出记录 " + digest
        def discover():
            matches = []
            for block in self._children(page_id):
                if block.get("type") != "child_page" or block.get("child_page", {}).get("title") != title:
                    continue
                children = self._children(ns.notion_id(block["id"]))
                controls = [b for b in children if b.get("type") == "toggle"
                            and ns._plain(b.get("toggle", {}).get("rich_text")) == "记录信息"]
                if any(any(ns._plain(child.get("paragraph", {}).get("rich_text")) == marker
                           for child in self._children(ns.notion_id(b["id"]))) for b in controls):
                    matches.append(self._request("GET", "/pages/" + ns.notion_id(block["id"]), idempotent=True))
            return matches
        body = [_block("paragraph", "本机待发布；此页记录产出入口和验证结果，不包含源文件内容。")]
        body += [_block("paragraph", a["title"] + " · " + a["result"]
                        + ("\n项目内路径：" + a["relative_path"] if a["relative_path"] else ""), url=a["url"])
                 for a in artifacts]
        body.append({"object": "block", "type": "toggle", "toggle": {"rich_text": ns._text("记录信息"),
                     "children": [_block("paragraph", marker)]}})
        page = self._create_unique(operation, discover, {"parent": {"type": "page_id", "page_id": page_id},
            "properties": {"title": {"type": "title", "title": ns._text(title)}}, "children": body})
        saved[digest] = ns.notion_id(page["id"])
        return ns._safe_https(page.get("url"))

    def import_project(self, manifest, task_id, *, parent_page=None, management_home=None):
        manifest = validate_manifest(manifest)
        project = manifest["project"]
        task_id = self._task_id(task_id)
        # setup uses its own lock; it must not be called inside an import lock.
        with self._locked():
            home = self._home(parent_page, management_home)
        self.setup(home)
        meta = self.create_project(task_id, project["name"], project["icon"])
        page_id = meta["page_id"]
        with self._locked():
            try:
                self._migrate(self.config["projects_data_source_id"], EXTRA_PROJECT_SCHEMA)
                self._migrate(self.config["tasks_data_source_id"], {**EXTRA_TASK_SCHEMA,
                    "Managed By": {"select": {"options": [{"name": "AI"}, {"name": "人类确认"}]}}, "Evidence": {"url": {}}})
                self._ensure_events(home)
                properties = {"Goal": {"rich_text": ns._text(project["vision"])},
                    "Current Goal": {"rich_text": ns._text(project["current_goal"])},
                    "Next Step": {"rich_text": ns._text(project["next_step"])},
                    "Summary": {"rich_text": ns._text(project["summary"])},
                    "Latest Progress": {"rich_text": ns._text(project["summary"])},
                    "Health": {"select": {"name": project["health"]}},
                    "Status": {self.config.get("status_property_type", "status"): {"name": project["status"]}}}
                progress = project["progress"]
                if progress:
                    properties.update({"Development Progress": {"number": progress["percent"]},
                        "Progress Source": {"select": {"name": "AI估算"}},
                        "Progress Evidence": {"rich_text": ns._text(progress["evidence"])},
                        "Progress Reviewed": {"date": {"start": progress["reviewed_at"]}}})
                if project["artifacts"]:
                    latest = project["artifacts"][0]
                    latest_url = self._artifact_index(project, page_id)
                    properties.update({"Latest Output": {"rich_text": ns._text(latest["title"])},
                        "Latest Output URL": {"url": latest_url}, "Outcome": {"url": latest_url}})
                page = self._request("PATCH", "/pages/" + page_id,
                    {"icon": project["icon"], "properties": properties}, idempotent=True)
                identity = hashlib.sha256((self.config["dataset_id"] + ":" + page_id).encode()).hexdigest()
                for action in project["manual_actions"]:
                    key = identity + ":task:" + action["id"]
                    self._row(self.config["tasks_data_source_id"], "Task Key", key, {
                        "Name": {"title": ns._text(action["name"])}, "Project": {"relation": [{"id": page_id}]},
                        "Status": {"select": {"name": action["status"]}}, "Managed By": {"select": {"name": "人类确认"}},
                        "Task Key": {"rich_text": ns._text(key)}, "Decision": {"rich_text": ns._text(action["decision"])},
                        "Recommendation": {"rich_text": ns._text(action["recommendation"])},
                        "Impact": {"rich_text": ns._text(action["impact"])}, "Evidence": {"url": action["evidence_url"]}})
                now = self._now().isoformat()
                imported_hash = hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                events = project["history"] + [{"id": "import-" + imported_hash, "title": "整理项目工作台",
                    "occurred_at": now, "result": "整理愿景、当前目标、人工事项和阶段产出，并绑定本机 Token 账本。",
                    "evidence_url": meta.get("url")}]
                preview_history = []
                for event_index, event in enumerate(events):
                    key = identity + ":event:" + event["id"]
                    # Historical facts are immutable on repeated imports.
                    found = self._query(self.config["events_data_source_id"], filter={"property": "Event Key", "rich_text": {"equals": key}})
                    if len(found) > 1:
                        raise ns.NotionError("duplicate_summary")
                    if found:
                        self._verify_created(found[0], {"parent": {"type": "data_source_id",
                            "data_source_id": self.config["events_data_source_id"]},
                            "properties": {"Event Key": {"rich_text": ns._text(key)}}})
                        self.state["uncertain_creates"].pop("import-row:" + key, None)
                    if not found:
                        stored = self._create_unique("import-row:" + key, lambda: self._query(self.config["events_data_source_id"],
                            filter={"property": "Event Key", "rich_text": {"equals": key}}), {
                          "parent": {"type": "data_source_id", "data_source_id": self.config["events_data_source_id"]}, "properties": {
                            "Name": {"title": ns._text(event["title"])}, "Project": {"relation": [{"id": page_id}]},
                            "Event Key": {"rich_text": ns._text(key)}, "Occurred At": {"date": {"start": event["occurred_at"] or now}},
                            **({"Created At": {"date": {"start": now}}} if self._event_created_type == "date" else {}),
                            "Result": {"rich_text": ns._text(event["result"]
                                + ("；本次回填，原完成时间未核实" if not event["occurred_at"] else ""))},
                            "Evidence": {"url": event["evidence_url"]}}})
                    else:
                        stored = found[0]
                    if event_index < len(project["history"]):
                        # The preview must agree with an existing immutable
                        # fact, even when a later manifest reuses its ID with a
                        # changed result. A correction needs a new event ID.
                        props = stored["properties"]
                        result = ns._plain(props["Result"]["rich_text"])
                        backfill = "；本次回填，原完成时间未核实"
                        undated = result.endswith(backfill)
                        preview_history.append({**event,
                            "title": ns._plain(props["Name"]["title"]),
                            "result": result[:-len(backfill)] if undated else result,
                            "occurred_at": None if undated else (props.get("Occurred At", {}).get("date") or {}).get("start")})
                self._layout({**project, "history": preview_history}, page_id)
                meta = self._project(page)
                self.state["projects"][page_id] = meta
                self.state["project_imports"][page_id]["last_imported_at"] = now
                self.state.pop("error", None)
            except ns.NotionError as exc:
                self._remember_error(exc)
                raise
        # Refresh Notion->local management including manual counters after the
        # explicit writes; subsequent scans read the same page and folder ID.
        self.projects(force=True)
        return {"page_id": page_id, "url": meta.get("url"), "task_id": task_id,
                "management_home_id": home, "imported": True,
                "layout_preserved_existing": self.state["project_imports"][page_id].get("layout_preserved_existing", False)}


def prepare_local(manifest, folder, task_root, data_dir, codex_home, *, save=False):
    folder, task_root = Path(folder).expanduser().resolve(), Path(task_root).expanduser().resolve()
    if not folder.is_dir() or folder == task_root or not folder.is_relative_to(task_root):
        _invalid()
    key, name = project_location(str(folder), task_root)
    # The application's accounting unit is a direct child of task_root.
    # Reject an inner repo selection rather than bind an unexpectedly broad folder.
    if key != "folder:" + str(folder):
        _invalid()
    validated = validate_manifest(manifest, folder)
    registry = ProjectRegistry(Path(data_dir), Path(codex_home))
    saved_root = registry.data.get("task_root")
    if saved_root is not None and (not isinstance(saved_root, str)
            or Path(saved_root).expanduser().resolve() != task_root):
        # Imports bind identities; they must not silently regroup past usage.
        _invalid()
    task_id = registry.identify(key, name)
    if save:
        registry.data["task_root"] = str(task_root)
        registry.save()
    return validated, task_id


def main(argv=None):
    parser = argparse.ArgumentParser(description="从经 AI 整理的项目清单幂等导入 Notion，并绑定本机文件夹。")
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parent / "data")
    parser.add_argument("--dry-run", action="store_true", help="只验证清单与产出文件，不联网、不保存绑定")
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.buffer.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            _invalid()
        command = json.loads(raw)
        _shape(command, {"manifest", "folder", "task_root", "codex_home", "parent_page", "management_home"})
        manifest, task_id = prepare_local(command.get("manifest"), command.get("folder"), command.get("task_root"),
            args.data_dir, command.get("codex_home") or Path.home() / ".codex", save=not args.dry_run)
        if args.dry_run:
            result = {"validated": True, "project": manifest["project"], "cloud_written": False}
            print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
            return 0
        workspace = ProjectImportWorkspace(args.data_dir)
        result = workspace.import_project(manifest, task_id, parent_page=command.get("parent_page"),
                                          management_home=command.get("management_home"))
        print(json.dumps({"ok": True, "result": result, "status": workspace.status()}, ensure_ascii=False))
        return 0
    except (ns.NotionError, ValueError, TypeError, OSError) as exc:
        error = exc.public() if isinstance(exc, ns.NotionError) else ns.NotionError("invalid_input").public()
        print(json.dumps({"ok": False, "error": error}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
