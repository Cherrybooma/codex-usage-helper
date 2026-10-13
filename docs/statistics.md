读取用户电脑上已有的 Codex 会话记录，按项目、会话、模型或日期汇总 **已记录的 token 用量**。

- 普通 JSONL、gzip 记录使用 Python 标准库，Python 3.10 及以上即可。
- 全部处理在本地进行，脚本没有网络请求。
- 只扫描 Codex home 下的 sessions 和 archived_sessions，不读取认证文件；不修改 Codex 的会话、配置或数据库。
- 解析过程中会读取会话 JSONL，但仅提取用量和归因元数据；导出的报告不包含提示词、代码、回复或工具输出。报告会包含本机目录和会话 ID。

## 快速使用

把 codex_usage.py 下载到自己的电脑，打开终端运行：

    python3 codex_usage.py

默认使用 CODEX_HOME 环境变量指定的位置，否则使用 ~/.codex，统计截至运行时的最近 7 × 24 小时，按项目排序。

Windows 可使用：

    py -3 codex_usage.py

指定 Codex 记录位置：

    python3 codex_usage.py --codex-home /Users/you/.codex

统计全部保留的历史：

    python3 codex_usage.py --all-time

统计一个自然月，使用上海时区：

    python3 codex_usage.py --since 2026-09-01 --until 2026-10-01 --timezone Asia/Shanghai

since 包含起始时刻，until 排除结束时刻。没有时区的日期或时间按 --timezone 解释，默认使用本机时区；带 Z 或 UTC 偏移的时间按其自身时区解释。天数模式按滚动的 24 小时计算。

系统时区无法解析为 IANA 时区时，会使用当前的 UTC 偏移。统计跨夏令时的历史，建议显式指定 --timezone，尤其是在 Windows 上。

查看会话、模型和逐日统计：

    python3 codex_usage.py --group-by session
    python3 codex_usage.py --group-by model
    python3 codex_usage.py --group-by day --timezone Asia/Shanghai

导出完整 JSON：

    python3 codex_usage.py --all-time --format json --output codex-usage.json

JSON 同时包含 totals、projects、sessions、models、daily、project_daily、coverage 和 warnings。project_daily 是项目与上海日期的交叉汇总，从同一条已经完成去重、窗口过滤和旧差分计算的记录归集；各 Token/记录计数与总数对齐，会话数以该行 thread_ids 的并集计算，不能跨日简单相加。--group-by 只改变终端表格，不影响 JSON 的完整内容。

如果系统没有时区数据库，使用 IANA 时区时可能需要安装 tzdata：

    python3 -m pip install tzdata

## 项目归因

脚本从 session_meta 和 turn_context 获取工作目录、轮次和模型。记录包含明确 turn_id 时，优先使用同一线程对应轮次中已出现的最近目录和模型；延迟响应及跨文件回查不会使用后续才出现的上下文。

若目录在本机仍存在且安装了 Git，会解析到 Git 仓库根目录；同一仓库的多个 worktree 默认合并到主仓库。要分别统计：

    python3 codex_usage.py --separate-worktrees

没有 Git、目录已删除、Git 命令失败或记录来自其他操作系统时，使用记录里的工作目录作为项目名称。目录未知的用量归入 unknown。

子代理自身的请求计入其执行目录对应的项目。会话明细保留 parent_thread_id 和 forked_from_id，便于以后按父任务归集；不会把父、子代理用量重复叠加。

## 计数口径

**新版按响应统计：** 读取 token_usage_record 的 usage，按 response_id 全局去重；不把 turn_token_usage、thread_token_usage 等累计快照再次加入。复制到 fork 的请求仍归原 thread_id；有原始记录时优先使用原始会话的时间。只有复制记录可用时，原始时间未知，只在 --all-time 中计入，日期为 unknown。

**旧版累计差分：** 使用 token_count.info.total_token_usage 的增量。整个历史会先参与建立基线，再按事件时间筛选增量，所以跨日不会把整段累计都算到第二天。last_token_usage 可能包含上下文估算值，不作为额外用量相加。

**混合版本：** 保留同一会话首条有效新版请求记录之前的旧增量；该会话开始出现新版记录后，仅累计新版请求。覆盖情况中分别列出 request_records 和 legacy_updates。

**旧 fork：** 根据 forked_from_id 找父历史，识别累计值相同的继承前缀并建立子会话基线。复制日志的时间戳可能被重写，因此不通过复制时间判断新消耗。父历史缺失时省略旧 fork 用量并警告；不能确认继承基线时，首条旧快照只用于建立基线。这些情况下统计可能不完整。

**旧计数器异常：** 人工的上下文窗口快照、累计值下降等事件只重建基线并警告。旧格式缺乏稳定请求 ID，统计标为累计差分估算，可能与实际账单不同。

同一逻辑会话的多个物理文件按文件头的创建时间合并，文件内部保持行序，避免时钟回拨改变升级和差分逻辑。跨文件重复的旧累计值按轮次和各项计数去重，coverage.rewritten_legacy_snapshots 列出这类记录；缺乏稳定 ID 的旧记录仍属于估算。

总数优先使用原记录的 total_tokens；缺失且 input_tokens、output_tokens 均可用时，推导为二者之和。缓存输入、缓存写入及推理输出分别展示，不能再次加进总数。缺失的细分字段在数值中使用 0 占位，coverage.missing_fields 会明确列出，不能据此判定真实用量为零。

旧累计字段缺失不使已知总数增量失效。字段恢复后的首条只重建该字段的基线，缺失期间及恢复首条无法确认的细分增量使用 0 占位，并列入 coverage.missing_fields 和 warnings；不会把恢复后的整个累计细分再加一次。

日期以记录内部的 timestamp 为准，不使用文件夹日期。缺少或无法解析时间戳的记录在时间窗口统计中省略并警告；--all-time 可以保留，日期为 unknown。

## 压缩、异常和覆盖情况

支持 .jsonl、.jsonl.gz、.jsonl.zst。同目录同名的未压缩和压缩版本同时存在时，优先读取未压缩文件。

对于 zstd 压缩记录，脚本按以下顺序寻找解压能力：Python 3.14 的 compression.zstd、zstandard Python 包、系统 zstd 命令。没有可用解压能力时会跳过并报告，不将它当作零用量。Python 3.10–3.13 可选安装：

    python3 -m pip install zstandard

损坏或正在写入的 JSON 行会跳过并警告，后续完整行仍可读取。缺少 Codex home 或命令参数错误会返回非零退出码；个别记录无法读取时会返回部分报告，并在 warnings、coverage 中说明。

表格模式将最多 20 条具体警告写入 stderr。JSON 模式包含所有警告，不在 stdout 混入诊断文字，便于后续程序读取。

仅覆盖指定 Codex home 中仍然保留的记录。远程机器、云端任务、另一个系统用户或不同 CODEX_HOME 的用量需要分别采集；删除的历史不能恢复。格式会随 Codex 版本变化，陌生版本需验证记录字段。

这是 token 用量统计，不是订阅剩余额度、API 最终账单或精确的文件级费用分摊。脚本不调用账户用量接口。

## 验证

测试只使用临时模拟会话和本地临时 Git 仓库，不读取真实用户记录：

    python3 -m unittest -v test_codex_usage.py

官方源码参考快照：

https://github.com/openai/codex/tree/39e013c8b7cb6eec46d16ff2b74fac4e22992e8c
