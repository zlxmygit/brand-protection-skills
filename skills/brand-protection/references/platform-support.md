# macOS、Windows 和 Linux

## 运行要求

三个系统使用同一个通用 ZIP。运行脚本只依赖 Python 标准库，不需要 WSL、Docker、Node.js 或第三方 Python 包。推荐使用 Python 3.13。Python 3.9 仅保留兼容回归，不建议新部署使用已经结束安全维护的版本。

CI 的原生平台矩阵如下。配置存在不等于测试通过；准确结果见对应提交的 GitHub Actions 和 Release 附件。单个任务会记录发现、通过及跳过的测试数，平台专属跳过项不计作通过。

| 平台 | 架构 | Python | 检查范围 |
|---|---|---|---|
| macOS 15 | Apple Silicon / arm64 | 3.13 | 全量回归、安装包、zsh、公开接口检查 |
| macOS 15 | Intel / x86_64 | 3.13 | 全量回归、安装包、zsh、公开接口检查 |
| Windows Server 2025 | x64 | 3.13 | 全量回归、安装包、Windows PowerShell / PowerShell、公开接口检查 |
| Ubuntu 22.04 和 24.04 | x86_64 | 3.13 | 全量回归、安装包、Bash、公开接口检查 |
| Ubuntu 22.04 | x86_64 | 3.9、3.11 和 3.14 | Python 兼容回归 |

Windows 10/11、其他 Linux 发行版、旧版 macOS、Windows ARM64 和 Linux ARM64 未纳入该矩阵。不能将上述结果宣称为这些系统的实机验收。不同宿主的 Skill 发现、授权和沙箱机制也需要分别确认。

## 命令和路径

在 macOS 或 Linux 的终端中，使用 `python3` 显式运行脚本；不依赖可执行位或双击文件。以下命令只读取空白模板，不联网、不写案件。

```bash
python3 "$HOME/.agents/skills/brand-protection/scripts/followup_queue.py" --case "$HOME/.agents/skills/brand-protection/assets/case-record.json"
```

Windows 使用 PowerShell。安装 Python 3.13 且提供 `py` 启动器时：

```powershell
$Skill = Join-Path $HOME '.agents\skills\brand-protection'
py -3.13 (Join-Path $Skill 'scripts\followup_queue.py') --case (Join-Path $Skill 'assets\case-record.json')
if ($LASTEXITCODE -ne 0) { throw '队列检查失败，请检查 JSON 中的 errors。' }
```

没有 `py` 启动器时，用已确认版本的 `python` 替换 `py -3.13`。文档中的 Bash 多行反斜杠不是 PowerShell 的续行符；Windows 可把命令写成一行。脚本路径和案件路径含空格时必须作为一个参数传入。

本地路径支持中文和空格。JSON 中的 Windows 路径可写成 `C:/Users/name/Private Cases/evidence.bin`，或使用转义后的双反斜杠。相对证据路径基于明确的 `--source-root`，与当前工作目录无关。案件文件使用 UTF-8，可带 BOM；LF 和 CRLF 均可读取。JSON 报告使用 ASCII 转义，解析后中文不丢失。

## 私有目录和链接

安装目录只放通用文件。真实品牌、证书、案件、证据、审批和运行输出放在仓库及宿主 Skill 扫描目录之外。私有根目录必须事先存在，不能使用 `..` 绕过目录边界。

macOS 的 `/tmp` 和 `/var` 常指向 `/private/tmp` 和 `/private/var`。工具仅接受这两处路径、准确目标且所有者为 root 的系统别名。其余用户符号链接仍会被拒绝。测试临时目录不适合长期存储证据。

Windows 仅支持普通本地磁盘目录。目录联接、其他重解析点、UNC 共享、设备路径、备用数据流、盘符相对路径及保留文件名会被拒绝。云盘占位文件或带目录联接的同步目录可能触发拒绝；应使用已落盘的普通本地私有副本，不关闭路径检查。

macOS 和 Linux 创建的私有文件使用 `0600`，新建工作子目录使用临时目录的受限权限。Windows 的 `0600` 不等于 NTFS 访问控制列表。应在存放资料前核对根目录 ACL，限制普通其他账户访问，并检查子目录继承；工具不自动修复已有目录权限。根目录也不能允许不可信进程并发替换父目录。

## 升级、迁移和排错

升级前，将旧安装目录移到扫描路径之外备份，再复制完整新目录。不要覆盖混装。跨系统迁移时，先备份私有案件，校验复制后的证据字节，再按真实位置调整路径。路径变化可能改变材料快照，必须重新核对审批；本版不自动迁移案件或更新批准哈希。

权限被拒绝时，先核对私有目录和宿主沙箱许可，不要默认以管理员身份运行。HTTPS 检查仍要求有效证书及 CA 信任链；缺少信任证书时修复 Python 或企业证书配置，不能关闭 TLS 验证。没有网络权限仍可使用本地队列、审查、导出和看板，不能把网络失败认作没有侵权或已下架。

网络查询、采集、外发和定时运行的原有授权边界保持不变。跨平台测试不代表已经验收真实投诉渠道或无人值守执行。

## 参考

- [GitHub 原生运行器说明](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
- [Python 文件权限说明](https://docs.python.org/3.13/library/os.html#os.chmod)
- [Python 版本维护状态](https://devguide.python.org/versions/)
