# 数据源约定

先读取目标父页面及其已有 data sources，按页面／数据源身份、关系与语义确认用途。下列是新工作台的参考约定，不是批量重命名旧库的要求。采用现有对应字段时记录映射；缺失的管理列可按当前任务需要补充，不能改坏正在使用的用量列。

## Projects

| 属性 | 建议类型 | 含义 |
| --- | --- | --- |
| Name | title | 项目名称 |
| 页面 icon | 页面属性 | emoji 或 Logo，胶囊沿用此身份 |
| Vision / Goal | rich_text 或正文 | 愿景、目标与预期结果，首页首位 |
| Status | status 或 select | 计划中、开发中、已发布、暂停、归档 |
| Weekly Goal / Next Step | rich_text | 当前阶段目标与具体下一步 |
| Priority / Health | select | 高／中／低；正常／有风险／受阻 |
| Development Progress | number | 有明确依据的 0–100 开发进度，未知留空 |
| Target Date / Last Reviewed | date | 明确计划日期与实际复盘日期，未知留空 |
| Summary / Outcome | rich_text / url | 当前状态摘要、成果链接 |
| Binding Key | rich_text | 若已有应用使用，保留其去重语义 |

任务、事件和投入都关联到同一个项目页面身份。项目改名不能断开关系或生成第二个项目。

不要因 Token 增长提高 Development Progress。Tasks 的已完成／全部有效行动项只能表达行动完成率，不应改写成整个项目完成度。

## Tasks

| 属性 | 建议类型 | 含义 |
| --- | --- | --- |
| Name | title | 一个具体行动项 |
| Project | relation → Projects | 所属项目，工作台约定每任务一个项目 |
| Status | status 或 select | 待处理、进行中、待确认、阻塞、已完成、取消 |
| Priority / Due | select / date | 优先级、已确认日期 |
| Managed By | select | AI 或人类确认 |
| Acceptance / Evidence | rich_text / url | 完成条件与可核查依据 |

桌面用量小助手当前 Tasks 计数适配器要求 Status 为上述选项的 select，以及 Project relation 指向配置的 Projects 数据源。连接其他 status 结构时先兼容适配，不通过改写远端结构让计数“看似正确”。

待办包含待处理、进行中、待确认和阻塞；受阻是待办的子集。已完成保留数据库行，视图可以隐藏它；取消和归档不计入当前待办。计数读取失败或不完整时不是零。

## Events

Events 存开发事实，不能替代 Tasks 当前状态。若已有事件库采用别的字段名，优先映射并复用。

| 属性 | 建议类型 | 含义 |
| --- | --- | --- |
| Name | title | 简短的实际变化，如“完成粘贴交互修复” |
| Project | relation → Projects | 事件所属项目 |
| Tasks | relation → Tasks | 相关行动项，一个事件可关联多个 Tasks |
| Occurred At | date | 实际发生时间，支持精确时间与时区 |
| Result | rich_text | 实际动作、结果，以及仍未验证的部分 |
| Evidence | url | 可核查结果的主链接；其余链接放正文 |
| Event Key | rich_text | 一次实际推进的稳定标识，用于重试去重 |
| Created At | created_time | Notion 实际建立记录时间，用于区分发生与录入 |

原完成时间未知的历史证据，记录为“本次回填旧证据”：Occurred At 是实际回填时刻，Result 明确原完成时间未核实。不要为了画满时间线伪造过去日期。无需为每个毫无变化的扫描创建事件。

Event Key 识别一次实际推进，不是仅使用项目名或任务 ID。同一次操作先查是否已记录，确认已存在就复用；下一次真实推进使用另一个键。不要将本机线程／响应 ID 或私人路径写入键中。

Event Key 的富文本属性没有唯一约束。一个项目由一个写者顺序更新，并在创建后按键复查；先查后建不能保证并发唯一。多条同键记录或写入结果不确定时停止同键追加、标为待核实，保留全部历史，不自行删除或合并。

事件结果后续被纠正时追加更正记录，关联原任务并在正文链接原事件。旧结果保留为当时的历史事实，不篡改成最终答案。

## Usage Daily

复用已有账本：Project、Day、Provider、Included、Counted Tokens、Total Tokens、细分计数、Quality、Data Scope、Statistics Until ISO、匿名 Dataset、Summary Key 等。不要重算原始日志或把两次扫描结果相加。

首页投入视图过滤 Included = true，用 Counted Tokens 汇总，同一来源的绝对摘要采用现有稳定键替换。已停计的旧分配留在审计视图。日会话数非可加量；多来源没有去重依据时分列展示，不声称完成了跨设备归并。

项目管理内容以用户授权的摘要维护；计量部分只传摘要。原始日志、代码、会话、工具输出、认证信息和本机绝对路径不进入 Notion。
