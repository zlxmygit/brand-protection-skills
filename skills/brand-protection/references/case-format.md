# 案件格式、校验与迁移

当前空白记录为 [case-record.json](../assets/case-record.json) `1.3`。它是可填写模板，`field_guidance` 提供字段说明，不是完整 JSON Schema。按需填写事实；空值不是已验证、已授权或已发送。

## 只读队列契约

运行 `python3 scripts/followup_queue.py --case /私有案件/case-record.json`；多案重复 `--case`，演练可传带时区的 `--now`。默认期限窗口 48 个实际小时，不能代替工作日和正式送达期限计算。

未启用或暂停的案件可运行 `python3 scripts/followup_queue.py --case /私有案件/case-record.json --validate-only`。该模式检查本工具负责的结构和历史关联，保留错误、期限和覆盖诊断。`due_tasks`、`deferred_tasks` 和 `recovery_tasks` 始终为空。它不启用跟进、不恢复暂停，也不修改案件。

- `due_tasks`：已到期候选，外发仍须按 [跟进规则](follow-up.md) 完成最新反馈、证据、权限和送达核验。
- `errors`：按记录返回的数据或关联错误。坏任务、坏期限不遮住其他可独立识别的期限；整份文件无法解析时才返回文件错误。CLI 有错误返回 `1`，参数错误返回 `2`；接入者必须读取输出与退出码，不能把非零退出简单当成没有待办。
- `held_tasks`：明确阻塞的任务。发送记录/行动/运行的关键关联不可信时，保守阻止外发候选，继续只读事项和有效期限；错误修复前不能把候选数量当作完整工作量。
- `deferred_tasks`：尚未达到执行时间的任务及限制依据。普通 `cooldown_until` 只约束催办/重提；补证、更正等仍服从有依据的 `all_outbound_not_before`。新通知改变时间时重排旧任务，不让旧周期覆盖正式要求。
- `deadline_alerts`、`unresolved_deadlines`、`deadline_conflicts`：已知临近/过期期限、待核期限及阻塞/延期冲突。冲突不会自动解除机构限制或授予发送权限。
- `recovery_tasks`：核对运行、只读任务改排、发送核对及阻塞复核的候选，实际恢复仍须保存决定再重新计算。字段及状态条件以跟进规则为准；租约过期本身不允许抢占。
- `coverage_gaps`：按目标阶段、已登记渠道、行动和有效待办检查范围。未结行动需要下一步；阻塞待办需要具体 `reason` 和 `review_at`。任务有 `target_ids` 时仅覆盖那些目标，未填写则继承行动范围。`phase: investigation` 的目标可用有效目标级只读任务覆盖，不要求先建立投诉行动。明确启用的复发观察独立检查，即使原行动和目标已结束。没有缺口不代表已发现全部侵权或已执行全部步骤。

每类记录的 `id` 为非空字符串且在本类中唯一；重复 ID 的记录全部隔离，不任取一条。发送尝试必须保留可核对的 `task_id`、`action_id`、`dedupe_key`，与历史任务一致。不能清理历史任务而留下无法追溯的发送记录。最小校验器不核实真实权属、收件人、授权或全部字段类型，不能作为法律判断和发送许可。

## 严格校验与期限关联

[队列安全契约](queue-safety.md) 说明此次审计修复的字段语义、行动级外发阻断和验收方法。数据版本保持 `1.3`；新增可选的 `followup_tasks.deadline_ids`，不自动迁移或改写案件。

显式 `target_ids` 必须是非空、无重复、存在于本案的目标 ID 列表；任务范围还须属于其行动。只有省略任务的 `target_ids` 才继承行动范围，空列表不代表全部目标。`claim_frozen` 出现时必须是 JSON 布尔值；可选时间限制仅允许带时区的时间字符串或 `null`，不把空字符串、数字或布尔值当作未配置。

`deadline_ids` 出现时必须是无重复的已有期限 ID 列表，且不能关联其他行动的期限。`[]` 明确表示该任务不承担期限响应；省略时兼容旧记录，按同一行动关联普通运营任务，但不将 `check_recurrence` / `verify_effect` 自动关联到当前应答期限。所有已知临近期限仍独立输出，不能用空关联清除期限告警。

CLI 以 UTF-8（兼容 BOM）读取，输出 ASCII 转义的 JSON，中文解析后不丢失。任意层级重复对象键、非标准/非有限数字及无法解析的输入按文件报错；不任取被重复键覆盖的值。其他可解析案件继续处理。

文件上限为 8 MiB。直接调用 Python API 也会检查共享预算：最多 10,000 条对象列表记录、50,000 项 `_ids` 列表引用、64 层嵌套和 100,000 个遍历节点。超限时停止任务计算；若 `deadlines` 列表未超过 10,000 条，仍保留其中能独立解析的期限。歧义 JSON、超大文件或超限的期限列表不能保证提取期限，须先恢复可信记录。预算错误不允许外发。

## 复发观察与阶段覆盖

可选顶层 `recurrence_observations` 保持 `schema_version: 1.3`。每项记录 `id`、非空 `target_ids`、`authorization_source`、`status`、`interval_hours` 和 `next_review_at`。只有显式 `enabled` 且授权来源、正数周期和带时区复查时间齐全时，才纳入观察覆盖检查。其他状态为 `not_authorized`、`paused` 和 `ended`；它们不要求新任务，也不自动恢复。

`check_recurrence` 用 `recurrence_observation_id` 关联观察。任务可不关联投诉行动。省略任务范围时，先继承已有行动范围；没有行动时继承观察范围。范围必须属于观察，不能借观察扩大原行动。有效下一步须覆盖对应目标，且执行或阻塞复核时间不晚于 `next_review_at`。缺少任务、只覆盖部分目标或安排过晚，都保留缺口。

旧 `1.3` 案件可省略观察数组及任务关联，仍按旧只读任务计算。兼容不证明已登记观察范围或获得观察授权。本案已有非空显式观察登记时，未关联的复发任务会保留并阻断，先确认归属，避免绕过观察暂停；不自动猜关联。调查目标显式填写 `phase: investigation`；此阶段需要带 `target_ids`、无行动关联的有效只读任务。案件级无范围任务不能代替目标取证。已有 `researching`、`draft` 或 `awaiting_evidence` 行动可共享该目标级调查步骤，已提交行动仍需自己的跟进任务。

观察字段为 `[]` 时保留旧行为。其他显式值须校验；重复 ID、畸形记录或非列表值报错，隔离后也不恢复未关联复发任务的候选资格。

旧 `follow_up.authorizations.recurrence_watch` 已有明确来源，却没有有效显式观察计划时，`warnings` 返回 `recurrence_authorization_requires_plan_review`。应核对当前范围、状态和节奏；即使已有兼容的旧复发任务，仍提示补齐登记。授权本身不等于启用，该提示不创建任务，也不阻断其他有效候选。

## CLI 参数与状态参考

| 参数 | 类型和约束 |
|---|---|
| `--case` | 必填文件路径；多案重复传入。 |
| `--now` | 可选带时区时间；省略时取当前 UTC。 |
| `--horizon-hours` | 有限非负数，默认 `48`。 |
| `--validate-only` | 无参数开关；只返回诊断，三类任务候选为空。 |

| 字段 | 值 |
|---|---|
| 只读任务 `kind` | `check_feedback`、`check_target`、`verify_receipt`、`prepare_escalation`、`verify_effect`、`check_recurrence`、`reconcile_send`。 |
| 外发任务 `kind` | `initial_notice`、`remind`、`resubmit`、`send_supplement`、`send_escalation`、`send_correction`。候选仍需重新核验权限。 |
| 任务 `status` | `planned`、`ready`、`in_progress`、`blocked`、`completed`、`cancelled`。闭合任务仍检查历史关联。 |
| 运行 `status` | `running`、`completed`、`interrupted`。 |
| 发送尝试 `status` | `prepared`、`in_flight`、`sent`、`outcome_unknown`、`confirmed_not_sent`。 |
| 观察 `status` | `enabled`、`not_authorized`、`paused`、`ended`。 |
| 报告 `state` | `preview`、`not_enabled`、`paused`、`validation_only`、`invalid_input`、`record_budget_exceeded`。 |

`follow_up.requested` 和 `enabled` 必须为 JSON 布尔值，且都为 `true` 才进入普通预览。`scheduler.status` 或 `host_adapter.status` 为 `paused` 时，普通预览返回 `paused` 和空候选。暂停不结束观察授权；恢复必须明确修改当前状态，恢复后的候选仍不授权外发。报告的 `validation_only: true` 标明只校验；`follow_up_state` 保留本次看到的 `preview`、`not_enabled` 或 `paused` 状态。

## 从旧版本迁移

在获得该私有案件维护权限后，先备份原记录及修订号，生成独立候选副本。逐项映射并校验；确认源修订未改变后，使用可恢复的原子替换保存。迁移不改变提交状态或当前用户暂停决定，也不自动启用监测。未知或不支持的版本保留原件，先人工/代理映射，不猜版本内容。

| 原字段/情形 | `1.3` 处理 |
|---|---|
| `1.1` 无持续跟进字段 | 从空白模板补缺省结构，`requested/enabled` 保持 false；仅沿用可追溯的实际持续跟进授权启用。 |
| `1.2` 已有跟进结构 | 保留案件 ID、历史、游标、权限、修订与真实时间；新增字段为空/未配置，不重置旧记录。 |
| 旧 `cooldown_until` | 先查原依据；普通催办冷却保留原字段，明确约束全部外发的通知另记 `all_outbound_not_before` 及出处。依据不明时先设相关任务阻塞并排复核，不凭字段名放行补证。 |
| 历史发送缺 ID/行动/任务关联 | 从原通知、消息或工单恢复并留证。仅可给已确定的记录分配本地 ID，不能猜关联或送达；查不清保留待核，不能删除记录绕过拦截。 |
| 运行缺少状态或领取信息 | 查实际执行记录；不能根据时间久远填 `interrupted`。`run_id/claimed_at/lease_expires_at` 与中断证据按事实填写，无法确认则核对而非自动重发。 |
| 开放目标/渠道未结构化 | 从已有渠道计划映射 `channel_plan` 及 `action.target_ids/provider_id/route`，暂未确认的服务商留空。缺少下一步或阻塞复核点时补建待办，不扩张任务范围。 |
| 已发现错误的已发通知 | 冻结原主张；按跟进规则建立关联原行动的独立更正事项，不改写原发送历史。 |
| 健康观察字段缺失 | `health_observer.mode=none`、`status=not_configured`；只有确有独立观察者或人工安排及授权时才填写对应状态。 |

映射完成才将候选版本设为 `1.3`，增加修订并记录迁移来源、时间、字段决定和仍未解决的错误。队列支持读取无版本及 `1.1/1.2` 记录并提示迁移，但不会替你写回，也不保证旧记录具备新恢复所需证据。

## v1.8.1 校验修正

升级后的材料依赖、品牌及目标关联、首次通知和输入输出检查见[审计修复说明](audit-fixes.md)。旧审批快照需要重新复核，不自动改写私有案件。
