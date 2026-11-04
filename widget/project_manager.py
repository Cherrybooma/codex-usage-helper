"""Small workflow CLI for the Markdown project system.

Management facts live in the vault; implementation lives in Documents/project.
Human review is represented as a version-bound feedback note, not inferred from
the absence of objections. This local file workflow is not identity authentication.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
import uuid

from markdown_store import (DEFAULT_DATA, DEFAULT_VAULT, DEFAULT_RULES, MarkdownError,
                            MarkdownWorkspace, management_snapshot, note_digest,
                            read_note, section, write_note)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix):
    return prefix + "-" + uuid.uuid4().hex


def project_directory(vault, project):
    """Resolve one stable project identity to the directory beside its card."""
    path, _, _, _ = locate(vault, project, "project")
    return path.parent


def project_records_directory(vault, project, name):
    folder = project_directory(vault, project)
    target = folder / name
    if target.is_symlink() or not target.resolve().is_relative_to(folder):
        raise MarkdownError("unsafe_path", "项目管理子目录不能通过符号链接离开项目目录。")
    return target


def records(snapshot):
    return [*snapshot.get("projects", []), *snapshot.get("tasks", []),
            *snapshot.get("events", []), *snapshot.get("feedback", []),
            *snapshot.get("artifacts", []),
            *(r for rows in snapshot.get("usage", {}).values() for r in rows)]


def locate(vault, identity, kind=None):
    rows = [r for r in records(management_snapshot(vault))
            if (r.get("id") or r.get("project_id")) == identity]
    if len(rows) != 1:
        raise MarkdownError("not_found", "找不到唯一记录：" + identity)
    row = rows[0]
    path = (Path(vault) / row["path"]).resolve()
    if not path.is_relative_to(Path(vault).resolve()) or path.is_symlink():
        raise MarkdownError("unsafe_path", "记录位置不在管理库中。")
    digest = note_digest(path)
    props, body = read_note(path)
    if digest != note_digest(path):
        raise MarkdownError("revision_conflict", "读取期间笔记被修改，请重试。")
    if kind and props.get("type") != kind:
        raise MarkdownError("wrong_type", "记录类型不符合该操作。")
    return path, props, body, digest


def save_existing(path, props, body, digest):
    old_revision = props.get("revision")
    if isinstance(old_revision, bool) or not isinstance(old_revision, int):
        raise MarkdownError("invalid_revision", "可修改记录需要整数 revision。")
    updated = {**props, "revision": old_revision + 1, "updated": now_iso()}
    write_note(path, updated, body, expected_revision=old_revision, expected_digest=digest)
    return updated


def append_event(vault, project, title, result, evidence="", key=None, occurred_at=None):
    folder = project_records_directory(vault, project, "历史")
    identity = ("e-" + hashlib.sha256((project + ":" + key).encode()).hexdigest()[:32]
                if key else new_id("e"))
    existing = [r for r in management_snapshot(vault)["events"] if r["id"] == identity]
    body = "# " + title + "\n\n## 结果\n\n" + result.strip() + "\n\n## 证据\n\n" + (evidence.strip() or "本次未提供额外证据，不代表功能验收通过。") + "\n"
    if existing:
        _, props, old_body, _ = locate(vault, identity, "event")
        if old_body.strip() != body.strip():
            raise MarkdownError("immutable_event", "同一事件键已有不同内容；更正请使用新的事件键。")
        return {"id": identity, "created": False}
    timestamp = now_iso()
    props = {"type": "event", "id": identity, "project_id": project, "name": title,
             "occurred_at": occurred_at or timestamp, "created_at": timestamp,
             "revision": 1, "evidence": evidence or None}
    write_note(folder / (identity + ".md"), props, body)
    return {"id": identity, "created": True}


def add_task(vault, project, title, kind, acceptance, evidence=None, recommendation="", impact=""):
    vault = Path(vault).expanduser().resolve()
    _, project_props, _, _ = locate(vault, project, "project")
    identity = new_id("t")
    human = kind in ("决策", "验收", "外部行动")
    ready_for_person = human and (kind != "验收" or bool(evidence and evidence.strip()))
    props = {"type": "task", "id": identity, "project_id": project, "name": title,
             "kind": kind, "managed_by": "人类确认" if human else "AI",
             "status": "待确认" if ready_for_person else "待处理", "priority": "中",
             "phase": project_props.get("phase"),
             "revision": 1, "created": now_iso(), "updated": now_iso(),
             "evidence": evidence}
    body = ("# " + title + "\n\n## 事项\n\n" + title + "\n\n## 推荐方案\n\n"
            + (recommendation or "待根据实际情况补充。") + "\n\n## 影响\n\n"
            + (impact or "未作额外时间、费用或范围承诺。") + "\n\n## 验收标准\n\n"
            + acceptance + "\n\n## 证据\n\n" + (evidence or "尚未提交成果。") + "\n")
    path = project_records_directory(vault, project, "任务") / (identity + ".md")
    write_note(path, props, body)
    return {"id": identity, "path": str(path.relative_to(vault))}


def submit_feedback(vault, task_id, target_revision, result, text, source):
    _, task, _, task_digest = locate(vault, task_id, "task")
    if task.get("managed_by") != "人类确认":
        raise MarkdownError("not_human_task", "该任务未提交人工确认。")
    if task.get("kind") == "验收" and task.get("status") != "待确认":
        raise MarkdownError("not_submitted", "验收成果尚未提交，请先准备成果、证据与版本。")
    if isinstance(target_revision, bool) or not isinstance(target_revision, int) or task.get("revision") != target_revision:
        raise MarkdownError("stale_feedback", "反馈版本与当前任务版本不同，请重新查看。")
    if result == "选择" and task.get("kind") != "决策":
        raise MarkdownError("wrong_kind", "选择结果只适用于决策事项。")
    if not text.strip() or not source.strip():
        raise MarkdownError("missing_evidence", "请保留用户实际意见及其来源。")
    identity = new_id("f")
    props = {"type": "feedback", "id": identity, "project_id": task["project_id"],
             "task_id": task_id, "target_revision": target_revision,
             "target_digest": task_digest,
             "result": result, "status": "已提交", "submitted_by": "用户",
             "revision": 1, "created": now_iso(), "updated": now_iso()}
    body = "# 人工反馈\n\n## 反馈\n\n" + text.strip() + "\n\n## 来源\n\n" + source.strip() + "\n"
    write_note(project_records_directory(vault, task["project_id"], "反馈") / (identity + ".md"), props, body)
    return {"id": identity, "task_id": task_id, "status": "已提交"}


def apply_feedback(vault, identity):
    vault = Path(vault).expanduser().resolve()
    feedback_path, feedback, feedback_body, feedback_digest = locate(vault, identity, "feedback")
    if feedback.get("status") == "已应用":
        return {"id": identity, "status": "已应用", "changed": False}
    if feedback.get("status") != "已提交" or feedback.get("submitted_by") != "用户":
        raise MarkdownError("invalid_feedback", "仅应用用户明确提交的反馈。")
    result = feedback.get("result")
    if result not in ("通过", "需修改", "选择"):
        raise MarkdownError("invalid_feedback", "反馈结果必须为通过、需修改或选择。")
    if not section(feedback_body, "反馈").strip() or not section(feedback_body, "来源").strip():
        raise MarkdownError("missing_evidence", "反馈必须保留用户意见及其来源。")
    task_path, task, task_body, task_digest = locate(vault, feedback.get("task_id", ""), "task")
    if task.get("project_id") != feedback.get("project_id"):
        raise MarkdownError("invalid_feedback", "反馈与任务所属项目不一致。")
    already = task.get("last_feedback_id") == identity
    if already and task.get("last_feedback_digest") != feedback_digest:
        raise MarkdownError("feedback_changed", "应用中的反馈被修改；请保留原记录并提交新的反馈。")
    if not already:
        revision = feedback.get("target_revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or task.get("revision") != revision:
            raise MarkdownError("stale_feedback", "反馈对应的是旧版本，未关闭当前任务。")
        if feedback.get("target_digest") and feedback["target_digest"] != task_digest:
            raise MarkdownError("stale_feedback", "任务内容已变化，即使版本号未变也需要重新确认。")
        if task.get("managed_by") != "人类确认":
            raise MarkdownError("invalid_feedback", "该任务不属于待人工处理事项。")
        if result == "选择" and task.get("kind") != "决策":
            raise MarkdownError("wrong_kind", "选择结果只适用于决策事项。")
        task.update(status="已完成" if result in ("通过", "选择") else "待处理",
                    last_feedback_id=identity, last_feedback_digest=feedback_digest,
                    human_result=result, human_reviewed_at=now_iso())
        feedback_link = "[[" + str(feedback_path.relative_to(vault).with_suffix("")) + "]]"
        task_body += "\n## 最近人工反馈\n\n" + feedback_link + " · " + result + "\n"
        save_existing(task_path, task, task_body, task_digest)
    feedback_link = "[[" + str(feedback_path.relative_to(vault).with_suffix("")) + "]]"
    event = append_event(vault, task["project_id"], "收到人工" + result + "反馈",
                         "事项：" + task.get("name", task["id"]) + "。\n\n" + feedback_body,
                         feedback_link, key="feedback:" + identity)
    # The original digest is checked by write_note. A concurrent edit must not
    # be silently labelled as the feedback that was just applied.
    feedback.update(status="已应用", applied_at=now_iso(), event_id=event["id"])
    save_existing(feedback_path, feedback, feedback_body, feedback_digest)
    return {"id": identity, "task_id": task["id"], "status": "已应用", "changed": not already}


def bridge_text(vault, project_path):
    props, _ = read_note(Path(vault) / project_path)
    identity = props.get("id") or "待按管理库核实"
    return ("\n<!-- markdown-project-management:start -->\n"
            "## Markdown 项目管理入口\n\n"
            "工程代码留在当前项目；目标、计划、任务、人工决策/验收、关键历史统一维护在 Obsidian。\n"
            "每轮开始先读取以下文件，遵循其中的轻量维护与验收约定：\n\n"
            "- " + str(DEFAULT_RULES / "AI项目管理约定.md") + "\n"
            "- " + str(DEFAULT_RULES / "数据约定.md") + "\n"
            "- " + str(Path(vault) / project_path) + "\n\n"
            "稳定项目 ID：`" + identity + "`。笔记改名或移动后，先在管理库按该 ID 定位，不因文件名改变新建项目。\n\n"
            "收尾只合并记录可检查成果、重要决定、承诺变化或阻塞；保留稳定 ID、真实证据和人工意见。"
            "不得将 AI 自检冒充用户验收；不在工程中再建第二套持续更新的管理台账。\n"
            "现有工程的代码、测试、产品护栏和 Git 提交要求继续适用。\n"
            "<!-- markdown-project-management:end -->\n")


def _artifact_target(vault, source_root=None, relative_path=None, url=None):
    """Only reference explicit project locations; never copy or serve their bytes."""
    if url:
        if source_root is not None or relative_path is not None:
            raise MarkdownError("invalid_artifact", "本机成果和 HTTPS 入口请选择一种。")
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme == "https" and bool(parsed.hostname)
                     and parsed.username is None and parsed.password is None
                     and (parsed.port is None or 1 <= parsed.port <= 65535))
        except ValueError:
            valid = False
        if (not valid or len(url) > 8192
                or any(c.isspace() or ord(c) < 32 for c in url)):
            raise MarkdownError("invalid_artifact", "成果远程入口必须是无凭据的有效 HTTPS 地址。")
        return {"source_url": url, "target_url": url, "local_only": False,
                "availability": "远程引用，未检查可达性"}, "url:" + url
    if source_root is None or relative_path is None:
        raise MarkdownError("invalid_artifact", "本机成果需要明确工程根目录和工程内相对路径。")
    root = Path(source_root).expanduser().resolve()
    relative = Path(relative_path)
    if (not root.is_dir() or relative.is_absolute() or ".." in relative.parts
            or not str(relative_path).strip()
            or any(ord(c) < 32 for c in str(relative_path))):
        raise MarkdownError("unsafe_artifact", "成果必须引用工程目录内的有效相对路径。")
    target = (root / relative).resolve()
    if not target.is_relative_to(root) or not target.exists():
        raise MarkdownError("unsafe_artifact", "成果不存在，或通过符号链接离开了工程目录。")
    resolved_vault = Path(vault).resolve()
    if root == resolved_vault or root.is_relative_to(resolved_vault) or resolved_vault.is_relative_to(root):
        raise MarkdownError("mixed_locations", "成果工程与管理库必须分开存放。")
    normalized = str(target.relative_to(root))
    return {"source_root": str(root), "relative_path": normalized,
            "target_url": target.as_uri(), "local_only": True,
            "availability": "本机引用，登记时存在"}, "local:" + str(root) + ":" + normalized


def register_artifact(vault, project, title, *, source_root=None, relative_path=None,
                      url=None, version=None, summary="", scope="", kind="document"):
    """Publish a small pointer to real project output, preserving user prose."""
    vault = Path(vault).expanduser().resolve()
    if not title.strip() or len(title) > 160 or any(ord(c) < 32 for c in title):
        raise MarkdownError("invalid_artifact", "请提供简短、可读的成果标题。")
    if kind not in ("code", "document", "app", "url", "other"):
        raise MarkdownError("invalid_artifact", "成果类型无效。")
    folder = project_records_directory(vault, project, "成果")
    target, reference = _artifact_target(vault, source_root, relative_path, url)
    identity = "a-" + hashlib.sha256((project + ":" + reference).encode()).hexdigest()[:32]
    existing = next((row for row in management_snapshot(vault)["artifacts"] if row["id"] == identity), None)
    managed = {"type": "artifact", "id": identity, "project_id": project,
               "name": title.strip(), "kind": kind, "source_version": version,
               "summary": summary.strip(), "verified_scope": scope.strip(),
               "status": "已登记", **target}
    start, end = "<!-- pm-artifact:start -->", "<!-- pm-artifact:end -->"
    generated = (start + "\n## 查看成果\n\n[打开成果](" + target["target_url"] + ")\n\n"
                 + ("这里未复制成果内容；原文件保留在工程目录，需要在能访问该目录的 Mac 上打开。" if target["local_only"] else "这里只保存网址，尚未确认该网址能否打开，也不表示成果已验收。")
                 + "\n\n版本：" + (version or "未指定") + "\n\n说明：" + (summary.strip() or "未补充")
                 + "\n\n已检查与未检查：" + (scope.strip() or "尚未验证成果内容；保存入口不表示验收通过。") + "\n" + end)
    if existing:
        path, old, body, digest = locate(vault, identity, "artifact")
        pattern = re.escape(start) + r".*?" + re.escape(end)
        updated_body = re.sub(pattern, lambda _: generated, body, flags=re.S) if start in body and end in body else body.rstrip() + "\n\n" + generated + "\n"
        updated = {**old, **managed}
        if updated == old and updated_body == body:
            return {"id": identity, "path": str(path.relative_to(vault)), "created": False, "changed": False}
        save_existing(path, updated, updated_body, digest)
        return {"id": identity, "path": str(path.relative_to(vault)), "created": False, "changed": True}
    safe_name = re.sub(r'[/\\:\x00-\x1f]', "-", title).strip(" .")[:100] or "成果"
    path = folder / (safe_name + "-" + identity[-8:] + ".md")
    if path.exists():
        raise MarkdownError("artifact_conflict", "同名位置已有其他记录，请保留原文件并换一个成果标题。")
    now = now_iso()
    write_note(path, {**managed, "cssclasses": ["pm-guide"], "revision": 1, "created_at": now, "updated": now},
               "# " + title.strip() + "\n\n" + generated + "\n")
    return {"id": identity, "path": str(path.relative_to(vault)), "created": True, "changed": True}


def install_bridge(folder, vault, project_path):
    path = Path(folder) / "AGENTS.md"
    text = path.read_text() if path.exists() else "# 项目工作规则\n"
    addition = bridge_text(vault, project_path)
    pattern = r"\n<!-- markdown-project-management:start -->.*?<!-- markdown-project-management:end -->\n?"
    updated = re.sub(pattern, lambda _: addition, text, flags=re.S) if "<!-- markdown-project-management:start -->" in text else text.rstrip() + "\n" + addition
    if updated != text:
        path.write_text(updated)
    return str(path)


def create_project(vault, data_dir, code_root, name, goal, icon, existing=False):
    if not name.strip() or len(name) > 80 or name in (".", "..") or re.search(r'[/\\:\x00-\x1f]', name):
        raise MarkdownError("invalid_name", "项目名必须是文稿目录下一层文件夹名称。")
    root = Path(code_root).expanduser().resolve()
    folder = (root / name).resolve()
    resolved_vault = Path(vault).resolve()
    if root == resolved_vault or root.is_relative_to(resolved_vault) or folder == resolved_vault or folder.is_relative_to(resolved_vault) or resolved_vault.is_relative_to(folder):
        raise MarkdownError("mixed_locations", "工程与项目管理库必须分开存放。")
    if folder.parent != root or not root.is_dir():
        raise MarkdownError("invalid_folder", "项目必须在指定文稿根目录的直接下一层。")
    if folder.exists() and not existing:
        raise MarkdownError("folder_exists", "工程目录已存在；确认使用已有工程时添加 --existing。")
    folder.mkdir(exist_ok=True)
    workspace = MarkdownWorkspace(data_dir, vault=vault)
    workspace.setup(str(vault), str(root))
    task_id = workspace._task_id({"folder": str(folder), "task_root": str(root)})
    bound = workspace.config.get("bindings", {}).get(task_id)
    if bound:
        project = next(p for p in workspace.projects() if p["project_id"] == bound)
    else:
        project = workspace.create({"name": name, "goal": goal, "icon": icon,
                                    "folder": str(folder), "task_root": str(root)})
    install_bridge(folder, vault, project["path"])
    return {"project": project, "folder": str(folder), "management_path": project["path"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    sub.add_parser("overview")
    export = sub.add_parser("export")
    export.add_argument("--output", type=Path)
    create = sub.add_parser("new-project")
    create.add_argument("--name", required=True)
    create.add_argument("--goal", required=True)
    create.add_argument("--icon", default="📁")
    create.add_argument("--code-root", type=Path, default=Path.home() / "Documents")
    create.add_argument("--existing", action="store_true")
    task = sub.add_parser("add-task")
    task.add_argument("--project", required=True)
    task.add_argument("--title", required=True)
    task.add_argument("--kind", choices=("执行", "决策", "验收", "外部行动"), default="执行")
    task.add_argument("--acceptance", required=True)
    task.add_argument("--evidence")
    task.add_argument("--recommendation", default="")
    task.add_argument("--impact", default="")
    event = sub.add_parser("record-event")
    event.add_argument("--project", required=True)
    event.add_argument("--title", required=True)
    event.add_argument("--result", required=True)
    event.add_argument("--evidence", default="")
    event.add_argument("--key")
    artifact = sub.add_parser("register-artifact", help="登记成果引用，不复制工程产出")
    artifact.add_argument("--project", required=True)
    artifact.add_argument("--title", required=True)
    artifact.add_argument("--source-root", type=Path)
    artifact.add_argument("--relative-path")
    artifact.add_argument("--url")
    artifact.add_argument("--kind", choices=("code", "document", "app", "url", "other"), default="document")
    artifact.add_argument("--version")
    artifact.add_argument("--summary", default="")
    artifact.add_argument("--scope", default="")
    feedback = sub.add_parser("submit-feedback")
    feedback.add_argument("--task", required=True)
    feedback.add_argument("--revision", type=int, required=True)
    feedback.add_argument("--result", choices=("通过", "需修改", "选择"), required=True)
    feedback.add_argument("--text", required=True)
    feedback.add_argument("--source", required=True, help="用户明确意见的来源，不能由AI推测")
    apply = sub.add_parser("apply-feedback")
    apply.add_argument("--id", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8766)
    args = parser.parse_args(argv)
    vault = args.vault.expanduser().resolve()
    try:
        if args.command == "serve":
            from management_api import main as serve_main
            return serve_main(["--vault", str(vault), "--data-dir", str(args.data_dir), "--port", str(args.port)])
        if args.command in ("check", "overview", "export"):
            snapshot = management_snapshot(vault, args.data_dir)
            if args.command == "check":
                result = {"ok": True, "projects": len(snapshot["projects"]), "tasks": len(snapshot["tasks"]),
                          "events": len(snapshot["events"]), "feedback": len(snapshot.get("feedback", []))}
            elif args.command == "overview":
                from management_api import overview_data
                result = {"schema_version": 1, **overview_data(snapshot)}
            else:
                result = snapshot
                if args.output:
                    args.output.parent.mkdir(parents=True, exist_ok=True)
                    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
                    result = {"ok": True, "output": str(args.output)}
        elif args.command == "new-project":
            result = create_project(vault, args.data_dir, args.code_root, args.name, args.goal, args.icon, args.existing)
        elif args.command == "add-task":
            result = add_task(vault, args.project, args.title, args.kind, args.acceptance, args.evidence, args.recommendation, args.impact)
        elif args.command == "record-event":
            result = append_event(vault, args.project, args.title, args.result, args.evidence, args.key)
        elif args.command == "register-artifact":
            result = register_artifact(vault, args.project, args.title, source_root=args.source_root,
                                       relative_path=args.relative_path, url=args.url, version=args.version,
                                       summary=args.summary, scope=args.scope, kind=args.kind)
        elif args.command == "submit-feedback":
            result = submit_feedback(vault, args.task, args.revision, args.result, args.text, args.source)
        elif args.command == "apply-feedback":
            result = apply_feedback(vault, args.id)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (MarkdownError, OSError, ValueError) as error:
        public = error.public() if isinstance(error, MarkdownError) else {"code": "operation_failed", "message": str(error)}
        print(json.dumps({"ok": False, "error": public}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
