# Notion 项目工作台与 Codex 投入账本

第一版只支持 Codex。Notion 是项目管理入口，管理项目目标、本周目标、下一步、状态、头像与成果，并用关联任务库组织行动项。本机完成 Token 计量，把绑定项目的每日摘要写入关联账本。用量是项目投入的一部分，不能代替进度或成果。未连接 Notion、未绑定项目或同步失败都不改变本地 Token 总数与占比分母。

当前实现提供工作台数据库建立、项目与任务计数读取、带图标的显式创建/绑定、每日摘要替换同步和本地失败队列。自动化检查使用隔离的状态化 API 模拟服务，不访问真实账号；真实联调需要在本机连接用户自己的工作区。对话内 Notion 插件授权不能转交给桌面后台，模拟测试通过不代表其他机器的持续同步已经验证。

Codex 通过 [notion-project-workbench 技能](../skills/notion-project-workbench/SKILL.md)在用户安排的任务中维护项目与行动项，并把实际推进追加为开发事件。桌面后台只刷新管理缓存和同步用量。尚未配置定时 Notion Custom Agent，不宣称已经持续自动分析项目进度。首页组织和 AI 维护约定见 [项目管理说明](project-management.md)。

应用默认本机模式，Notion 为可选项。在“项目与 Notion 设置…”中关闭“启用 Notion 同步”时，连接表单隐藏，后台和手动同步均在读取凭据与联网之前停止；已有绑定、项目缓存及每日摘要保留，本地计量继续。关闭不会撤销此前已完成的云端写入，正在执行的操作完成前开关不可更改。

## 首次连接

1. 在 Notion 建立一个用于项目管理的父页面。
2. 建立内部连接，授予读取、插入与更新内容能力。将这个父页面共享给该连接。
3. 在应用的 Notion 设置中开启“启用 Notion 同步”，保存连接凭据，或明确选择自己的 Pages 密钥文件；再填写父页面链接或页面 ID。支持 ⌘V 粘贴，加载状态不禁用输入框。
4. 点击“初始化项目工作台”，建立或复用 Projects、Tasks 和 Usage Daily。已有 Tasks 数据源配置会复用原库；旧连接未配置 Tasks 时仍可读取项目和同步用量。
5. 为项目在 Notion 页面顶部设置 icon；从本地任务中选择“绑定已有项目”，或在应用里选择 emoji / HTTPS Logo 后创建并绑定。不自动将所有本地文件夹上传或批量建立项目。

默认凭据由应用保存到 macOS Keychain 的专用项目：服务 `local.codex.usage-widget.notion`，账号 `integration`。也可以在设置中明确选择一个包含 Notion Token 的 `.pages` 文件作为来源；后台仅从该文件在内存中提取唯一 Token，不搜索其他文件、不生成明文副本、不上传 Pages 文件。文件中已有的 Token 仍需要用户自行保护；应用只将提取的 Token 用于 Notion 官方认证。来源文件移动或解析失败时暂停连接，不静默改用旧凭据。

文件来源只在本地 `app-settings.json` 保存路径，不保存 Token。选择“改用钥匙串”后恢复专用钥匙串模式。Python 不读取 Codex 认证文件、浏览器、其他 Keychain 项目或认证环境变量；凭据不放入 JSON、命令行参数、仓库和日志，也不通过聊天收集。后台状态与胶囊渲染仅读取本地缓存，不读取来源文档或探测 Keychain。

文件来源支持含 `Index/Document.iwa` 的 ZIP 格式 Pages 文档；仅允许一个可识别的 Notion Token。旧目录式文档、多个不同 Token 或超过读取上限的文件会拒绝连接，不猜测内容。

第一次同步从连接时的北京时间自然周周一开始。例如 2026-10-13 连接，起点为 2026-10-12。这个起点持久保存，不会在下周自动把前周失败的队列抛弃。数据库位置一经建立保持固定，避免重新填父页面后误创建另一套库。

## 项目、任务与关联账本

项目库的字段如下。旧项目库没有新增的管理属性时仍兼容，应用不会静默补写目标、日期或管理结论。

| 属性 | 用途 |
| --- | --- |
| Name / 页面 icon | 项目名称与胶囊中的同一头像身份 |
| Status | 计划中、开发中、已发布、暂停、归档 |
| Goal / Weekly Goal / Next Step | 项目目标、本周目标、具体下一步 |
| Priority / Health | 高／中／低，以及正常／有风险／受阻 |
| Development Progress | 明确维护的 0–100 开发进度，未知时留空 |
| Target Date / Last Reviewed | 交付日期、最近复盘日期；未知时留空 |
| Summary / Outcome | 最近项目摘要与成果链接 |
| Binding Key | 内部创建去重依据，不用于首页展示 |

Status 支持 Notion 的 status 属性，以及已有项目库使用同样选项的 select 属性；建立或重新连接时验证结构。名称、管理信息和页面图标从 Notion 读取。状态不是 agent 的实时运行状态，也不会重写过去消耗。

### 项目图标

新建项目时，应用要求一个 emoji 或有效 HTTPS Logo，随同创建请求直接写入页面 icon。绑定已有项目前，应用重新检查远端页面；没有可显示图标时拒绝绑定，并提示先在 Notion 设置。读取已有页面时还支持 Notion 文件图标和自定义 emoji 的 HTTPS 图像地址。

Notion 页面 UI 本身不能强制每个项目必须有 icon，这项规则由本应用的创建／绑定流程验证。人或其他客户端之后删除图标时，应用会在详情提示补充，胶囊显示文件夹符号；已有绑定和历史统计保留。不会用名称首字、随意图标或用量占比冒充项目头像。

### Tasks 数据源

工作台的任务库通过 `Project` 关系关联项目。当前后台只读任务数，不创建或修改任务。新建任务库有以下字段：

| 属性 | 用途 |
| --- | --- |
| Name / Project | 行动项名称与所属项目 |
| Status | 待处理、进行中、待确认、阻塞、已完成、取消 |
| Priority / Due | 高／中／低和计划日期 |
| Managed By | AI 或人类确认 |
| Acceptance / Evidence | 验收标准与可核查结果链接 |

任务源配置为本地 `notion-config.json` 中可选的 `tasks_data_source_id`。建立的新工作台会配置此项；已有连接未配置时不显示任务计数，不影响项目读取和用量同步。任务读取要求 `Project` 为指向当前 Projects 数据源的 relation，`Status` 为上述选项的 select；旧任务库可不含新增管理字段。

待办数包含待处理、进行中、待确认与阻塞；受阻数是待办中的子集，不能再加一次形成总任务数。已完成只统计已完成，取消和已归档任务不计。任务读取失败、未知状态、截断关系或缓存过期时保留未知状态，不把缺失数据显示为零，不从 Token 推断任务完成。

胶囊内环优先使用合法的 Development Progress，未设置时可使用 `已完成 / (已完成 + 待办) × 100`，并标为“行动完成率”。只有任务计数完整、可用且分母非零时才计算；它不表示整个项目的完成度。外环仍是本周 Token 占比，与开发进度分别解释。

### Events 开发历史

AI 项目管理工作流还使用 Events，将开发过程与 Tasks 当前状态分开。应用“初始化项目工作台”建立或复用 Projects、Tasks、Usage Daily；Events 由技能在明确的项目管理任务中读取和维护，当前桌面后台不拉取完整事件列表。

| 属性 | 用途 |
| --- | --- |
| Name / Project / Tasks | 实际推进标题、项目关系与相关任务关系 |
| Occurred At / Created At | 发生时间与 Notion 实际录入时间 |
| Result / Evidence | 实际动作、结果、未核实部分与证据链接 |
| Event Key | 同一次推进的稳定去重标识 |

每次实际推进必须追加 Events；顺序重试先查询并复用已有记录。已完成 Tasks 保留，当前行动视图可隐藏已完成项，但不能删除它们或覆盖历史事件。需要更正时追加更正事件。原完成时间未知的旧证据记录为“本次回填”，使用实际回填时刻并明确“原完成时间未核实”，不编造过去日期。

Events 按同项目单写者顺序维护，创建后按 Event Key 复查。该键是没有唯一约束的富文本，先查后建不保证并发唯一。已有多条同键记录或并发／创建结果不确定时暂停重复创建，标为待核实并保留全部历史，不自行删除或合并。

首页固定为愿景、待人工确认、当前进展与下一步、历史开发事件、每项目 Token 投入，愿景在首位。人工区主视图只筛选待确认；普通阻塞在当前行动或风险，需人的阻塞形成具体确认项。单项目页面直接展示阶段、完成事实和 AI 下一行动，不重复同名项目卡。历史默认显示日期排序的 Events 列表，另有可展开原生时间线；胶囊目前读取项目 Summary 和任务计数，完整事件在 Notion 中查看。

每日用量库字段如下：

| 属性 | 含义 |
| --- | --- |
| Name / Project / Day / Provider | 日期、项目关系与固定来源 Codex |
| Included / Counted Tokens | 当前绑定是否有效，以及当前可汇总的 Token；周／月统计使用此列 |
| Total Tokens | 原始记录的总数规则；不再把缓存输入和推理输出加一次 |
| Input Tokens / Output Tokens | 输入、输出细分 |
| Cached Input Tokens / Cache Write Input Tokens / Reasoning Output Tokens | 原始计量中的细分项，不额外加入总数 |
| Response Records / Legacy Updates | 按响应记录覆盖与旧累计更新数量 |
| Sessions | 当日去重会话数；跨日不能直接相加为周会话数 |
| Quality | 按响应记录、旧累计估算、部分未读取或未核实 |
| Data Scope | 当前保留日志，或本次没有重新复算的此前已观测摘要 |
| Statistics Until / Statistics Until ISO / Synced At | 统计截止日期、精确 UTC 文本与最后一次实际写入时间；日期属性在部分连接中只有分钟精度 |
| Dataset / Summary Key / Schema Version | 匿名来源数据集、唯一摘要键与结构版本 |

周／月汇总使用 `Counted Tokens`，或过滤 `Included = true`。`Total Tokens` 保留该条观测的原数值。改绑到另一项目后，完整来源快照还会核对服务端已存在记录，本地同步状态丢失或通过插件创建的初始记录也能正确处理。旧分配会取消 Included 并将 Counted Tokens 置零，原数值保留审计；新分配使用同样的单次用量。重新绑定回原页面可以恢复该分配，不能把两边都当成新的消耗。远端更新没有跨行事务，同步失败时云端可能暂时不完整；本地统计和队列保留。

一条摘要的身份为“匿名来源数据集 × Notion 项目页面 ID × 上海日期 × Codex”。同一项目当天再次扫描时替换原有绝对数值，不将前后两次扫描值相加。不同 Codex home/来源数据集的摘要不会互相覆盖；后续多设备汇总仍需解决重复日志问题，当前不宣称已完成跨设备去重。

首次绑定靠稳定本地任务 ID 与 Notion 页面 ID，之后 Notion 项目改名不会改变身份。多个本地任务可以绑定同一 Notion 项目：Token 和记录数合并，当日会话使用仅在本机保留的哈希键取并集。若调用方没有提供去重依据，会话数留空并注明“会话数未跨任务去重”，不能直接相加冒充正确值。哈希会话键不会写入 Notion 或同步队列。

没有 Token/质量变化时不重复更新云端；新的扫描截止时间本身不触发写入，因此云端 `Statistics Until` 表示最后实际传输的截止时间。较旧扫描不能覆盖已成功同步的较新扫描。历史修正允许降低数值，不假定 Token 只能增加。

`Data Scope` 必须区别两种情况：

- “本机当前保留记录；不代表完整账户用量或最终账单”。
- “包含此前已观测摘要；部分历史本次未重新复算，不是当前保留记录的全量统计”。

原始日志、代码、对话、工具输出、线程 ID、响应 ID 和绝对本地路径都不上传。上传项目名称仅发生在用户明确创建项目时。Notion 官方额度不写入每日 Token 摘要，不能从本地 Token 倒推出订阅剩余百分比。

## 离线、重试和写入不确定

本地 `notion-config.json` 保存数据源 ID、匿名数据集、首次同步起点与绑定；`notion-state.json` 保存项目缓存、待同步摘要及已同步指纹。文件权限为 0600，原子替换。首次连接时，系统钥匙串可能要求授权读取此专用项目；连接设置给用户保留授权时间。

桌面后台 CLI 的 `--sync-notion` 同样要求 `app-settings.json` 中的 `notion_sync_enabled` 为布尔 `true`。`--force-sync` 只跳过客户端同步间隔，不能绕过本机模式或服务端重试等待。直接调用 `notion_sync.py` 是独立开发接口，显式调用联网命令仍会尝试连接；日常使用应通过桌面应用或后台 CLI。

网络操作有跨进程独占锁；胶囊缓存读取不等待该锁。

项目缓存约 5 分钟刷新；超过 10 分钟或读取失败标为旧缓存。远程页面被删除或移入垃圾箱时保留本地统计与历史，不继续写入该页面。Notion 查询处理分页。

HTTP 429、529 按官方 `Retry-After` 有限重试。较长等待保存在本地，让后续后台轮次到期重试，手动刷新也不能绕过服务器指定时间。401、403 和被禁用的连接不在单次请求内反复重试。读取与绝对值更新可有限重试；创建请求的网络错误或 5xx 不盲目重复。

创建前先持久记录待确认操作。若 Notion 返回“已提交资源 ID”，读取该资源；否则按摘要键重新查询。确认找到唯一记录后继续更新。若未找到，暂停该次创建并持续查询，避免“服务端已写入但暂时不可见”制造副本。官方禁用 API 后自动同步保持暂停，只有显式重新连接或手动重试会再请求；离线仍保留本地事实。极端情况下需要用户在 Notion 核查；不能仅凭客户端超时判断写入失败。已经存在多个相同摘要键时也暂停写入，不任意覆盖其中一条。

这套唯一键是应用层约定，Notion 数据库没有唯一约束。一个本地来源的数据操作通过本地锁串行；不支持不同电脑共用并发写入同一数据集配置。

## 开发接口

```python
from widget.notion_sync import NotionWorkspace

workspace = NotionWorkspace(data_dir)
workspace.status()                       # 只读本地，不读凭据或联网
workspace.project_for_task(task_id)       # 只读项目缓存
workspace.setup(parent_page_url_or_id)    # 显式建立或复用三库工作台
workspace.projects(force=True)            # 后台刷新远程项目
workspace.cached_projects()              # 只读本地管理缓存
workspace.create_project(task_id, name, {"type": "emoji", "emoji": "📦"})
                                         # 显式创建带图标的项目并绑定
workspace.bind(task_id, page_id)
workspace.unbind(task_id)
workspace.sync(daily_rows, now=utc_end, force=False)
# 桌面后台传完整来源快照，支持改绑后的旧分配停计：
workspace.sync(all_saved_rows, now=utc_end, complete_dataset_ids=[dataset_id], complete_until=utc_end)
```

`complete_dataset_ids` 只用于明确声明已传完整快照的来源，普通增量调用不要传；较旧的完整快照不会停计较新的分配。更改已绑定账本的项目根目录前，需先解除绑定并重新归集，或使用独立 data-dir；保留的旧摘要不能冒充已按新规则复算。

`daily_rows` 使用已计量的数据，不调用 Notion 重新计算 Token：

```json
{
  "task_id": "本地稳定身份，不上传",
  "dataset_id": "匿名 UUID",
  "provider": "codex",
  "day": "2026-10-13",
  "total_tokens": 100,
  "input_tokens": 60,
  "output_tokens": 25,
  "cached_input_tokens": 30,
  "cache_write_input_tokens": 0,
  "reasoning_output_tokens": 10,
  "request_records": 2,
  "legacy_updates": 0,
  "sessions_count": 1,
  "session_keys": ["仅在本机取并集的哈希"],
  "quality": ["response_records_verified"],
  "statistics_until": "2026-10-13T08:00:00+00:00",
  "observed_history": false
}
```

允许的 `quality` 是 `response_records_verified`、`legacy_estimated`、`partial_unreadable`、`unknown`。未知值只标成未核实，不上传调用方任意文本。计数必须为非负整数且不大于 JavaScript/Notion 可精确表达的整数边界；同一批不能重复传入相同任务和日期。多个当前可复算任务合并到同一项目时需使用相同统计截止时间。若含本次没有重新复算的此前已观测摘要，可以与新截止的其他任务合并，云端采用其中最近的截止时间，并注明部分历史本次没有重新复算；每条原观测截止仍由本地账本保留。

设置界面可通过 stdin JSON 命令调用，不传凭据：

```sh
python3 widget/notion_sync.py --data-dir /your/local/data
```

stdin 命令为 `status`、`projects`、`setup`、`bind`、`unbind`、`create_project` 或 `sync`，例如 `{"command":"setup","parent_page":"父页面 ID 或链接"}`。`create_project` 必须提供 `icon`，例如 `{"type":"emoji","emoji":"📦"}`。返回 JSON 包含 `ok`、`result`、`status`，失败只返回固定的安全错误文本，不输出服务器原始报错、请求体或堆栈。

`status.connected` 是最近一次实际连接操作确认的状态，不能当成实时网络连通性。`state` 为 disconnected、ready 或 error；包含 `message`、`projects_url`、`usage_url`、`last_synced_at`、缓存项目与队列数量；任务读取问题独立记录为 `tasks_error`。没有连接时本地统计仍可正常使用。

测试命令：

```sh
python3 -m unittest -v widget/test_notion_sync.py widget/test_notion_credentials.py
```

## 官方 API 依据

实现固定 `Notion-Version: 2026-03-11`，使用数据库的 `initial_data_source`，以及 `/data_sources/{id}/query`。关系引用数据源 ID，记录身份使用页面 ID，不从展示 URL 推导身份。[版本说明](https://developers.notion.com/reference/versioning)、[创建数据库](https://developers.notion.com/reference/create-a-database)、[查询数据源](https://developers.notion.com/reference/query-a-data-source)、[属性与关系结构](https://developers.notion.com/reference/property-object)。

重试依据官方对 429、529、权限错误，以及“写入成功但响应失败”的说明。[请求限制与写入重查](https://developers.notion.com/reference/request-limits)。
