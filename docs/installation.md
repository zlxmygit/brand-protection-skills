# 安装 Brand Protection Skill {{tag}}

## 环境与下载

macOS、Windows 和 Linux 使用同一个 `brand-protection-{{tag}}.zip`。辅助脚本仅依赖 Python 标准库，推荐 Python 3.13。运行 Python 脚本不需要 WSL、Docker、管理员权限或额外的 `pip install`。

本版原生 CI 包含 macOS 15（Apple Silicon 和 Intel）、Windows Server 2025 x64、Ubuntu 22.04 / 24.04 x86_64。其他系统版本及宿主应用不等于已实机验收，具体结果以同提交的 GitHub Actions 为准。包内 `references/platform-support.md` 列出范围和限制。

从本版 GitHub Release 下载五个附件，保存在同一下载目录：ZIP、`INSTALL.md`、`PACKAGE-MANIFEST.json`、`VALIDATION.json` 和 `SHA256SUMS.txt`。不要将开发分支当作正式发布。校验值用于核对下载完整性，不代替可信来源核验。

## macOS 和 Linux

在终端切换到上述下载目录，先核对 Python。`python3` 应指向已安装的兼容版本，不要假设系统自带 Python。

```bash
python3 --version
```

macOS 使用：

```bash
shasum -a 256 -c SHA256SUMS.txt
```

Linux 使用：

```bash
sha256sum -c SHA256SUMS.txt
```

四项校验均为 `OK` 后才解压。以下命令拒绝覆盖已有安装；升级时先按“升级与回滚”备份旧目录。

```bash
(
  set -eu
  destination="$HOME/.agents/skills/brand-protection"
  if [ -e "$destination" ] || [ -L "$destination" ]; then
    echo "已有安装。先将旧目录备份到扫描路径之外。" >&2
    exit 1
  fi
  stage=$(mktemp -d)
  python3 -m zipfile -e "brand-protection-{{tag}}.zip" "$stage"
  mkdir -p "$HOME/.agents/skills"
  cp -R "$stage/brand-protection" "$destination"
  python3 "$destination/scripts/followup_queue.py" --case "$destination/assets/case-record.json"
)
```

最后一条命令只读取空白模板，不联网，不修改案件。目录含空格或中文时保留引号。文件通过 Python 显式执行，不需要 `chmod +x`。

## Windows

在原生 Windows PowerShell 5.1 或 PowerShell 7 中进入下载目录。安装 Python 3.13 后，先检查：

```powershell
py -3.13 --version
```

没有 `py` 启动器时，先用 `python --version` 确认解释器，再将以下命令的 `py -3.13` 替换为 `python`。不要在 PowerShell 中照抄 Bash 的多行反斜杠。

检查四个下载文件：

```powershell
$ErrorActionPreference = 'Stop'
Get-Content -LiteralPath '.\SHA256SUMS.txt' | ForEach-Object {
    $Expected, $Name = $_ -split '\s+', 2
    $Actual = (Get-FileHash -LiteralPath $Name -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($Actual -ne $Expected) { throw "校验失败：$Name" }
    Write-Output "OK: $Name"
}
```

校验全部通过后执行以下安装命令。目标存在时会停止，不覆盖混装。

```powershell
$ErrorActionPreference = 'Stop'
$Collection = Join-Path $HOME '.agents\skills'
$Destination = Join-Path $Collection 'brand-protection'
if (Test-Path -LiteralPath $Destination) {
    throw '已有安装。先将旧目录备份到扫描路径之外。'
}
$Stage = Join-Path ([IO.Path]::GetTempPath()) ('brand-protection-install-' + [guid]::NewGuid().ToString('N'))
Expand-Archive -LiteralPath '.\brand-protection-{{tag}}.zip' -DestinationPath $Stage
New-Item -ItemType Directory -Path $Collection -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $Stage 'brand-protection') -Destination $Destination -Recurse
py -3.13 (Join-Path $Destination 'scripts\followup_queue.py') --case (Join-Path $Destination 'assets\case-record.json')
if ($LASTEXITCODE -ne 0) { throw '队列检查失败，请检查 JSON 中的 errors。' }
```

不要求修改 PowerShell 执行策略。命令直接启动 Python，不执行下载的 `.ps1` 文件。Windows 10/11 可按同类本地 Python 流程验证，但本版 CI 的 Windows 实机是 Server 2025，不能把两者混称为全部验收通过。

## 宿主加载和私有目录

安装后的结构应为 `.agents/skills/brand-protection/SKILL.md`，不要多嵌套一层同名目录。项目级安装可改放当前项目的 `.agents/skills/`。同一宿主扫描范围内只保留一份该 Skill。

刷新或重启支持 Agent Skills 的宿主，按其规则调用 `$brand-protection`。安装目录及发现方式参考 [OpenAI 官方说明](https://developers.openai.com/codex/skills)；其他宿主按各自文档配置。脚本测试通过不代表每个宿主的发现和沙箱许可都已验收。

真实品牌资料、证书、案件、审批、证据和报告放在安装目录、源码仓库及宿主扫描目录之外。私有根目录须事先存在。macOS / Linux 可在个人目录中创建受限目录；Windows 使用普通本地目录，并核对 NTFS ACL 和子目录继承。`0600` 在 Windows 上不代表已经限制其他账户访问，详见 [Python 说明](https://docs.python.org/3.13/library/os.html#os.chmod)。

私有路径支持中文和空格。拒绝用户符号链接、Windows 目录联接、UNC 共享、设备路径和备用数据流。macOS 仅对系统的 `/tmp` 和 `/var` 精确别名作有限兼容，不放开任意符号链接。不要将临时目录当作永久证据库。

## 升级与回滚

先保存正在处理的私有案件及其修订号。把旧 `brand-protection` 整个目录移动到扫描路径之外，例如个人目录下的独立备份目录，再按上文安装。不要把备份留在 `.agents/skills`，也不要将新脚本覆盖在旧目录上。

回滚时，先移出新安装目录，再恢复完整旧版本。不要因代码回滚而覆盖更新后的私有案件。本版不迁移案件；跨系统复制时，先核对证据哈希，再修正实际路径并重新核对相关审批。消费者复核和提交前审批的既有版本要求继续生效。

保留 `LICENSE`、`agents`、`assets`、`references` 和 `scripts`。维护用 `zh-tech-writing` 不在独立包内。安装不会自动联网、发送投诉、签名、付款、发布消费者材料或启动监测。
