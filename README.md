# doc2md-for-ai

将有文本层的单栏 PDF 转换为供 AI 知识库使用的 Markdown，并检查转换后的结构问题。

这个技能来自 WorkBuddy 文档转换实践，经过 Codex 修复与回归验证。它将转换说明、
参数探测、参考转换器和质量检查器放在同一个目录中，供支持本地 skill 的 AI 工具使用。

## 适用范围

- 有文本层的单栏 PDF；允许裁剪图片、表格正文和脚注的知识库场景。
- 检查 Markdown 的标题、图表引用、段落、列表和分章结构。
- EPUB 仅提供粗粒度探测；其他格式需要外部转换工具。

多栏 PDF 会被拒绝，扫描件需要先 OCR。需要表格、脚注或原版式完整保真时，
应选择其他转换实现。自动检查通过不等于内容保真，仍需对照源文抽样。

## 使用

将整个目录放入所用 AI 工具的 skills 目录，保留 `SKILL.md`、`scripts/`、
`references/` 和 `assets/` 的相对关系。具体加载方式以所用工具为准。

WorkBuddy 的 `skill_manage` 另有平台管理标记：若需由它修改或删除此技能，
在**本机安装副本**的 YAML 顶层补充 `agent_created: true`，保留原有 `metadata`。
通用发布版只在 `metadata` 下记录此标记；Codex 的校验器会拒绝额外的顶层字段。
这个差异影响平台管理权限，不改变转换脚本。

也可以直接运行脚本：

```text
python -m pip install -r requirements.txt
python scripts/probe.py book.pdf -o baseline.json
python scripts/convert_pdf.py book.pdf baseline.json excerpt.md --pages 1-20
python scripts/check.py excerpt.md --json
```

先阅读 probe 的 warnings 和产物的 known_limits，并对照源页。确认路线适用后再转全文：

```text
python scripts/convert_pdf.py book.pdf baseline.json book.md
python scripts/check.py book.md --out check-report.md
```

参数随书测量，不能把示例 baseline 或另一份书的参数直接拿来使用。
全文规模检查和分章检查的用法见 [SKILL.md](SKILL.md)。

## 验证

```text
python scripts/selftest.py
```

2026-11-15 在 Windows、Python 3.13、PyMuPDF 1.28.2、pdfplumber 0.11.10
环境运行，55 项回归断言全部通过。其他系统尚未完成独立验证。
测试现场生成合成 PDF，实际调用探测、转换和检查命令，并注入已知缺陷。

| check 退出码 | 含义 |
|---|---|
| 0 | 未发现 blocker，自动可判覆盖率达标 |
| 1 | 发现 blocker |
| 2 | 输入或配置错误 |
| 3 | 证据不足 |

检查器的覆盖率衡量检测项有无证据，不是内容保真率。完整判据和盲区见
[验收标准](references/standards.md) 与 [换书手册](references/format-playbook.md)。

## 已知限制

未匹配书签可能导致标题未恢复；变化页眉、矢量图标签和无框线表可能保留。
列表标记已丢失的合并无法只凭 Markdown 可靠判断。
脚本不会自动修复首字母下沉或英文断词，数学和复杂代码也需额外复核。

仓库不包含实际书籍 PDF、转换后的整书、个人配置、访问令牌或本地验收截图。

## 依赖许可

PyMuPDF / MuPDF 提供 AGPL 与 Artifex 商业许可两种选择，使用时须符合所选许可的要求。
依赖库保留各自的许可要求，本项目的源码许可证不构成对依赖的重新授权。

## 源码许可证

本项目采用 [GNU Affero General Public License v3.0](LICENSE)（AGPL-3.0）。
