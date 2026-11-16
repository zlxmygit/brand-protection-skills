# 合成场景与只读验收

以下请求、标识、授权文字和时间均为合成信息。示例适用于任意品牌，不含真实品牌、案件、邮箱或权利材料。真实输入保存在仓库外的私有案件目录。

在安装后的 `brand-protection` 目录运行命令。命令只调用现有队列函数，在内存中构造记录并检查输出，不联网、不写案件、不发送，也不创建调度器。记录仅演示队列边界，真实案件仍须按空白模板填写实际依据。固定时间仅用于演练，不是法律期限。

队列候选不代表发送许可。真实任务仍须按[持续跟进规则](follow-up.md)检查最新反馈、送达状态、来源限制、签署权限和审批。材料审批使用 `snapshot_version: 1.2`；审批文件自身的 `version: 1.0` 不变，旧哈希须重新复核。

## 权利未齐的新线索

合成请求：“先核查一个新线索。商标文件稍后补，市场还没确定，先不要投诉。”

必要输入是用户提供的目标、线索来源和调查范围。将目标设为 `phase: investigation`，记录 `rights` 和市场缺口。只读任务显式绑定 `target_ids`，不为满足覆盖检查伪造投诉行动、服务商或渠道。未请求持续跟进时，`follow_up.requested/enabled` 保持 `false`。

安全动作是整理已授权资料、保存公开观察和准备条件式草稿。缺少的权利、市场和外发授权继续保留，不升级为事实。使用校验模式检查记录，不启用跟进。

```bash
python3 -B - <<'PY'
from datetime import datetime
from scripts.followup_queue import build_queue

stamp = "2026-11-16T04:00:00Z"
case = {
    "schema_version": "1.3", "case_id": "synthetic-new-lead",
    "rights": [], "markets": [],
    "missing_information": ["rights", "specific_markets", "submission_authority"],
    "follow_up": {"requested": False, "enabled": False},
    "targets": [{"id": "target-new", "supplied_url": "https://lead.example/",
        "normalized_domain": "lead.example", "status": "open", "phase": "investigation"}],
    "actions": [], "deadlines": [], "runs": [], "outbound_attempts": [],
    "followup_tasks": [{"id": "inspect-new", "action_id": None,
        "target_ids": ["target-new"], "kind": "check_target",
        "status": "planned", "due_at": stamp}],
}
out = build_queue(case, datetime.fromisoformat(stamp.replace("Z", "+00:00")),
                  validate_only=True)
assert out["validation_only"] is True
assert not out["errors"] and not out["coverage_gaps"], out
assert out["due_tasks"] == out["deferred_tasks"] == out["recovery_tasks"] == []
assert case["actions"] == [] and case["follow_up"]["enabled"] is False
print("new-lead: validated; follow-up remains disabled")
PY
```

预期：记录通过最小结构与覆盖检查，执行候选为空。它不证明权属、市场选择或侵权成立，也不证明目标网页已经取证。命令行等价校验参数是 `--validate-only`；该参数不授予调查或发送权限。

## 送达不明时先核对

合成请求：“上轮运行中断了，不确定催办有没有发出。先查清楚。”

必要输入包括原 `task_id`、`action_id`、`dedupe_key`、发送尝试状态，以及可读取的原消息或工单记录。`prepared`、`in_flight` 和 `outcome_unknown` 都要核对。它们分别不能证明“未发送”“已发送”或“发送失败”。保留历史任务和发送尝试。

安全动作是读取原发送渠道，核对收件方、消息标识、时间和回执，再保存有依据的核对结果。查不清时继续保留阻塞。换键、新建一轮或等待租约过期都不能解除送达不明。

```bash
python3 -B - <<'PY'
from datetime import datetime
from scripts.followup_queue import build_queue

stamp = "2026-11-16T04:00:00Z"
for state in ("prepared", "in_flight", "outcome_unknown"):
    case = {
        "schema_version": "1.3", "case_id": "synthetic-delivery",
        "follow_up": {"requested": True, "enabled": True},
        "targets": [{"id": "t", "status": "open", "channel_plan": [
            {"provider_id": "p", "route": "email"}]}],
        "actions": [{"id": "a", "status": "acknowledged", "target_ids": ["t"],
            "provider_id": "p", "route": "email"}],
        "deadlines": [], "runs": [],
        "followup_tasks": [
            {"id": "old", "action_id": "a", "kind": "remind", "status": "completed",
             "due_at": stamp, "dedupe_key": "old-key"},
            {"id": "new", "action_id": "a", "kind": "remind", "status": "planned",
             "due_at": stamp, "dedupe_key": "new-key"},
            {"id": "reconcile", "action_id": "a", "kind": "reconcile_send",
             "status": "planned", "due_at": stamp}],
        "outbound_attempts": [{"id": "attempt", "task_id": "old", "action_id": "a",
            "dedupe_key": "old-key", "status": state}],
    }
    out = build_queue(case, datetime.fromisoformat(stamp.replace("Z", "+00:00")))
    assert not out["errors"], out
    assert {t["id"] for t in out["due_tasks"]} == {"reconcile"}, out
    assert any(t["id"] == "new" for t in out["held_tasks"]), out
    print(state + ": repeat send held; reconciliation remains available")
PY
```

预期：三个状态都阻止新催办，只读送达核对仍可成为候选。命令没有访问邮箱，不能把其输出写成“已查明送达”或“保证只发送一次”。

## 原案关闭后的独立更正

合成请求：“原事项已经关闭，但先前通知有一处错误。整理更正，不要自行撤回其他主张。”

必要输入包括错误所在的原通知、受影响主张、相反证据、接收方和期望更正。保留原行动及发送历史，冻结有问题的原主张。新更正行动用 `corrects_action_id` 引用原行动，另记更正原因、范围、授权和核验结果。

安全动作是按接收方准备更正。原案终态不消除补救事项；原批准也不授权更正或撤回。缺少更正外发授权时，将发送任务设为 `blocked`，填写 `reason` 和 `review_at`。已有明确更正授权时，仍须对更正材料取得独立的 `1.2` 审批快照。下面只检验：原行动已关闭且无未决送达时，独立更正可以进入队列候选。

```bash
python3 -B - <<'PY'
from copy import deepcopy
from datetime import datetime
from scripts.followup_queue import build_queue

stamp = "2026-11-16T04:00:00Z"
case = {
    "schema_version": "1.3", "case_id": "synthetic-correction",
    "follow_up": {"requested": True, "enabled": True},
    "targets": [{"id": "t", "status": "closed"}],
    "actions": [
        {"id": "original", "status": "verified_effective", "claim_frozen": True,
         "target_ids": ["t"], "provider_id": "p", "route": "email"},
        {"id": "correction", "status": "correction_pending", "target_ids": ["t"],
         "provider_id": "p", "route": "email", "corrects_action_id": "original",
         "authorization_scope_and_source": "synthetic: correction only; no withdrawal"}],
    "deadlines": [], "runs": [], "outbound_attempts": [],
    "followup_tasks": [{"id": "correct", "action_id": "correction",
        "kind": "send_correction", "status": "planned", "due_at": stamp,
        "dedupe_key": "correction-key"}],
}
before = deepcopy(case)
out = build_queue(case, datetime.fromisoformat(stamp.replace("Z", "+00:00")))
assert not out["errors"], out
assert {t["id"] for t in out["due_tasks"]} == {"correct"}, out
assert case == before
assert case["actions"][0]["status"] == "verified_effective"
print("correction: independent candidate; original record remains unchanged")
PY
```

预期：原行动的终态和冻结保持原值。更正只是候选，没有发送或撤回。若同一更正关联组仍有未决发送，新更正同样须等送达核对；关闭原案不能绕过此限制。队列不检查完整审批，须另按[提交前审查](submission-review.md)核验。

## 结案后继续已授权的观察

合成请求：“原案结案后，按已同意的每周范围继续观察。发现新线索先取证，不自动投诉。”

必要输入是独立的 `recurrence_observations` 记录：`id`、`target_ids`、`authorization_source`、`status`、`interval_hours` 和 `next_review_at`。授权来源应能追溯范围、频率和停止条件。观察任务用 `recurrence_observation_id` 关联记录；结案状态和安装 Skill 都不授予观察权限。

安全动作是按授权读取目标和保存本轮证据。`enabled` 才可产生观察候选；`paused`、`ended` 和 `not_authorized` 保持停止状态。出现镜像或新域名时，记录新线索和证据，再核实调查范围、权利和后续动作，不继承旧案侵权结论。

```bash
python3 -B - <<'PY'
from datetime import datetime
from scripts.followup_queue import build_queue

stamp = "2026-11-16T04:00:00Z"
for state in ("enabled", "paused", "ended", "not_authorized"):
    case = {
        "schema_version": "1.3", "case_id": "synthetic-observation",
        "follow_up": {"requested": True, "enabled": True},
        "targets": [{"id": "t", "status": "closed"}],
        "actions": [], "deadlines": [], "runs": [], "outbound_attempts": [],
        "recurrence_observations": [{"id": "watch", "target_ids": ["t"],
            "authorization_source": "synthetic: weekly public observation only",
            "status": state, "interval_hours": 168, "next_review_at": stamp}],
        "followup_tasks": [{"id": "observe", "action_id": None, "target_ids": ["t"],
            "kind": "check_recurrence", "status": "planned", "due_at": stamp,
            "recurrence_observation_id": "watch"}],
    }
    out = build_queue(case, datetime.fromisoformat(stamp.replace("Z", "+00:00")))
    assert not out["errors"], out
    due = {t["id"] for t in out["due_tasks"]}
    assert due == ({"observe"} if state == "enabled" else set()), out
    print(state + ": " + ("observation candidate" if due else "observation held"))
PY
```

预期：仅 `enabled` 状态产生观察候选。候选不证明观察已执行、任务会后台唤醒或反馈源可读。真实邮箱、外发、宿主调度和自然触发仍需分别验收；这些命令未覆盖这些边界。
