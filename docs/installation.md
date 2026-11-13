# 安装 {{tag}}

下载本版的 `brand-protection-{{tag}}.zip` 和 `SHA256SUMS.txt`。安装包只包含通用 Skill、空白模板和辅助脚本。真实案件应放在安装目录之外。

## 核对下载文件

Linux 可用 `sha256sum brand-protection-{{tag}}.zip`，macOS 可用 `shasum -a 256 brand-protection-{{tag}}.zip`。Windows PowerShell 可用：

```powershell
Get-FileHash .\brand-protection-{{tag}}.zip -Algorithm SHA256
```

将结果与 `SHA256SUMS.txt` 中对应文件的值比较。只有下载了清单内全部文件时，才运行 `sha256sum -c SHA256SUMS.txt`。

## 安装或升级

解压后得到 `brand-protection/` 文件夹。把整个文件夹放到项目的 `.agents/skills/` 或用户目录的 `~/.agents/skills/`。下一层应直接包含 `SKILL.md`。保留 `LICENSE`、`agents`、`assets`、`references` 和 `scripts`。

升级前，将旧目录备份到宿主扫描路径之外。再复制新目录，避免新旧脚本混用。不要覆盖私有案件。重启或刷新宿主后，用 `$brand-protection` 调用。

支持内置安装器的 Codex 环境也可使用固定版本地址：

```text
使用 $skill-installer 安装以下 Skill：
https://github.com/zlxmygit/brand-protection-skills/tree/{{tag}}/skills/brand-protection
```

安装路径以宿主实际结果为准。发现机制参考 [OpenAI 官方文档](https://developers.openai.com/codex/skills)。本包不包含邮箱连接器、后台服务或自动发送器。

## 运行与检查

辅助脚本使用 Python 3.9 及以上版本，仅依赖标准库。在 Skill 目录运行：

```bash
python3 -B scripts/followup_queue.py --case assets/case-record.json
python3 -B scripts/investigation.py --case assets/case-record.json
```

空白模板应分别返回未启用跟进和没有调查记录。检查不会启用调度或修改文件。Windows 可将 `python3` 换成可用的 `python`。

正式使用时，传入私有案件路径。按本版说明处理字段错误。不要把候选数量、检查通过或模板完成当作投诉成功。

## 回滚与验证范围

需要回滚时，移走新安装目录，再恢复备份。代码回滚不会恢复已修改的案件。维护案件前应单独备份；本版脚本不修改案件。

`VALIDATION.json` 记录源码与解压安装包的测试结果。`PACKAGE-MANIFEST.json` 记录文件清单和哈希。自动测试使用合成数据，真实连接器、外发和定时运行需要另行验收。
