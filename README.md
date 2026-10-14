# 用量小助手 · Codex Usage Helper

**在 Notion 管理项目目标、行动与开发历史，在 Mac 胶囊查看项目进展和 Token 投入。**

一个连接 Notion 的 macOS 项目看板，也包含在本机运行的 Codex 用量扫描器。项目目标、下一步、管理状态和任务计数放在详情里；Token 账本记录每个项目的投入。胶囊平时只显示图标，鼠标悬停或点击后再展开信息。

![桌面组件界面示意，所有数值均为演示数据](docs/widget-preview.png)

> 图中为演示数据。项目 Token 来自本机保留的记录；官方剩余额度是另一种数据，二者不能直接换算。

## 可以做什么

- **五部分项目工作台**：愿景、待人工确认、当前进展与下一步、历史开发事件、每项目 Token 投入。使用 Notion 原生项目与任务视图，已完成任务和开发事件保留。
- **项目管理信息**：读取目标、本周目标、下一步、状态、优先级、健康度和日期；关联 Tasks 数据源后显示待办、受阻和已完成数量。
- **纯图标小胶囊**：常态只显示 Codex、账本与项目图标；项目头像来自 Notion 页面自身的 icon。悬停或点击后查看完整项目名与详情卡。
- **三种视觉样式**：液态玻璃、Pulse 暗色、经典白色；项目外环表达本周 Token 占比，内环表达有依据的开发进度或行动完成率，缺数据只显示轨道。
- **按项目看投入**：桌面组件按 `Documents` 下一级文件夹名称归集本机任务；命令行扫描器还会识别工作目录和 Git 仓库。Token、占比、会话和响应数放在展开详情中。
- **三种展示方式**：顶部横条、右侧竖条、桌面小框分别开关，也可以同时开启。默认在桌面层，普通窗口可以盖住它。
- **额度详情**：通过官方 Codex app-server 的只读接口读取当前窗口剩余百分比及重置时间；保存观测后显示本周已记录累计和超额情况。
- **关联的项目与账本**：Notion 项目作为管理入口；本机把绑定项目的每日用量摘要写入关联账本，离线保留项目缓存和本地统计。
- **独立周期核算**：按上海自然周记录、按确认的会员续费账期核算月投入；Notion「周期与额度」页面单独保存周期账本与真实额度窗口，不放进具体项目管理。
- **终端与导出**：按项目、会话、模型、日期汇总，并输出完整 JSON。

日志扫描和计量在本机进行。启用 Notion 后，绑定项目的每日摘要和已配置独立周期页的计数、项目／模型汇总、额度观测与统计质量会发送到指定工作区；原始日志不会上传。组件读取官方额度时会使用官方客户端的账户连接，但不会把会话日志、项目目录或统计报告发送给它。

AI 管理通过 [notion-project-workbench 技能](skills/notion-project-workbench/SKILL.md)在获授权的任务中维护项目与行动项，每次实际推进追加带时间、结果、证据和任务关系的 Events。尚未配置持续运行的 Notion Custom Agent。桌面后台只刷新管理信息缓存、计量并同步用量，不根据 Token 自动判断项目进度或完成情况。首页结构、管理约定及官方参考见 [项目管理说明](docs/project-management.md)。

## 快速开始：macOS 桌面组件

需要 **macOS 15+、Python 3.10+**，以及能提供 macOS 26 SDK 的 Swift/Xcode Command Line Tools。源码包含 Liquid Glass API，因此构建也需要对应 SDK；macOS 15 运行时使用毛玻璃回退。

在终端中克隆并运行：

```sh
git clone https://github.com/Cherrybooma/codex-usage-helper.git
cd codex-usage-helper
python3 -m venv .venv
.venv/bin/python3 -m pip install -r requirements-optional.txt
sh widget/build.sh
sh widget/run.command
```

`requirements-optional.txt` 提供 zstd 压缩日志支持和时区数据库；普通 JSONL/gzip 的扫描不需要第三方依赖。

额度功能还需要本机安装且登录官方 Codex CLI 或桌面应用；没有可用接口时，Token 扫描仍可使用，额度显示未取得或带标记的旧缓存。

启动后，在菜单栏或组件右键菜单中选择“顶部横条 / 右侧竖条 / 桌面小框”。悬停展开、点击保持，再点收起；点击其他模块切换详情，Esc 或外部点击关闭详情。在菜单栏选择退出才会停止后台刷新。

构建产物位于 `widget/build/用量小助手.app`。这是本地 ad-hoc 签名的开发应用，尚未提供可独立安装的发行包；请保留项目目录和 `.venv`，应用目前依赖其中的 Python 数据层。

项目根目录和 Notion 连接可在菜单栏的“项目与 Notion 设置…”中配置。新建项目时必须选择 emoji 或 HTTPS Logo，绑定已有项目前会检查其 Notion 页面图标。连接凭据默认保存在系统钥匙串，也可明确选择自己的 Pages 密钥文件作为来源；应用不保存明文副本。首次同步从本周开始，不会自动为所有文件夹创建项目。见 [Notion 说明](docs/notion.md)。

更完整的运行说明见 [桌面组件文档](widget/README.md)。

## 只使用命令行扫描器

扫描器可在安装 Python 3.10+ 的 macOS、Linux、Windows 上使用，不需要构建桌面应用。

```sh
# 最近滚动 7 天，默认按项目汇总
python3 codex_usage.py

# 分别按会话、模型、日期查看
python3 codex_usage.py --group-by session
python3 codex_usage.py --group-by model
python3 codex_usage.py --group-by day --timezone Asia/Shanghai

# 导出完整报告
python3 codex_usage.py --format json --output report.json

# 全部保留历史
python3 codex_usage.py --all-time

# 使用固定时间窗口，终点不包含在内
python3 codex_usage.py --since 2026-10-01 --until 2026-10-08 --timezone Asia/Shanghai

# 同一 Git 仓库的 worktree 分开显示
python3 codex_usage.py --separate-worktrees
```

Windows 可将 `python3` 替换为 `py -3`。优先使用 `CODEX_HOME`，否则扫描 `~/.codex/sessions` 与 `~/.codex/archived_sessions`。参数以 `python3 codex_usage.py --help` 为准。

## 怎么统计

| 数据 | 统计方法 |
| --- | --- |
| 新版日志 | 累计单次 `token_usage_record.usage`，按 `response_id` 去重；累计快照不重复相加 |
| 旧版日志 | 计算 `token_count` 累计差分，窗口之前的历史用于建立基线；结果属于估算 |
| Token 总数 | 优先采用原记录 `total_tokens`；缓存输入和推理输出是细分，不再加进总数 |
| 项目归属 | 采用请求当时的工作目录/模型上下文，识别 Git 根目录；未知目录保留分类 |
| 日期 | 使用记录内部时间，不能用文件夹日期代替 |
| 桌面“本周” | 北京时间周一 00:00 至下周一 00:00；刷新时只统计当前时刻之前 |
| 会员月核算 | 按已确认续费日期／时间逐月划分；未知时待配置，日期精度标为估算 |
| 命令行默认窗口 | 最近滚动 7×24 小时，与桌面自然周不同 |

同一仓库的多个 worktree 默认合并；归档副本、重复响应、fork 继承和新旧格式混合均有去重/基线处理。不能确认的旧历史会标为估算、跳过并警告，而不会为了凑总数补算。

连接 Notion 且已有项目缓存时，横条和竖条优先展示管理项目，按管理状态、优先级和名称排序，项目即使本周没有 Token 也可出现。归档项目不进入这份列表；存在计划中或开发中的项目时，暂停项目暂不占据胶囊位置。未关联本地记录的项目明确显示未关联，不将未知用量写成真实零消耗。

本机模式按用量展示前三个真实任务，即使第二、第三名低于 5%；桌面小框保留“低于 5% 合并到其他”的分类。占比均以全部本机已记录 Token 为分母，管理项目列表不参与总数求和。

完整口径与异常处理见 [统计说明](docs/statistics.md)。

菜单「周期核算…」可保存续费锚点、刷新核算并打开独立 Notion 页面。周／月从同一批计量事件按时间重分组，不能相加，也不能把一个月当四周。默认导出当前及此前共 12 个周／会员月；保存新的月锚点后旧云端方案保留审计，不重复计入。完整规则与命令见 [周期核算说明](docs/period-proposal.md)。

## 额度与“本周超额”

“当前已用”是官方当前窗口百分比；“本周已用”是固定自然周中已保存的观测累计，二者在提前重置后会不同。

例如重置前记录了 80%，重置后又记录了 70%，本周已记录使用约 150%，超额约 50%。只累计实际观测到的使用增量，**重置本身不会凭空增加 100%**。

采集中断、首次采集发生在周中、重置前缺少最终读数、额度容量变化等情况都会影响判断：

- 有缺口时提示“周记录不完整”。
- “未超额”表示已记录累计未超过 100%，不能证明完整账户消费未超额。
- 可确认的超额下限显示 `≥`，变化原因或容量有歧义时显示待核实。
- 超额表示相对额度的额外使用，**不代表额外收费**。

不会假设某个订阅每周固定发放多少 Token，也不会用项目 Token 占比推算项目消耗的官方额度。

## 数据与隐私

- 扫描器不联网，不直接读取 `auth.json` 或认证环境变量，不修改 Codex 会话记录。
- 报告只导出用量与归因元数据，不包含提示词、代码、回复或工具输出；报告仍可能包含私人目录和会话 ID。
- 官方额度认证由官方客户端处理，本项目只调用 `account/rateLimits/read`，保留必要额度字段。
- Notion 同步采用稳定项目身份和“项目 × 日期 × Codex × 匿名来源数据集”摘要键，重复扫描替换数值；会话哈希键只在本地用于去重。
- 本地缓存、额度历史和窗口设置位于 `widget/data/`；真实验证导出位于 `validation/`，均被 Git 忽略。
- 仓库中的 UI 预览和示意图使用演示数据，不包含作者的真实用量记录。

## 检查与测试

Python 测试使用临时模拟日志与临时 Git 仓库，不读取用户真实会话：

```sh
.venv/bin/python3 -m unittest -v test_codex_usage.py widget/test_widget_backend.py \
  widget/test_project_store.py widget/test_notion_sync.py widget/test_notion_credentials.py \
  widget/test_period_accounting.py widget/test_period_backend.py \
  widget/test_quota_accounting.py widget/test_period_notion.py
```

Swift 数据与交互检查：

```sh
mkdir -p widget/build/module-cache
xcrun swiftc -parse-as-library -swift-version 5 -D WIDGET_CHECKS \
  widget/UsageWidget.swift widget/UsageStrip.swift widget/NotionSettings.swift widget/test_strip.swift \
  -o widget/build/strip-checks -framework AppKit -framework SwiftUI -framework Security \
  -module-cache-path widget/build/module-cache
widget/build/strip-checks

# 周期设置、续费精度和未知数据检查
xcrun swiftc -parse-as-library -swift-version 5 -D WIDGET_CHECKS \
  widget/UsageWidget.swift widget/UsageStrip.swift widget/NotionSettings.swift widget/test_period_settings.swift \
  -o widget/build/period-settings-checks -framework AppKit -framework SwiftUI -framework Security \
  -module-cache-path widget/build/module-cache
widget/build/period-settings-checks
```

已在 macOS 15、Python 3.12、Swift 6.2.4 / macOS 26.2 SDK 上完成本机检查。具体测试结果以当前版本实际执行为准；模拟测试通过不等于其他机器的真实用量已验证。

## 目录

```text
codex_usage.py                本地日志扫描器
test_codex_usage.py           扫描器测试
widget/UsageWidget.swift      macOS 应用与桌面小框
widget/UsageStrip.swift       条形组件与交互
widget/widget_backend.py     本周归集、官方额度读取和历史估算
widget/period_accounting.py   自然周与会员月的事件时间核算
widget/quota_accounting.py    已保存官方窗口与观测质量
widget/period_notion.py       独立 Notion 周期账本与额度窗口同步
widget/project_store.py      稳定项目身份和项目每日事实
widget/notion_sync.py         Notion 元数据、绑定与摘要同步
widget/notion_credentials.py  用户选定的 Pages 密钥来源
widget/NotionSettings.swift   原生设置和专用钥匙串凭据
widget/test_notion_sync.py    同步、重试与幂等测试
widget/test_widget_backend.py 数据层测试
widget/test_strip.swift       Swift 数据与交互检查
widget/preview/demo.html     使用演示数据的交互预览
docs/statistics.md            详细统计口径
docs/notion.md                连接、项目图标、任务读取和摘要同步
docs/project-management.md    项目管理首页、AI 维护约定和官方参考
docs/period-proposal.md        已确认的周期核算规则与续费设置
skills/notion-project-workbench/ 可发布的 Codex 项目管理技能
```

## 当前限制

只覆盖本机仍保留的记录，不能恢复删除的历史，也不会自动合并其他设备/云端任务的 Token。日志格式与官方 app-server 版本变化可能需要适配。macOS 26 原生玻璃效果尚未在该系统实机验证；窗口位置会保存并按可见区域恢复，多屏切换仍需更多实机验证；现阶段以本地构建运行。默认本机模式，Notion 连接可以稍后开启。
