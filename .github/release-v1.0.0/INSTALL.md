# Brand Protection Skill v1.0.0 安装说明

本包是独立 Agent Skill，不是浏览器插件、MCP 服务或后台投诉程序。源码固定为 `400e3a00710474d8a321dcd06b1148d910391609`；软件版本 `v1.0.0` 与案件数据格式 `1.3` 是不同概念。许可见包内 `LICENSE`（MIT）。

## 1. 下载并校验

从本仓库的 [v1.0.0 Release](https://github.com/zlxmygit/brand-protection-skills/releases/tag/v1.0.0) 下载 `brand-protection-v1.0.0.zip`、`SHA256SUMS.txt` 和 `VALIDATION.json`。优先使用上述专用 ZIP，不要把 GitHub 自动生成的整个源码归档当成安装目录。

Linux 可执行 `sha256sum brand-protection-v1.0.0.zip`，macOS 可执行 `shasum -a 256 brand-protection-v1.0.0.zip`；Windows PowerShell 执行 `Get-FileHash .\brand-protection-v1.0.0.zip -Algorithm SHA256`。将结果与同一 Release 的 `SHA256SUMS.txt` 中对应文件的值核对。不一致时停止安装。

原 `SHA256SUMS.txt` 列出独立 Skill ZIP 与完整源码 ZIP 的哈希；两个包均下载后可使用 `sha256sum -c SHA256SUMS.txt`（macOS：`shasum -a 256 -c SHA256SUMS.txt`）。仅下载一个包时核对其对应行即可。新增 `VALIDATION-SHA256SUMS.txt` 单独校验 `INSTALL.md` 和 `VALIDATION.json`；不替换原有校验文件。哈希用于检查字节一致性，不是法律证据真实性或独立安全认证。

## 2. 安装独立 Skill

解压后应得到一个 `brand-protection/` 文件夹，其内直接包含 `SKILL.md`、`LICENSE`、`agents/`、`assets/`、`references/`、`scripts/`，共 17 个通用文件。安装说明和补充验证报告作为 Release 的独立附件提供；已发布 ZIP 保持原样，不重新打包。

复制整个文件夹到以下一个位置，不要只复制 SKILL.md：

| 范围 | 目录 |
|---|---|
| 当前项目 | `<项目目录>/.agents/skills/brand-protection/` |
| 当前用户 | `$HOME/.agents/skills/brand-protection/` |

以下为 Linux/macOS 用户级首次安装示例，在下载目录执行；存在旧安装时停止，先按下节备份，避免合并新旧文件：

```bash
mkdir -p "$HOME/.agents/skills"
if [ -e "$HOME/.agents/skills/brand-protection" ]; then
  echo "已有安装，请先备份并移出 Skills 扫描目录。"
else
  unzip brand-protection-v1.0.0.zip -d "$HOME/.agents/skills"
fi
```

Windows PowerShell 首次安装：

```powershell
$skills = Join-Path $HOME '.agents\skills'
$target = Join-Path $skills 'brand-protection'
if (Test-Path $target) { throw '已有安装，请先备份并移出 Skills 扫描目录。' }
New-Item -ItemType Directory -Force -Path $skills | Out-Null
Expand-Archive -LiteralPath '.\brand-protection-v1.0.0.zip' -DestinationPath $skills
```

重新启动或刷新宿主后，用 `$brand-protection` 显式调用。以上路径按 [OpenAI 官方 Skills 文档](https://learn.chatgpt.com/docs/build-skills) 核验；不同客户端的安装位置、权限及刷新方式以其当期说明为准。本包没有插件清单，不宣称可直接作为 ChatGPT 网页端插件安装。

若环境提供 `$skill-installer`，也可要求安装固定版本目录：

```text
使用 $skill-installer 安装：
https://github.com/zlxmygit/brand-protection-skills/tree/v1.0.0/skills/brand-protection
```

安装器可能使用其管理的目录；避免手动安装与安装器产生同名副本。

## 3. 本地冒烟检查

仅运行只读队列需要 Python 3.9+，无需第三方 Python 依赖。Linux/macOS 执行：

```bash
python3 -B "$HOME/.agents/skills/brand-protection/scripts/followup_queue.py" \
  --case "$HOME/.agents/skills/brand-protection/assets/case-record.json"
```

Windows 可将 `python3` 换为 `python`，使用对应本地路径。空白模板的预期结果为 `state: not_enabled`、空待办、无错误、退出码 0；不会发信、创建调度或写回模板。JSON 中中文可能显示为 Unicode 转义，解析后原值保留。

## 4. 首次使用与权限

```text
使用 $brand-protection。
品牌、官方站点、目标市场和资料位置使用我本次明确提供的信息。
先整理证据、核验权利与服务商、准备逐渠道投诉草稿，暂不提交。
填写后的案件及证据保存到仓库和 Skill 安装目录之外的私有目录。
```

真实网页核查需要网络/浏览器工具；读取反馈、对外提交和持续巡检分别需要对应连接器、明确授权及可用调度器。安装本包不授权投诉、签名、付款或后台监测。队列候选不是发送许可；真实外发仍须核对最新反馈、授权、互斥、持久化和送达结果。

## 5. 升级、回滚与隐私

升级前保存现有版本及本地自定义内容，将旧 Skill 备份移到扫描目录之外，再安装新文件夹；不要把两个同名版本同时留在扫描路径。回滚时恢复完整旧文件夹，不能重置真实发送历史或案件状态。

本版保留案件格式 `1.3`，新增可选 `deadline_ids`，不会自动迁移案件。更严格的类型、目标范围、重复键校验可能暴露旧记录错误；应根据可信原件修复，不删除发送记录来绕过阻断。见 `references/case-format.md` 与 `references/queue-safety.md`。

本包仅含通用规则、空白模板和只读脚本，没有真实品牌材料、已填写案件、证书、截图、联系人、邮件、令牌或运行日志。实际资料必须保存在安装目录和公共仓库之外；不要覆盖随包空白模板。
