# 一键导入已有项目

## 先核实项目事实

读取用户指定项目内有用的 README、愿景／计划文档、发布说明、版本记录和实际成果，不递归读取所有文件。排除密钥、认证文件、个人笔记和原始 Codex 对话。必要时核对实现与测试结果；只有日志中的说法而没有实际成果，不当作已交付。

输出短愿景、当前目标、下一行动、简短阶段进展、正式产出与人工事项。读取已有 Notion 管理页时保留已确定的愿景、状态选项、图标和稳定任务／事件身份，不能凭同名绑定陌生项目。

完成进度使用可核查的里程碑加权估算：每项 `weight > 0`、`completion 0..100`、证据摘要。`percent = round(Σ weight × completion / Σ weight)`；保留评估时间。权重对应整个愿景，不只对应目前列出来的小任务。无足够依据时 `progress: null`，新项目保持未知；导入不会用任务数补进度，也不会清除已存在但本次没有重新评估的进度。

## 项目清单 v1

仅以下白名单字段被接受。清单不含本机绝对路径、凭据、源码片段、原始日志、对话、线程／响应 ID 或官方余额；本机文件夹单独传给配置程序，不上传 Notion。

```json
{
  "version": 1,
  "project": {
    "name": "素材目录",
    "icon": {"type": "emoji", "emoji": "🧭"},
    "vision": "把分散素材整理成可以查找和复用的目录。",
    "status": "开发中",
    "health": "顺利",
    "current_goal": "完成目录入口与分类验收。",
    "next_step": "核对三个分类与对应入口。",
    "summary": "索引已生成，等待分类验收。",
    "progress": {
      "percent": 65,
      "reviewed_at": "2026-10-15T10:00:00+08:00",
      "evidence": "索引已验证；分类验收尚未完成。",
      "milestones": [
        {"name": "生成索引", "weight": 50, "completion": 100, "evidence": "已检查实际索引文件"},
        {"name": "分类验收", "weight": 50, "completion": 30, "evidence": "已完成部分分类抽查"}
      ]
    },
    "artifacts": [
      {"title": "素材索引", "relative_path": "docs/index.md", "result": "提供分类、名称与文件入口。"}
    ],
    "manual_actions": [
      {"id": "accept-index", "name": "验收分类", "status": "待确认", "decision": "分类是否符合使用习惯",
       "recommendation": "先核对三个常用分类", "impact": "决定是否投入正式使用"}
    ],
    "history": [
      {"id": "first-index", "title": "生成索引", "result": "已生成可浏览索引；原完成时间未核实。"}
    ]
  }
}
```

上述只是格式示例，不能照抄成真实项目事实。创建前替换名称、愿景、产出与日期，并核查实际文件。icon 也可使用 `{"type":"external","external":{"url":"https://..."}}`。

`Status` 为计划中／开发中／已发布／暂停／归档。`Health` 支持顺利／有风险／阻塞及既有库的正常／受阻；沿用实际库选项。人工事项 `Status` 是待处理／进行中／待确认／阻塞／已完成／取消，全部以人类确认管理，不把 AI 的执行清单塞进人工事项。`id` 是项目内稳定短键，重复导入更新同一 Tasks，不删除漏传的旧事项。

产出可以提供经核实的 HTTPS `url`，或存在的 `relative_path`；相对路径不可越过项目、不可指向软链接外部目标。没有公开链接的本机成果会得到真实 Notion 产出索引，并明确「本机待发布」，索引记录摘要而非上传文件。可同时提供 URL 与相对路径作核对。

历史可提供 `occurred_at`（带时区的 ISO 时间）和 `evidence_url`；未知原日期就省略，程序使用真实回填时间并注明。历史同 `id` 已存在时保留原事实；更正需新增事件，不能换 Result 覆盖旧记录。每份不同清单另记一条实际导入事件。

## 执行与恢复

定位用量小助手安装源码中的 `widget/project_import.py`。用户当前项目可以位于另一个文件夹，不能误把当前工作区当扫描器目录。程序的 `--help` 提供实际选项；数据目录使用已安装应用的同一 `widget/data`，以复用身份与 Notion 配置。

本机统计以项目根目录的直接子文件夹为粒度。仓库在项目文件夹内部时，选择其上层项目文件夹；不要把内层仓库单独绑定成一个更大的文件夹。先确认应用保存的 task_root，不在导入时暗中改变历史归集规则。

将清单保存到当前项目的 `project-import.json`。构造私有命令 JSON，包含：

```json
{"manifest": {"version":1,"project":{}}, "folder":"本机项目文件夹绝对路径",
 "task_root":"应用已保存的项目根目录", "codex_home":"实际CODEX_HOME或~/.codex"}
```

已有应用配置时不再提供父页，程序复用 `parent_page_id` 与绑定。首次配置：用户已有管理主页时传 `management_home`；没有时传 `parent_page` 作为已授权容器，在其下新建「项目管理」。内部 Integration 必须有可写父页面；连接器能创建工作区级页面时可先创建，再把实际主页面 ID 配给本机。连接器授权不能转交桌面进程，没有本机凭据时明确尚未完成本机连接。

可用 Python 将两个 JSON 合并后从 stdin 调用，避免手工拼接引号或在命令行传秘密：

```python
import json, subprocess
from pathlib import Path

scanner = Path("实际用量小助手源码路径")
project_folder = Path("用户指定的直接子项目文件夹")
command = {
    "manifest": json.loads((project_folder / "project-import.json").read_text()),
    "folder": str(project_folder), "task_root": "应用已保存的项目根目录",
    "codex_home": "实际CODEX_HOME或用户的.codex路径"
}
arguments = [str(scanner / ".venv/bin/python3"), str(scanner / "widget/project_import.py"),
             "--data-dir", str(scanner / "widget/data")]
dry = subprocess.run(arguments + ["--dry-run"], input=json.dumps(command), text=True, capture_output=True)
assert dry.returncode == 0, dry.stdout
actual = subprocess.run(arguments, input=json.dumps(command), text=True, capture_output=True)
print(actual.stdout)
```

`--dry-run` 不联网、不读凭据、不保存 registry 或绑定。实际导入复用系统钥匙串／用户已选密钥来源，只访问当前 Integration 授权页面。所有创建先记录待确认操作；结果不确定时停止创建并重新查询，继续使用同一清单，不换键重试。

已有项目正文和原生数据库视图保留，程序返回 `layout_preserved_existing: true`。随后用连接器读取实际块，维护这次确需更新的愿景／待确认／进展／下一步，不复制框架。新项目正文包含五部分和真实原生双栏；Tasks／Events／Usage Daily 关联同一 Project 页面身份。正式历史仍在 Events，不用正文活动文字替代。正文应直接呈现完整当前目标和近期最多三条历史事实，并保留完整库入口；已有页面按实际块更新，避免只留下数据库链接。Token摘要只能来自同一份已计量本机快照并注明统计截止，无法匹配和读取失败分开表达。

最后刷新项目状态与本机快照，核对 Notion icon、AI 进度来源、当前目标、产出链接、人工数量、本机 folder ID → page ID 的绑定。Token 由本机扫描生成，导入程序不从清单接收任意 Token 数。胶囊前两个入口固定为 Codex 账号和本期 Token；后面最多四个项目位从 Notion 管理项目中按真实本周 Token 降序选取，每次刷新重新排序。零用量管理项目可以补足展示位，同用量按稳定身份排序；其他管理项目及未纳入管理的本机目录归入「其他」。没有可用管理项目时回退本机有用量文件夹前四项。只调整展示，全部保留日志的 Token 和有用量文件夹任务数仍完整计入。导入后不要为项目设置固定优先级。
