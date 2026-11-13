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

证据记录保留响应哈希，但不保存完整响应正文。需要保全原文时，应使用获授权的取证方式另行保存。工具不会把联系人或返回文本当作操作指令。

## 限制与错误

查询默认超时为 10 秒，最大允许 30 秒；这是连接和读取的超时设置，不是全流程时限保证。响应最多读取 1 MiB。工具不自动重试或批量扫描。

`--fresh-hours` 默认为 1，允许大于 0、最多 24 小时。这是内部复核间隔，与注册期限和法律期限无关。来源返回的缓存状态无法确定时保持 `unknown`。

退出码 `0` 表示取得可解析的查询结果，`1` 表示来源失败或响应异常，`2` 表示输入错误。限流后应按服务商规则处理，不得循环重试或更换身份绕过限制。

## 官方依据

入口和字段按 [Verisign 查询说明](https://www.verisign.com/news-insights/registration-data-access-protocol/help/)及 [RFC 9082](https://www.rfc-editor.org/rfc/rfc9082.html)核验。使用时遵守 [Verisign 条款](https://www.verisign.com/legal-center/rdap-terms/)，不要开展高频自动查询。核验日期：2026-11-13。

本版用合成数据检查失败场景，并在发布前查询一次公开的 `example.com`。实际执行结果见发布附件 `VALIDATION.json`。该检查不代表真实案件或全网发现能力已验收。
