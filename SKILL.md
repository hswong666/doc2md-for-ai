---
name: doc2md-for-ai
description: "Converts text PDFs into Markdown for AI knowledge bases using measured layout parameters, and checks converted Markdown for structural defects. Helps choose an external conversion route for EPUB, DOCX and other formats. Use when preparing documents for RAG or repairing Markdown with page headers, footnotes, broken paragraphs or merged lists."
metadata:
  agent_created: true
---

# doc2md-for-ai

把文档转换成供知识库 / RAG 使用的 Markdown，并如实报告裁剪与未验证项。

自带转换器仅支持**有文本层的单栏 PDF，知识库裁剪模式**。它不是无损归档器；多栏会拒绝，扫描件需先 OCR。需要保留表格数据、脚注或原版式时，选择其他实现，不运行本转换器冒充支持。

## 环境

使用已装好依赖的 Python；命令中的 `python` 代表该解释器，不绑定某台机器的绝对路径。

| 依赖 | 用途 |
|---|---|
| PyMuPDF (`pymupdf`) | PDF 探测与转换必需 |
| pdfplumber | 有横线证据的表格区域识别；缺失时保留表格文字并声明限制 |
| Python 标准库 | EPUB 粗粒度探测、Markdown 检查 |
| markitdown（外部可选） | EPUB / DOCX / PPTX / HTML 的另一条转换路线 |

完整回归自检需要 PyMuPDF 和 pdfplumber。脚本不下载模型、不访问网络。

## 工作流程

### 1. 探测与选路

```text
python scripts/probe.py book.pdf -o baseline.json
```

读取 `verdict`、`warnings` 和 `params`，不要只看退出码。

- `needs_ocr`：先做 OCR。
- `columns.n` 不为单栏：使用支持分栏阅读顺序的解析器；本转换器拒绝执行。
- 单栏文本 PDF：试转节选，对照源页再决定是否转全文。
- EPUB：可用 probe 看结构；本包没有 EPUB 转换器。DOCX / PPTX / HTML 也需外部转换工具。

换书或源文件变化必须重新 probe。`assets/dama-en.example.json` 仅展示结构，不能作为可执行参数包。

### 2. 理解测量值

书籍参数在 baseline，probe/check 的配置阈值在 `assets/thresholds.json`，语法与几何算法容差在脚本。不要把算法常量、书籍测量值和验收阈值混为一谈。

`null` 表示未取得可靠测量，不等于“没有这个特征”。转换器跳过相应字号/段距规则，在 `known_limits` 声明；不得套上一本书的值。独立几何证据仍可识别脚注分隔线。

`chrome_bands` 是页高比例；字号、行距、段距是点值。`body_tolerance`、`bookmark_anchor`、`page_number` 和 `footnote_separator_y_ratio` 是探测参考信息，当前转换器不把它们当作独立执行开关。

### 3. 转换

```text
python scripts/convert_pdf.py book.pdf baseline.json excerpt.md --pages 1-20
python scripts/convert_pdf.py book.pdf baseline.json book.md --known-limits "人工确认的其他限制"
```

转换行为：

- 保留首个书签之前的文字；不凭书签起始页删除前言。
- 先标记书签标题、题注和书目，再清页带和统计脚注位置。书目保护跨页，在后续同级或更高级标题退出。
- 有书签时按文本锚定标题；没有书签时，仅以编号与测得字号组合回退。书签未命中的标题需人工复核。
- 每页识别满足位置、宽度、行数、字数和字号校验的脚注分隔线；未命中时只使用非 null 的脚注字号兜底。仅本页识别到脚注时才去上标注号。
- 表格只按有横线证据的 bbox 删文字；不会从表题开始盲删后续若干行。位图区域文字删除，矢量框、无框线表及未识别表格文字保留供复核。
- 中英图/表题共用识别规则与检查器；输出块引用。列表遇新标记断项，缩进续行合并，退回起项缩进时结束列表。
- 等宽字体或代码前缀行保留为围栏代码；中文断行之间不额外插空格，英文以空格连接。下沉首字母和连字符不自动修复。
- 未收句段落可跨页续接；**到文件或节选结尾必须写出缓冲内容**，不能为了断句率删除它。

输出 front matter 含 `removed` 和 `known_limits`，自动声明未知页带、未命中书签及节选边界。它们记录裁剪策略及限制，不证明每一处识别正确。发现正文丢失时回到源文重转，不能靠自检数字判断保真。

### 4. 检查与交付

```text
python scripts/check.py book.md --json
python scripts/check.py book.md --chapters-dir chapters --out check-report.md
```

`--baseline baseline.json` 可做**全文规模粗筛**，不会提高可判覆盖率，不适合直接用全书 baseline 检查节选。已明确裁剪的大量表格/脚注也可能触发规模差异，需解释原因。

退出码：

| CLI | 含义 |
|---|---|
| check | 0 无 blocker 且覆盖率达标；1 有 blocker；2 输入错误；3 证据不足 |
| convert_pdf | 0 写出产物；2 输入错误或不支持的版式；5 缺依赖 |
| probe | 0 写出结果（仍须读 warnings）；2 输入错；3 加密；4 需 OCR；5 缺依赖 |
| selftest | 0 全部断言通过；1 流程或断言失败 |

检查器按 11 个标准分组，8 组自动检测、3 组需抽样。某组有可判子项不代表整组已被完整验证。标准、配置 key 和盲区见 [references/standards.md](references/standards.md)，只在解释报告或修改判据时读取。

交付时报告退出码、blocker/major、未验证项及裁剪内容。`known_limits` **不会豁免 blocker 或把退出码变成通过**；用户接受带缺陷结果时，仍应明确标为未通过自动验收。不靠放宽阈值或删除正文让结果变绿。

## 修改后验证

```text
python scripts/selftest.py
```

自检现场生成多组合成 PDF，实际跑 probe → convert → check，并注入空章节、合并列表、改名锚点、缺句末标点等缺陷。断言数以运行输出为准；日志、JSON 断言结果和合成产物保存在打印出的临时目录。

自检覆盖确定性回归，不证明任意书籍保真。换书、换语言、遇到不同版式或要修改转换规则时，读取 [references/format-playbook.md](references/format-playbook.md)，再对照真实源页抽样。
