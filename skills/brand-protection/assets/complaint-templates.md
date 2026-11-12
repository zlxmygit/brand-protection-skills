# 投诉与升级模板

模板用于生成独立案件材料，不直接群发。本文件中 `{{字段}}` 是待填项目，不是已经确认的事实。保留内部草稿标识；只有所需字段、事实、证据、收件人、声明和权限均核实后，才生成不含占位符的最终文本。正式表单要求优先。没有具体作品、权利链或鉴定证据时，不让流畅措辞掩盖缺失。

## 每份材料附带的内部检查

- 接收方/角色、官方入口和最近核验日：{{recipient_and_policy}}
- 请求措施及其实际能力：{{requested_action}}
- 具体对象、原作品/权利和逐项证据：{{claim_mapping}}
- 权利人、代理及签署权限：{{authority}}
- 对方可能的授权/转售/合理使用及相反证据：{{assessment}}
- 提交文本版本、附件及披露范围：{{version_and_disclosure}}
- 费用、法律声明、已有用户授权及仍缺项目：{{permission_and_gaps}}

## A. Copyright / DMCA draft

Subject: Copyright infringement notice — {{domain}} — {{case_id}}

To {{verified_service_provider_or_designated_agent}},

I am {{legal_name}}, {{copyright_owner_or_authorized_representative}} of {{rights_owner}}.

The copyrighted work(s) at issue are {{specific_original_works}}. Ownership or authorization is supported by {{evidence_references}}. Original reference locations are {{original_urls_or_descriptions}}.

The material requested for removal or disabling is identified below:

| Original work | Reported URL / exact location | Copying identified | Evidence |
|---|---|---|---|
| {{work}} | {{reported_url}} | {{specific_comparison}} | {{evidence_id}} |

Please remove or disable access to the identified material within your service and provide a reference for this notice.

Contact: {{legal_name_business_address_email_phone}}

{{Insert the applicable good-faith statement after the signer verifies authorization and legal exceptions.}}
{{Insert the applicable accuracy and authority statement, including required penalty-of-perjury language, only after the authorized signer confirms it.}}

Signature: {{authorized_signature}}
Date: {{date}}

注意：最终通知需满足适用规则，尤其善意、准确性、宣誓范围、签名与可定位内容要求。声明不是可选项；尚未确认则整份材料保持草稿。按 [版权局官方要求](https://www.copyright.gov/512/) 与收件方表单生成，不将纯商标或商品真伪指控伪装成版权作品。

## B. Trademark / impersonation / counterfeit draft

Subject: {{trademark_or_impersonation_or_counterfeit}} report — {{target}} — {{case_id}}

To {{verified_recipient}},

{{rights_owner}} owns or controls the following rights: {{mark_registration_territory_goods_status_or_supported_other_rights}}. I act as {{capacity_and_authorization}}.

Our official reference is {{official_reference}}. The reported pages/accounts are {{exact_urls_and_ids}}.

Observed conduct: {{specific_observations_with_dates}}.
Rights concern and territorial connection: {{explained_basis}}.
Evidence: {{mark_and_use_comparison}}.

{{For counterfeit claims only: identify the actual product/seller and explain the supported authenticity findings, samples or other reliable evidence. Omit unsupported counterfeit allegations.}}

We request {{action_within_recipient_authority}}. Supporting documents: {{minimal_attachment_index}}.

{{Insert recipient-required good-faith, accuracy and authority declarations only after confirmation.}}
Contact and authorized signature: {{verified_details}}

## C. Host / CDN / payment escalation

Subject: {{case_id}} — {{specific_policy_issue}} concerning {{reported_urls}}

The evidence at {{evidence_ids}} indicates that {{target}} currently uses your {{verified_service_and_role}}. The specific reported conduct is {{supported_facts}}, involving {{identified_rights_or_policy}}.

Please {{specific_request}}. Where your role is limited to proxy/CDN services, please route this report under your published process to the responsible hosting provider and provide available follow-up information.

For a payment-related report, the connection between your current service and the identified goods is {{payment_evidence}}. This is a rights-owner report; {{state any transaction only if supported by an actual record}}.

Prior case/reference and response: {{ticket_history_if_any}}.
Evidence and contact: {{attachments_and_authorized_contact}}.

根据实际接收方删除不适用段落；不能把 CDN 转交请求发给银行，或把支付终止写成网页下架。Stripe 等指定表单优先，网页设计纠纷与交易商品侵权分开。

## D. Refusal / request for more information

Subject: Supplement to {{ticket_id}} — {{reported_url}}

Your response dated {{date}} requested or concluded {{accurate_summary}}.
We provide the following additional information addressing that point: {{new_evidence_and_explanation}}.
The original request remains limited to {{supported_scope}}.
Please review under {{verified_policy_or_appeal_route}} and advise whether further information is required.

若新证据不足，保留驳回结论并说明缺口；不要用更强措辞代替证据。

## E. Formal dispute / customs / counsel handoff

使用具体机构当期正式表格；本清单是附件组织方式，不替代起诉状、UDRP complaint、AFA 或 RFA。

1. Requested remedy, jurisdictions, parties and representation.
2. Rights schedule with status and ownership/authorization documents.
3. Chronology, exact domains/listings/sellers and independently supported relationships.
4. Element-by-element grounds, contrary evidence and unresolved questions.
5. Original evidence index, copies, translations and integrity information.
6. Actual orders, samples/authentication, shipments and losses, where available.
7. Prior notices, decisions, counter-notices and live deadlines with source notices.
8. Fees, costs/undertakings, signer authority and requested professional decisions.

海关另附真伪辨识、授权制造商/进口商、货运线索与紧急联系。正式法律文件中的程序选择、管辖、宣誓和费用承担均须由有权限的人确认。

## F. Follow-up on an existing notice

Subject: Follow-up on {{existing_ticket_id}} — {{specific_reported_target}}

We are following up on our notice delivered on {{verified_delivery_date}}, reference {{existing_ticket_id}}. Our latest check on {{date_and_timezone}} found {{specific_current_observation_and_limits}} at {{reported_urls}}.

{{If supported: identify the published or case-specific response date and explain that it has passed. Otherwise simply request a status update without asserting a missed legal deadline.}}

Please confirm the current status and whether you need any additional information to assess the existing report. {{Include only relevant new evidence or a response to a specific request.}}

Original notice/reference: {{reference}}.
Authorized contact: {{contact}}.

回复原工单，使用已有授权与最新事实；发送前复查反馈和去重记录。仅在已验证的重提条件下改为新通知，并说明与旧案的关系，不因无回应机械重发整份投诉。

## G. Correction / withdrawal of an earlier notice

先按 [更正与撤回协议](../references/follow-up.md) 确认原通知、已送达渠道、纠正依据和权限。每个接收方独立起草；只保留适用的更正、部分撤回或全部撤回段落，不能把用户停止工作的要求直接改写为对外撤回。

Subject: {{Correction_or_withdrawal}} of notice {{existing_ticket_id}} — {{specific_targets}}

To {{verified_original_recipient_or_official_correction_channel}},

I am {{verified_name_and_capacity}}, authorized to make this {{correction_or_withdrawal}} concerning our notice delivered on {{verified_delivery_date}}, reference {{existing_ticket_id}}.

The affected statement(s), claim(s) and URL(s) are: {{exact_scope_from_original_notice}}.

{{For a correction: identify each inaccurate statement and provide its verified replacement, with only the evidence necessary to explain the correction.}}

{{For a withdrawal: state explicitly whether the entire notice or only the identified claims/URLs is withdrawn. Do not imply withdrawal of unrelated claims without authority.}}

The basis for this update is {{verified_facts_and_minimal_evidence}}. Please record this {{correction_or_withdrawal}} against the original notice and review any resulting measures within your control under your applicable process.

Please confirm receipt and explain the status of any relevant restoration or other corrective action. {{If applicable under your process: please forward this update to the recipients to whom you forwarded the original notice and confirm the available details.}}

Original notice/reference: {{notice_version_and_ticket}}.
Authorized contact and any required signature: {{verified_details}}.

最终文本不得加入未经授权的法律承认、和解、弃权、签名或宣誓。保留原通知，补救行动以 `corrects_action_id` 关联原行动，发送使用 `send_correction` 并遵守同一持久化/去重协议。分别记录送达、受理、措施复核及不可见部分；收到撤回回执不等于全部限制已解除。
