# 支持与反馈

本仓库维护可复用的品牌维权 Skill、模板和队列工具。问题反馈用于改进项目；公共 Issues 不是向服务商提交投诉的入口，也不是法律委托或品牌案件受理渠道。

## 选择入口

| 需求 | 入口 |
|---|---|
| 安装、准备资料、运行条件 | [README](README.md)、[品牌与市场表单](skills/brand-protection/assets/intake-form.md) |
| 持续跟进、状态与期限 | [跟进协议](skills/brand-protection/references/follow-up.md)、[案件格式](skills/brand-protection/references/case-format.md) |
| 文档错误、脚本异常 | [Bug 报告](https://github.com/zlxmygit/brand-protection-skills/issues/new?template=bug_report.yml) |
| 新市场、新渠道或功能建议 | [功能建议](https://github.com/zlxmygit/brand-protection-skills/issues/new?template=feature_request.yml) |
| 官方入口失效或政策变化 | [渠道与规则更新](https://github.com/zlxmygit/brand-protection-skills/issues/new?template=channel_update.yml) |
| 漏洞或隐私泄露 | [SECURITY.md](SECURITY.md) |
| 一般使用问题 | 搜索 [已有 Issues](https://github.com/zlxmygit/brand-protection-skills/issues)，没有匹配问题时新建脱敏问题 |

## 提供有效信息

说明所用的 commit SHA 或 tag、安装方式、Agent/客户端版本、操作系统，以及涉及队列时的 Python 版本。给出最小复现步骤、期望结果、实际结果和脱敏错误。使用虚构案件替代真实证据；不要直接上传完整私有目录、邮箱记录或案件 JSON。

维护者不承诺固定回复时间，不提供紧急值守。存在法院、海关、反通知等正式期限时，请通过用户已经选定的主管机构、服务商或专业顾问处理，不能依赖 GitHub Issue 的响应速度。

## 能力边界

- Skill 不自带邮箱、云主机、定时服务或投诉发送器；这些能力需要宿主工具、连接器和对应授权。
- Python 队列只读取和分类本地记录，不访问网络，不提交投诉，也不持久化案件状态。
- 法律与平台参考需要按具体市场和提交时点核验；项目不保证任何投诉、域名争议或扣货结果。
- 实际品牌材料、案件、密钥及运行账户由使用者保管，不属于仓库收集范围。
