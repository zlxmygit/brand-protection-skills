# 证据包导出

本工具把逐项批准的文件复制到独立 ZIP。它检查案件、文件哈希、披露审查和批准有效期。工具不联网、不脱敏、不发送材料，也不修改案件。

## 使用方法

先准备本案证据和披露审查文件，再指定私有输入、输出目录。

```bash
python3 scripts/evidence_export.py \
  --case /path/to/private-case/case-record.json \
  --review /path/to/private-case/disclosure-review.json \
  --source-root /path/to/private-evidence \
  --output-root /path/to/private-exports
```

两个目录须已经存在，且位于仓库和 Skill 之外。工具只读取审查文件列出的证据，不递归扫描目录。文件必须在指定证据目录内，不能通过符号链接或父目录路径越界。

## 披露审查格式

以下字段保存在私有目录。示例中的哈希必须替换为实际文件哈希。

```json
{
  "version": "1.0",
  "id": "review-1",
  "case_id": "本案 ID",
  "recipient": "已经核实的接收方",
  "purpose": "本次披露用途",
  "reviewed_by": "实际复核者",
  "authorization_source": "授权来源的私有引用",
  "approved_at": "2026-11-13T03:00:00Z",
  "expires_at": "2026-11-13T05:00:00Z",
  "items": [
    {
      "evidence_id": "copy-1",
      "derived_from_id": "original-1",
      "sha256": "待填写的 64 位小写 SHA-256",
      "disclosure_reviewed": true
    }
  ]
}
```

`evidence_id` 必须引用本案证据。证据记录需含 `original_path`、`sha256` 和 `disclosure_class`。派生副本还需用 `derived_from_id` 引用同案原记录，审查文件中的引用必须一致。工具拒绝重复 ID、重复选择和循环引用。

默认只导出已经审查的派生副本。确需导出原件时，在对应条目中明确写入 `allow_original: true`，并核对实际披露权限。`disclosure_class` 只接受 `public` 或 `restricted`；即使标记为公开，仍要逐项审查。

批准时间必须带时区，且满足已生效、未过期。内部有效期来自实际批准安排，不是法律期限。文件哈希发生变化后，必须重新审查；不能只改台账里的哈希绕过检查。

## 输出与复核

工具新建 `export-` 目录，包含 `evidence.zip` 和 `export-receipt.json`。ZIP 内只有编号后的 `.bin` 文件和 `MANIFEST.json`。清单包含案件 ID、证据 ID、副本关联、大小和哈希，不包含本机路径、接收方或内部授权来源。

接收方、审查引用和安装路径等信息只保留在私有回执。**不要把回执连同 ZIP 一起发送。** ZIP 中的案件及证据 ID 也应纳入披露审查。

单文件最多 10 MiB，合计最多 50 MiB，每次选择 1 至 100 项。工具按确定顺序打包，并回读核对字节。写入失败只清理本次输出，原始证据保持不变。

## 验证范围

`disclosure_reviewed` 是已经完成的人工作业记录，不能用填入布尔值代替真实审查。工具不验证复核者身份或授权真伪，不认证法律结论，也不保证图片和文件已正确脱敏。

导出不会运行网页、打开文档或检查其中的操作指令。目录应由可信本机用户管理。当前实现不防御已控制本机账户的并发恶意进程。Windows 目录权限需另行检查。

退出码 `0` 表示已保存并核对导出包；`1` 表示校验或写入失败。成功导出不授予发送权限。对外提交仍遵守[持续跟进规则](follow-up.md)。

## v1.8.1 校验修正

升级后的材料依赖、品牌及目标关联、首次通知和输入输出检查见[审计修复说明](audit-fixes.md)。旧审批快照需要重新复核，不自动改写私有案件。
