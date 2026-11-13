# 渠道规则检查

本工具只检查 Cloudflare 版权报告的准备记录。工具不联网、不签署声明，也不提交表单。商标、仅有售假指称、注册商争议、缓存移除和反通知不在本配置范围内。

## 官方依据与使用方法

配置依据为 [Cloudflare 报告说明](https://www.cloudflare.com/trust-hub/reporting-abuse/)和[处理方式](https://www.cloudflare.com/trust-hub/abuse-approach/)，核验日期为 2026-11-13。入口为[官方表单](https://abuse.cloudflare.com/)。页面和表单仍需在实际提交前复查。

在 Skill 目录运行：

```bash
python3 scripts/channel_check.py \
  --case /path/to/private-case/case-record.json \
  --action-id action-1
```

案件、主体、目标、服务商和证据都沿用原台账。输出中的 `checklist_ready_for_review` 仅表示记录满足本配置检查，不表示有权提交或必然移除。

## 服务角色

服务商记录的 `organization` 填写 `Cloudflare`。`role` 只接受 `reverse_proxy` 或 `hosting`，并需有 `verified_at` 和 `attribution_evidence_ids`。一个 IP、响应头或产品名称不能单独证明具体内容由 Cloudflare 托管。

代理角色只接受 `forward_report`，请求转交及可提供的后续信息。实际托管角色接受 `hosted_removal_review`，请求审查指定内容。工具不承诺第三方结果，也不从配置计算法律期限。

## 行动中的审查记录

在待审行动加入 `channel_review`。以下是合成字段示例，时间和确认值需要按实际工作填写。

```json
{
  "profile_id": "cloudflare-copyright-v1",
  "policy_url": "https://www.cloudflare.com/trust-hub/reporting-abuse/",
  "report_url": "https://abuse.cloudflare.com/",
  "checked_at": "2026-11-13T03:00:00Z",
  "review_before": "2026-11-13T05:00:00Z",
  "reviewed_by": "实际复核者",
  "service_role": "reverse_proxy",
  "attribution_verified": false,
  "requested_action": "forward_report",
  "confirmations": {
    "current_form_reviewed": false,
    "contact_fields_reviewed": false,
    "signature_authority_reviewed": false,
    "good_faith_reviewed": false,
    "accuracy_authority_reviewed": false,
    "forwarding_disclosure_reviewed": false
  },
  "work_mappings": [
    {
      "target_id": "target-1",
      "right_id": "right-1",
      "reported_url": "https://suspected.example/item",
      "location_description": "页面中具体作品的位置",
      "comparison_evidence_ids": ["evidence-1"]
    }
  ]
}
```

`checked_at` 是本次核对时间。`review_before` 是内部复核点，不是服务商承诺或法律期限。晚于渠道审查的服务商核验记录，会要求重新审查。

每个行动目标至少对应一项作品和位置。`reported_url` 必须与该目标已记录的原始或最终页面地址相同。权利类型必须是同品牌的 `copyright`，权利人须已建档，并填写 `specific_work` 和 `original_reference`。比较证据只能引用本案记录。

## 声明、披露与审批

配置列出需要准备的通知字段，不代填签名或法律声明。只有实际完成当前表单、联系人、签署权限、善意、准确性及转交披露审查后，才能把对应值改为 `true`。

版权报告可能转交主机和网站运营者，不能承诺匿名。对外只披露必要材料，内部审查记录留在私有目录。字段通过检查不证明复核者身份、权属或归属判断真实。

带有 `channel_review` 的行动会把该记录纳入[提交前材料快照](submission-review.md)。记录发生变化后，原审批不再匹配；没有新增字段的旧案件保持原快照规则。

本工具不检查全部发送状态。实际行动仍需核对最新反馈、审批范围和[队列规则](queue-safety.md)。退出码 `0` 表示清单检查通过，`1` 表示缺少条件或输入错误。

## v1.8.1 校验修正

升级后的材料依赖、品牌及目标关联、首次通知和输入输出检查见[审计修复说明](audit-fixes.md)。旧审批快照需要重新复核，不自动改写私有案件。
