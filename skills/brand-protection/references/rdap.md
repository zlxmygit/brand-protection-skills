# 域名注册查询

本工具通过 RDAP（Registration Data Access Protocol，注册数据访问协议）查询域名注册信息。当前只接入 Verisign 的 `.com` 和 `.net` 入口。

## 使用方法

在 Skill 目录运行以下命令。先确认你有权限调查该域名，且允许向注册局提交域名查询。

```bash
python3 scripts/rdap_lookup.py --domain example.com --target-id target-1 --allow-network
```

`example.com` 是公开测试域名。实际使用时，替换为本案已登记的域名和目标 ID。工具只接受显式二级域名，不把 `www.example.com` 自动改成 `example.com`。

查询不需要密钥。工具只向固定的 Verisign 主机发送该域名，不发送案件内容、证书或联系人。它不访问返回结果中的链接，不跟随重定向，也不读取代理环境变量。

## 保存结果

输出中的 `result` 可放入本案的 `investigation.results`，`evidence` 可加入本案证据列表。先核对目标、域名和 ID，再按案件写入规则保存。工具不会替你修改台账。

注册局返回匹配的域名对象时，结果标记为 `registered`。合格的 404 响应标记为 `not_found`，仅表示该入口未查到。其他错误保持 `unknown`。这些结果都不证明权属、侵权、安全或域名可购买。

默认只输出响应哈希，`original_path` 为 `null`，不会保存完整响应正文。该记录不能作为已保全原文的文件依赖。工具不会把联系人或返回文本当作操作指令。

需要保全原文时，显式传入仓库外已存在的私有目录：

```bash
python3 scripts/rdap_lookup.py --domain example.com --target-id target-1 --allow-network --archive-root /path/to/private-case/evidence
```

`--archive-root` 只允许已有目录。工具拒绝父级跳转、用户创建的符号链接，以及 Skill 或本仓库内的路径。它在私有目录中新建独立的 `rdap-*` 目录，以排他方式创建 `response.bin`，保留收到的原始字节，不改写编码、空格或换行。已有文件不会被覆盖。

保存后，工具用后续审查所用的证据读取器读回文件，并核对原文字节与 SHA-256。成功时，证据的 `original_path` 为绝对路径，`disclosure_class` 保持 `restricted`。报告包含 `read_only: false`、`writes_archive: true` 和 `writes_case: false`。归档不修改案件，也不授予投诉权限。确认目标和证据后，再按原有规则写入本案。

私有目录校验失败时，工具在联网前停止。写入或读回校验失败时，报告返回 `archive_unavailable`，不输出成功证据；注册状态保持 `unknown`，证据引用保持空列表。工具尝试清理本次新建的目录，保留其他文件。清理也受存储权限限制，残留文件不能当作已验证证据。修复目录权限或存储问题后，人工决定是否重新查询，工具不会自动重试。

Python 调用可使用 `lookup(..., archive_root=private_directory)`。省略该关键字参数时，旧调用继续只读运行，`read_only` 为 `true`，`writes_archive` 为 `false`。原有 `result`、`evidence` 和错误字段继续保留。

## 限制与错误

查询默认超时为 10 秒，最大允许 30 秒；这是连接和读取的超时设置，不是全流程时限保证。响应最多读取 1 MiB。工具不自动重试或批量扫描。

`--fresh-hours` 默认为 1，允许大于 0、最多 24 小时。这是内部复核间隔，与注册期限和法律期限无关。来源返回的缓存状态无法确定时保持 `unknown`。

退出码 `0` 表示取得可解析的查询结果。显式归档时，还要求保存及读回校验成功。退出码 `1` 表示来源、响应或归档失败，`2` 表示输入错误或私有目录不可用。限流后应按服务商规则处理，不得循环重试或更换身份绕过限制。

## 官方依据

入口和字段按 [Verisign 查询说明](https://www.verisign.com/news-insights/registration-data-access-protocol/help/)及 [RFC 9082](https://www.rfc-editor.org/rfc/rfc9082.html)核验。使用时遵守 [Verisign 条款](https://www.verisign.com/legal-center/rdap-terms/)，不要开展高频自动查询。核验日期：2026-11-13。

本版用合成数据检查失败场景，并在发布前查询一次公开的 `example.com`。实际执行结果见发布附件 `VALIDATION.json`。该检查不代表真实案件或全网发现能力已验收。

## v1.8.1 校验修正

升级后的材料依赖、品牌及目标关联、首次通知和输入输出检查见[审计修复说明](audit-fixes.md)。旧审批快照需要重新复核，不自动改写私有案件。
