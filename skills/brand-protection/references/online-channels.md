# 线上渠道与能力边界

官方资料核验基准：2026-11-12。以下是入口，不是永久有效的表单、必胜承诺或目标站点的服务商认定。准备实际提交时，重新打开相关官方政策/表单，记录日期、必填字段、费用、披露和申诉方式；不需要每次加载所有渠道。

## 内容、主机和平台

| 路径 | 何时使用与证据 | 入口及限制 |
|---|---|---|
| 版权/美国 DMCA | 可主张的图片、视频、原创文案或代码被复制；附权属和逐项 URL 对照 | [美国版权局 Section 512](https://www.copyright.gov/512/)；[DMCA 代理查询](https://dmca.copyright.gov/osp/)。不是通用商标或假货通知；不要求所有作品先登记才可发通知。各法域/平台程序另查。 |
| 商标/冒充官网 | 标志使用、商品范围、受保护地区、误导的官方关系及混淆证据 | 向已核实主机/平台的 trademark、impersonation 或 IP 入口；品牌词出现本身不是充分理由。具体登记与未登记权利适用性逐渠道核实。 |
| 源站主机/VPS | 已识别实际托管方，内容与其服务有关 | 使用该服务商正式 abuse/IP/DMCA 入口；投诉请求限于其实际能力。IP 所属网络或上游不是当然的内容托管方。 |
| CDN/反向代理 | 已证实使用对应服务，源站未知或需协调转交 | [Cloudflare 处理方式](https://www.cloudflare.com/trust-hub/abuse-approach/)区分反向代理与实际托管产品。通常可转交给主机/运营者；不能要求其删除不托管的原站内容。 |
| 建站平台 | 确认目标实际使用该平台且有具体侵权页面 | [Shopify IP 总入口](https://help.shopify.com/en/manual/compliance/intellectual-property)、[版权](https://help.shopify.com/en/manual/compliance/intellectual-property/copyright-policy)、[商标/商业外观](https://help.shopify.com/en/manual/compliance/intellectual-property/trademark-trade-dress-policy)。商品/页面逐一列出；总店铺链接不能替代具体侵权 URL。 |

商标、商业外观、外观设计和版权的构成不同。网站“长得像”不自动满足商业外观的显著性、非功能性等条件；复杂主张准备事实包供适格律师判断。

Cloudflare 可能转发投诉及联系信息；版权/商标报告不能假定匿名。提交前核实当次表单披露说明。其托管服务可能采用类似 DMCA 的流程，不能据此宣称 DMCA 法律本身覆盖商标。[官方报告字段说明](https://developers.cloudflare.com/api/resources/abuse_reports/methods/create/)

## 搜索、广告、社交和交易平台

| 路径 | 所需材料与结果边界 | 官方起点 |
|---|---|---|
| 搜索版权移除 | 原作品和被索引的具体侵权 URL；结果是对应搜索服务处理 | [Google Legal Help](https://support.google.com/legal/)；移除索引不等于删除源站，其他搜索引擎分别提交。 |
| 搜索假货移除 | 具体售假商品页、商标和假货证据；普通商标纠纷不走此类型 | [Google Counterfeit Overview](https://support.google.com/legal/answer/9922110?hl=en)。不得把品牌词/未授权经销直接填为假货。 |
| 广告/购物商品 | 广告 ID、广告主、落地页、展示地区/日期、对应品牌与商品证据 | [Google Ads 假货政策](https://support.google.com/adspolicy/answer/176017?hl=en)。Search、Ads、Shopping 等按产品入口处理；品牌关键词触发广告不当然构成可受理侵权。 |
| 电商平台 | 卖家 ID、ASIN/商品 ID、变体、实际商品链接、权利与交易鉴定证据 | [Amazon Report a Violation](https://sell.amazon.com/blog/announcements/report-a-violation)、[Amazon IP 规则](https://sellercentral.amazon.com/help/hub/reference/external/GU5SQCEKADDAQRLZ?locale=en-En)、[eBay IP/VeRO](https://www.ebay.com/sellercenter/resources/intellectual-property)。先核实角色和地区，勿把导购域名等同于平台卖家。 |
| 社交账号/内容 | 账号 URL/ID、帖子/视频/广告 ID、冒充声明和权利材料 | 针对 Meta/Instagram、TikTok、YouTube 等实际发现的平台，现场查官方 impersonation、trademark、copyright 或广告渠道。不同内容类型分开；投诉账号不等于删掉独立站。 |
| 钓鱼/恶意网站 | 欺骗性收集凭据/支付资料或恶意软件的具体证据 | 现场核实 Google Safe Browsing、Microsoft 等官方安全举报入口。仿站/商标争议不自动满足钓鱼标准，不用安全举报替代 IP 证据。 |

## 支付与交易支持

- **PayPal：** 使用 [Infringement Report Policy](https://www.paypal.com/us/legalhub/paypal/infringementrpt-full) 对应的权利人表单；选择适用地区版本，证明目标当前使用其服务。品牌方侵权投诉与买家的退款/争议程序分开，不能虚构买家身份。
- **Stripe：** [IP Notice Process](https://stripe.com/legal/ip-policy)要求证据连接“使用 Stripe 的交易”与“侵权商品/服务”。仅网站名称、Logo 或设计纠纷可能不适合此程序；残留脚本不是当前使用的充分证明。依政策说明处理商家自身通知程序。测试购买可有帮助但不是必需，本 Skill 不自动授权购买。Stripe 能决定提供服务与否，不能替你删除卖家的网页；不要重复提交同一事项或附完整卡号。
- **其他收单行/支付商/卡组织/BNPL：** 有当前交易关联再核实官方规则、接收资格与合适入口。不能从 Visa/Mastercard 图标推导实际收单行；不要承诺冻结账户或退回款项。
- **物流/仓储/履约商：** 有包裹、仓库、提单等线索时，先保全证据，再按其真实能力准备侵权告知或与海关/执法衔接；运单隐私与经营主体归属需核查。

## 拒绝、反通知及升级

提交后持续检查、催办频率、去重、重提条件及调度恢复统一遵循 [持续跟进规则](follow-up.md)，渠道政策和具体通知优先于运营默认值。

按拒绝原因修正：权属不足→补权利链；URL 不具体→逐页补充；收件方不托管→更新归属；真实转售/合理使用争议→调整主张或法律评估。保留原投诉和拒绝决定，不通过换类别、换身份重复举报绕过实体结论。

DMCA 合格反通知涉及服务商收件后 10–14 个工作日的恢复窗口及特定诉讼通知条件；记录实际通知和服务商期限，不把“将要起诉”“回复不同意”写成已提起诉讼。立即整理权属、原通知、反通知及争点，交适格律师判断。商标平台回复流程不能机械套用这个期限，尤其 Shopify 商标回复与版权反通知不同。

欧盟平台的内部投诉、DSA 非诉争议解决和合规投诉见 [地区参考](legal-regions.md)。平台 SLA、法定期限和自行设定的复查日应分别标注；没有来源时不得承诺“48 小时必下架”。

## 品牌恢复与预防（仅按请求执行）

准备官方域名/授权卖家验证页、消费者举报入口和客服辨识话术；只发布可证实事实，避免未经核验的黑名单。可评估重要拼写域名防御、品牌/商标登记、原始素材管理及冒名邮件的 SPF/DKIM/DMARC；购买域名、改 DNS、公开公告和长期监测须在相应授权范围内。上述预防并不能替代对他人站点的证据与投诉。

专利与外观设计走 [专项分流](patents-designs.md)；版权投诉可能进入反通知程序时，按 [诉讼准备字段](evidence.md#dmca-反通知前的诉讼准备) 提前记录缺口。已发通知后来发现错误的更正/撤回统一遵循 [持续跟进规则](follow-up.md)，不以本地关闭代替外部补救。
