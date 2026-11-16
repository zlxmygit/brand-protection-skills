# 宿主协议与本地文件适配器

[本地适配器](../scripts/host_adapter.py)校验宿主提供的反馈收据，并原子保存案件。它不连接邮箱、不签署、不外发，也不创建调度器。所有结果都保留 `authorizes_submission: false`。

[空白协议模板](../assets/host-adapter-contract.json)默认停用，私有路径全部为空。模板用于准备配置和请求，不是已经启用的连接器。案件、请求、反馈收据及运行记录必须留在用户显式指定的私有目录。

## 路径与命令

私有目录必须已存在，且位于仓库、Skill 和宿主扫描目录之外。案件和请求文件都必须在该目录内。适配器复用既有路径检查，拒绝用户符号链接、Windows 重解析点和目录穿越。所有 JSON 输入使用严格解析器和结构预算，拒绝重复键、非有限数字及超限输入。

默认操作为只读 `status`。以下命令都需要明确提供私有目录和案件文件：

```bash
python3 scripts/host_adapter.py status \
  --private-root /path/to/private-case --case case-record.json

python3 scripts/host_adapter.py validate \
  --private-root /path/to/private-case --case case-record.json \
  --request host-request.json

python3 scripts/host_adapter.py commit \
  --private-root /path/to/private-case --case case-record.json \
  --request host-request.json
```

`validate` 检查当前修订与请求，返回拟写入的修订号。它不领取任务，也不创建锁文件。`commit` 才执行写入。请求中的字段均为数据，不作为命令、代码或插件执行。

## 配置与暂停

把模板的 `follow_up_host_adapter` 对象放入私有案件的 `follow_up.host_adapter`。协议版本为 `1.0`。`status` 取 `disabled`、`active` 或 `paused`；周期和宽限分别使用 `expected_interval_hours`、`grace_hours`。启用前填写当前授权依据、反馈源和实际调度配置。

该扩展为可选项，不自动迁移旧游标或缺少来源范围的消息标记。旧标记若没有对应处理状态，适配器会拒绝提交。先保留原件，再核清范围和处理结果；不能清空历史来通过检查。

状态只通过请求中的 `transition` 改变。该对象需要 `from`、`to`、`authorization_source` 和带时区的 `recorded_at`。首次启用还可填写周期和宽限。允许停用后启用、暂停后恢复、活动中暂停，以及停用活动或暂停的适配器。`from` 必须匹配当前状态。

`active` 不会修改 `follow_up.requested` 或 `follow_up.enabled`，也不授予发送权限。`paused` 或 `disabled` 时，只允许记录控制转换或失败运行，拒绝反馈处理和业务记录替换。暂停不会删除已发送历史、未决发送、期限或恢复点。

队列遇到 `host_adapter.status: paused` 时，默认不返回到期、延期或恢复候选。`followup_queue.py --validate-only` 仍执行完整结构校验，三类候选均为空。适配器的 `validate` 只校验本地事务，不代替队列检查。

## 提交请求

请求必须包含 `version: 1.0`、整数 `expected_revision` 和 `run`。可选字段为 `transition`、`feedback_batches` 和 `records`。未知字段会被拒绝。

`run` 需要唯一 `id`、`kind`、`scheduler_id`、起止时间、`source_read_results` 和 `errors`。`kind` 取 `manual_trial`、`manual_control` 或 `scheduled`。手动运行的 `scheduler_id` 必须为空；调度运行必须匹配案件中实际配置的 `automation_id`。未来时间和倒置的起止时间均会报错。

`source_read_results` 逐项记录 `source_id`、`account_scope` 和 `status`，状态为 `complete`、`partial` 或 `failed`。完整读取必须提供相同范围的完整反馈批次。失败或部分读取可以保存失败运行，但不能在同一请求中推进任何反馈游标。

`records` 仅允许替换 `actions`、`deadlines`、`followup_tasks` 和 `outbound_attempts`。每个值是该节的完整列表，需要保留相关历史。适配器检查标识和案件归属；它不替代所有案件业务规则。写入后仍应运行完整队列校验。

## 反馈分页与消息效果

反馈源来自同案的 `follow_up.feedback_sources`。每个源需要唯一 `id`、`account_scope`、非空 `authorized_thread_or_ticket_ids` 和 `authorization_source`。来源及账号组成读取范围，线程必须在显式授权列表中。

每个反馈批次包含以下字段：

| 字段 | 要求 |
|---|---|
| `source_id`、`account_scope` | 匹配一个已配置反馈源。 |
| `covered_thread_or_ticket_ids` | 完整覆盖该源已授权的全部线程，无重复。 |
| `from_cursor` | 匹配已保存游标，首次读取为空。 |
| `read_at` | 带时区，且不晚于运行结束。 |
| `read_receipt` | 同私有目录内的原始读取收据，包含 `path` 和 `sha256`。 |
| `pages` | 1 至 100 页的完整、有序分页链。 |

每页包含 `cursor`、`next_cursor`、`complete` 和 `messages`。首个 `cursor` 匹配 `from_cursor`，后一页匹配前页的 `next_cursor`。只有末页的 `complete` 为 `true`。缺页、循环、提前结束和不完整末页均被拒绝。宿主必须保留连接器的真实分页响应；适配器校验收据字节和声明的链，不认证连接器，也不能发现宿主伪造的完整标记。

每条消息包含来源、账号、`thread_or_ticket_id`、`message_id`、`content_sha256` 和 `derived_state`。去重键包含来源、账号、线程及消息 ID。同键同内容可以重读；同键内容或既有处理结果冲突时拒绝整次提交。

`derived_state` 需要 `category`、`summary`、`observed_at` 和 `effect`。可选 `action_id` 必须引用同案行动。`effect` 必须明确选择以下一种：

| 效果 | 必须同时保存的内容 |
|---|---|
| `records_updated` | 非空 `record_refs`；每项含 `section`、`id`、`after_sha256`，引用本次 `records` 中确有变化的记录。 |
| `no_change` | 具体 `reason`，说明为何无需变更业务记录。 |
| `requires_review` | 具体 `reason` 和 `review_at`；适配器同时建立未结的 `feedback_review_items`，状态为 `open`。 |

`after_sha256` 是变更后单条记录的规范 JSON 哈希：ASCII 转义、键排序、分隔符为逗号和冒号、不允许非有限数字。脚本提供 `record_digest(row)`。声明会改变行动和期限时，两项记录都要引用；漏写任一项就不能推进游标。

适配器不能判断宿主遗漏了多少法律或业务效果。需要专业判断时使用 `requires_review`，保留未结项。处理消息的标记只证明反馈已写入本地协议状态，不表示补证已完成、期限已履行或案件已解决。

同一次原子写入保存业务变更、`feedback_events`、未结复核项、`processed_message_ids`、读取收据、`source_cursors`、运行记录和新修订号。有消息而没有明确效果时拒绝。没有新消息的完整读取仍保存覆盖收据和运行结果，不虚构消息或业务结论。

## 互斥、原子写入与未知发送

提交使用同目录的固定锁文件。POSIX 使用 `flock`，Windows 使用 `msvcrt` 字节独占锁；锁文件保留，避免并发进程锁住不同 inode。进程退出后由操作系统释放锁。租约字段不参与互斥，其他写入入口必须遵守同一协议。

持锁后重新读取案件，并执行 `expected_revision` 比较。适配器在同目录创建临时文件，写完后刷新并 `fsync`，再用 `os.replace` 一次替换案件。它重新读取并核对最终 JSON 和文件哈希。校验、版本冲突或替换前失败时，旧案件和游标保持不变。

如果替换成功而读回失败，输出 `commit_uncertain`。此时不得自动重试；先检查已保存的修订、消息和发送状态。本版未对目录执行 `fsync`，不承诺断电后的目录项持久性。网络文件系统的锁及替换语义需要另行验证。

既有 `prepared`、`in_flight` 和 `outcome_unknown` 尝试必须保留。不能换去重键、改载荷或删除关联行动和任务来解除阻断。未核清时，关联行动和任务保持原样。核清后仍须保留其身份、目标、渠道和任务关联。

保留规则覆盖发送尝试的整个生命周期。`sent` 和 `confirmed_not_sent` 也不能在后续事务中删除、改键或改写既有事实。终态允许补充回执等缺失详情；关联行动和任务允许合法状态更新，但身份、目标、渠道、任务类型及去重关系必须保留。此协议不授予清理历史的权限。

历史保留也覆盖发送行动所属的整个更正连通分量。即使中间行动没有发送尝试，仍须保留该分量原有的行动 ID 和 `corrects_action_id` 关系。不能移动或删除中间节点来切断未决发送屏障。适配器每次提交只遍历一次历史关系；无关纯草稿仍可合法编辑。

从未决发送改为 `sent` 或 `confirmed_not_sent`，必须填写 `reconciliation`：`conclusion`、`checked_at`、`checked_by`、`execution_terminated: true` 和非空 `evidence_ids`。证据必须引用本案已有记录，且本地文件与 SHA-256 一致。工具验证证据引用与字节，不认证核验者，也不独立证明发送结果或原进程已经结束。未知结果继续保留，不能据此重发。

## 健康状态与验收边界

`status` 只读返回 `unverified`、`overdue` 或 `healthy`。没有启用跟进、没有真实调度配置、没有完整来源读取及成功写回记录时，结果为 `unverified`。手动试跑、未来记录和失败运行不能成为调度健康证明。

`healthy` 仅表示配置所指的 `scheduled` 运行，已经保存全部来源覆盖收据和成功事务，且未超过约定周期与宽限。健康周期取运行完成时间和全部已授权来源读取时间中的最早值，输出为 `freshness_observed_at`。`persisted_at` 只证明写回，不刷新旧读取。延迟补写或近期运行使用旧来源收据，仍按真实观测时间判断超期。

超期返回 `overdue`。较新的失败运行会阻止旧成功记录恢复健康；补写顺序和新修订号不能掩盖失败。`healthy` 不证明机构作出回复、外部调度器仍可唤醒，或所有业务工作已经完成。

`connector_access` 和 `independent_observer` 仍标记为 `unverified`。只填写独立观察者配置，不能证明失联时可告警；同一电脑上的状态检查也不能证明这项能力。真实连接器、自然调度、独立观察者和外发应分别验收。

合成回归命令：

```bash
python3 -B -m unittest discover -s tests -p test_v184_host_adapter.py -v
```

测试覆盖进程互斥及死亡后释放、版本冲突、替换失败、分页与范围缺口、消息效果漏写、未知发送保留、暂停恢复和健康证据。本地 POSIX 实测不能代替 Windows、真实调度器或真实反馈源验收。
