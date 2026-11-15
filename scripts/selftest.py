#!/usr/bin/env python3
"""Offline regression: synthetic PDFs, real probe/convert/check CLIs and mutations.

Requires PyMuPDF and pdfplumber. Exit 0 on all assertions, 1 on failure.
Artifacts and subprocess logs are kept in the printed temporary directory.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pymupdf

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
CJK = "china-ss"


def build_pdf(path: Path):
    doc = pymupdf.open()
    # 第 1 页：正文 + 上标注号 + 脚注分隔线 + 脚注 + 页码
    p = doc.new_page(width=595, height=842)
    p.insert_text((60, 100), "第一章 测试", fontname=CJK, fontsize=18)
    body = "这是一段用来验证脚注处理的正文，句子里带一个上标注号，后面应当被去掉。"
    p.insert_text((60, 140), body, fontname=CJK, fontsize=10)
    filler = ("这一段是填充文字，用来让探测脚本判定本页存在文本层，避免被当成扫描件。"
              "内容本身没有意义，只是需要足够的字符量，并且以句号结尾以便段落合并。"
              "再补一句，确保整段在页面宽度内完整换行，不会被页边裁断。")
    p.insert_textbox((60, 165, 535, 300), filler * 2, fontname=CJK, fontsize=10, lineheight=1.6)
    p.insert_text((60 + 10 * len(body) / 2, 136), "1", fontname="helv", fontsize=6)
    p.insert_text((60, 720), "这一句放在页面底部，用来验证段落不会断在半句上。", fontname=CJK, fontsize=10)
    p.draw_line((60, 760), (240, 760))                       # 脚注分隔线：窄、位于页底
    p.insert_text((60, 775), "1测试脚注作者，《书名》，2024。", fontname=CJK, fontsize=8)
    p.insert_text((285, 810), "1", fontname="helv", fontsize=9)

    # 第 2 页：只有横线的表 + 表题
    p = doc.new_page(width=595, height=842)
    p.insert_text((60, 100), "1.1 表格小节", fontname=CJK, fontsize=14)
    p.insert_text((60, 140), "下面这张表只有横线，没有竖线，默认策略抓不到。", fontname=CJK, fontsize=10)
    filler = ("这一段同样是为了凑够文本层字符量，确保探测结果不是扫描件。"
              "文字会重复出现，因此也会顺带检验重复段落检测是否正常工作。")
    p.insert_textbox((60, 300, 535, 400), filler * 2, fontname=CJK, fontsize=10, lineheight=1.6)
    p.insert_text((60, 180), "表1-1 测试表", fontname=CJK, fontsize=9)
    for i, y in enumerate((200, 230, 260, 290)):
        p.draw_line((60, y), (520, y))
    for row, y in enumerate((218, 248, 278)):
        for col, x in enumerate((60, 210, 360)):
            p.insert_text((x, y), "单元格%d%d" % (row, col), fontname=CJK, fontsize=9)

    # 第 3 页：代码块（含 # 注释）+ 正文
    p = doc.new_page(width=595, height=842)
    p.insert_text((60, 100), "1.2 代码小节", fontname=CJK, fontsize=14)
    p.insert_text((60, 140), "下面的代码里有一行以井号开头的注释，不能被当成标题。", fontname=CJK, fontsize=10)
    filler = ("代码前后的正文都需要足够字符量，才能让本页被判定为文本页而不是扫描件。"
              "这些句子以句号结尾，便于段落合并逻辑正确处理。")
    p.insert_textbox((60, 310, 535, 420), filler * 2, fontname=CJK, fontsize=10, lineheight=1.6)
    for i, line in enumerate(("# setup", "x = 1", "# loop", "for i in range(3):", "    x += i")):
        p.insert_text((60, 180 + i * 14), line, fontname="cour", fontsize=9)
    p.insert_text((60, 280), "代码之后的正文，用于确认段落没有被代码打断。", fontname=CJK, fontsize=10)
    # 第 4 页：英文题注（短）与英文正文句（长、带句号），验证不会被混为一谈
    p = doc.new_page(width=595, height=842)
    p.insert_text((60, 100), "1.3 English Captions", fontname="helv", fontsize=14)
    p.insert_text((60, 140), "Figure 1 Test caption for English", fontname="helv", fontsize=10)
    p.insert_text((60, 170), "Figure 2 represents a generic model, with activities at different levels.",
                  fontname="helv", fontsize=10)
    filler_en = ("This paragraph exists only to keep the page above the text-layer threshold, "
                 "and it ends with a period so that paragraph merging works as expected. ")
    p.insert_textbox((60, 195, 535, 320), filler_en * 3, fontname="helv", fontsize=10, lineheight=1.5)

    doc.set_toc([[1, "第一章 测试", 1], [2, "1.1 表格小节", 2],
                 [2, "1.2 代码小节", 3], [2, "1.3 English Captions", 4]])
    doc.save(path)
    doc.close()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="doc2md-selftest-"))
    print("Artifacts: %s" % tmp)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    run_count = 0

    def run(script, *args):
        nonlocal run_count
        run_count += 1
        cmd = [sys.executable, "-B", str(HERE / script), *map(str, args)]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=env)
        (tmp / ("run-%02d.log" % run_count)).write_text(
            json.dumps(cmd, ensure_ascii=False) + "\nexit=" + str(result.returncode)
            + "\n" + result.stdout + "\n" + result.stderr, encoding="utf-8")
        return result

    def write_json(path, data):
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def inspect(mdpath, *args):
        result = run("check.py", mdpath, "--json", *args)
        if result.returncode not in (0, 1, 3):
            raise RuntimeError(result.stderr)
        return result, {x["id"]: x for x in json.loads(result.stdout)["items"]}

    pdf, base, out = tmp / "fixture.pdf", tmp / "baseline.json", tmp / "out.md"
    build_pdf(pdf)

    for script, args in (("probe.py", (pdf, "-o", base)),
                         ("convert_pdf.py", (pdf, base, out))):
        r = run(script, *args)
        if r.returncode != 0:
            print("FAIL pipeline: %s\n%s" % (script, r.stderr[:400]))
            return 1

    md = out.read_text(encoding="utf-8")
    checks = [
        ("脚注正文已删除", "测试脚注作者" not in md),
        ("上标注号已删除", "注号1" not in md and "带一个上标注号，后面" in md),
        ("表题变成锚点", "> 表1-1 测试表" in md),
        ("表体没有串行输出", "单元格00" not in md),
        ("代码在围栏内", "```" in md and "# setup" in md),
        ("代码注释未成标题", "```\n# setup" in md),
        ("代码缩进保留", "\n    x += i\n```" in md),
        ("正文没被代码切断", "代码之后的正文" in md),
        ("标题层级正确", "\n## 1.1 表格小节" in md and "\n## 1.2 代码小节" in md),
        ("英文短题注变锚点", "> Figure 1 Test caption for English" in md),
        ("英文正文句未被当题注", "Figure 2 represents a generic model" in md
                              and "> Figure 2 represents" not in md),
    ]
    sys.path.insert(0, str(HERE))
    import check as _C
    heads = [l for l in _C.strip_fences(md).split("\n") if l.startswith("#")]
    para_bad = 0
    for blk in [b.strip() for b in md.split("\n\n") if len(b.strip()) > 150]:
        if blk.startswith(("#", "-", ">", "`")) or "```" in blk:
            continue
        if not blk.rstrip().endswith(("。", "！", "？", "：", "；", ".")):
            para_bad += 1
    checks += [
        ("关键正文完整保留", all(s in md for s in ("页面底部", "下面这张表", "代码之后的正文", "This paragraph exists"))),
        ("数值：标题恰好 4 个", len(heads) == 4),
        ("数值：无断句段落", para_bad == 0),
    ]
    # Exercise the checker CLI, including a real missing English caption.
    result, items = inspect(out)
    checks.append(("真实 check CLI 检出悬空英文引用", result.returncode == 1 and items["3a"]["status"] == "BLOCKER"))
    clean = tmp / "clean.md"
    clean.write_text(md + "\n> Figure 2 Generic model\n", encoding="utf-8")
    result, items = inspect(clean)
    checks += [
        ("补齐真实锚点后 check CLI 通过", result.returncode == 0 and items["3a"]["status"] == "PASS"),
        ("小样本长度分布未冒充通过", all(items[k]["status"] == "UNVERIFIED" for k in ("1c", "7a", "7b"))),
    ]

    # Controlled layout isolates known failure modes from probe sampling noise.
    stress_pdf, stress_base, stress_out = tmp / "stress.pdf", tmp / "stress.json", tmp / "stress.md"
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((60, 100), "Chapter 1 Content", fontsize=18)
    p.insert_text((60, 140), "这是合法的冒号段落：", fontname=CJK, fontsize=10)
    p.insert_text((60, 180), "Alpha; beta; gamma; delta.", fontsize=10)
    p.insert_text((60, 220), "- FIRST_LIST_ITEM", fontsize=10)
    p.insert_text((60, 240), "- SECOND_LIST_ITEM", fontsize=10)
    p.insert_text((80, 255), "CONTINUATION.", fontsize=10)
    p.insert_text((60, 285), "AFTER_LIST_PROSE.", fontsize=10)
    p.insert_text((60, 700), "CROSS_PAGE_START", fontsize=10)
    p = doc.new_page()
    p.insert_text((60, 100), "CROSS_PAGE_END.", fontsize=10)
    p.insert_text((60, 600), "References", fontsize=8)
    p.insert_text((60, 640), "[1] SMALL_BIB_ENTRY https://example.org/a", fontsize=8)
    p = doc.new_page()
    p.insert_text((60, 100), "[2] NEXT_PAGE_BIB_ENTRY", fontsize=8)
    p.insert_text((60, 300), "Chapter 2 Content", fontsize=18)
    p.insert_text((60, 340), "BODY_AFTER_BIBLIOGRAPHY.", fontsize=10)
    p.insert_text((60, 750), "3 REAL_FOOTNOTE_AFTER_BIB", fontsize=8)
    p = doc.new_page()
    p.insert_text((60, 100), "EOF_WITHOUT_PUNCTUATION", fontsize=10)
    doc.set_toc([[1, "Chapter 1 Content", 1], [2, "References", 2], [1, "Chapter 2 Content", 3]])
    doc.save(stress_pdf)
    doc.close()
    controlled = json.loads(base.read_text(encoding="utf-8"))
    controlled.pop("artifact", None)
    controlled["profile"] = {}
    controlled["params"].update(body_size=10, footnote_size=8, superscript_size=None,
                                chrome_bands={"header": None, "footer": None},
                                paragraph_gap=24, leading_median=16,
                                columns={"n": 1}, heading_level_source="bookmark_level")
    write_json(stress_base, controlled)
    result = run("convert_pdf.py", stress_pdf, stress_base, stress_out)
    checks.append(("压力样本转换成功", result.returncode == 0))
    stress = stress_out.read_text(encoding="utf-8") if stress_out.exists() else ""
    checks += [
        ("文末无标点仍保留", "EOF_WITHOUT_PUNCTUATION" in stress),
        ("跨页未收句缓冲完整续接", "CROSS_PAGE_START CROSS_PAGE_END." in stress),
        ("中文冒号段落保留", "这是合法的冒号段落：" in stress),
        ("英文分号段落保留", "Alpha; beta; gamma; delta." in stress),
        ("列表分项保留缩进续行", "- FIRST_LIST_ITEM\n\n- SECOND_LIST_ITEM CONTINUATION." in stress),
        ("列表退出后的正文独立", "\n\nAFTER_LIST_PROSE." in stress),
        ("小字号书目标题及 URL 条目保留", "## References" in stress and "SMALL_BIB_ENTRY https://example.org/a" in stress),
        ("跨页书目保留", "NEXT_PAGE_BIB_ENTRY" in stress),
        ("书目状态在下一章退出", "REAL_FOOTNOTE_AFTER_BIB" not in stress and "BODY_AFTER_BIBLIOGRAPHY." in stress),
    ]
    _, bib_items = inspect(stress_out)
    checks.append(("书目编号未误报脚注", bib_items["4a"]["status"] == "PASS"))
    result = run("convert_pdf.py", stress_pdf, stress_base, tmp / "excerpt.md", "--pages", "1")
    excerpt = (tmp / "excerpt.md").read_text(encoding="utf-8")
    checks.append(("节选未收句仍保留且声明截断", result.returncode == 0 and "CROSS_PAGE_START" in excerpt and "Page excerpt" in excerpt))
    for key in ("footnote_size", "superscript_size", "paragraph_gap", "leading_median"):
        controlled["params"][key] = None
    write_json(tmp / "null.json", controlled)
    result = run("convert_pdf.py", stress_pdf, tmp / "null.json", tmp / "null.md")
    null_md = (tmp / "null.md").read_text(encoding="utf-8")
    checks.append(("null 不猜字号且不崩溃", result.returncode == 0 and "REAL_FOOTNOTE_AFTER_BIB" in null_md and "footnote_size is null" in null_md))
    result = run("convert_pdf.py", stress_pdf, stress_base, stress_pdf)
    checks.append(("转换输出不能覆盖源PDF", result.returncode == 2 and stress_pdf.read_bytes().startswith(b"%PDF")))
    (tmp / "bad.json").write_text("{}", encoding="utf-8")
    result = run("convert_pdf.py", stress_pdf, tmp / "bad.json", tmp / "bad-out.md")
    checks.append(("缺失参数返回输入错误而非崩溃", result.returncode == 2 and not (tmp / "bad-out.md").exists()))
    example = SKILL / "assets" / "dama-en.example.json"
    result = run("convert_pdf.py", stress_pdf, example, tmp / "example-out.md")
    checks.append(("禁止执行书籍样例baseline", result.returncode == 2 and not (tmp / "example-out.md").exists()))
    stale_pdf = tmp / "stale-bookmarks.pdf"
    with pymupdf.open(stress_pdf) as stale:
        stale.set_toc([[1, "OUTDATED_BOOKMARK_NOT_ON_PAGE", 1]])
        stale.save(stale_pdf)
    result = run("convert_pdf.py", stale_pdf, stress_base, tmp / "stale-bookmarks.md")
    stale_md = (tmp / "stale-bookmarks.md").read_text(encoding="utf-8")
    checks.append(("失配书签自动声明而非静默", result.returncode == 0 and "Unmatched bookmarks" in stale_md))
    checks.append(("未知页眉自动声明", "header band is null" in stale_md))

    controlled["params"]["columns"]["n"] = 2
    write_json(tmp / "columns.json", controlled)
    result = run("convert_pdf.py", stress_pdf, tmp / "columns.json", tmp / "rejected.md")
    checks.append(("已知多栏明确拒绝不写文件", result.returncode == 2 and not (tmp / "rejected.md").exists()))
    col_pdf = tmp / "columns.pdf"
    doc = pymupdf.open()
    p = doc.new_page()
    for i in range(12):
        p.insert_text((60, 110 + i * 18), "LEFT_%02d column sentence." % i, fontsize=10)
        p.insert_text((320, 110 + i * 18), "RIGHT_%02d column sentence." % i, fontsize=10)
    doc.save(col_pdf)
    doc.close()
    result = run("probe.py", col_pdf, "-o", tmp / "detected-columns.json")
    col_base = json.loads((tmp / "detected-columns.json").read_text(encoding="utf-8"))
    checks.append(("单页多栏 probe 实际检出", result.returncode == 0 and col_base["params"]["columns"]["n"] == 2))
    col_base["params"]["columns"]["n"] = 1
    write_json(tmp / "stale-columns.json", col_base)
    result = run("convert_pdf.py", col_pdf, tmp / "stale-columns.json", tmp / "missed-columns.md")
    checks.append(("漏采多栏逐页守卫拒绝", result.returncode == 2 and not (tmp / "missed-columns.md").exists()))
    from probe import has_columns
    label_rows = []
    for i in range(8):
        for x in (60, 320):
            label_rows.append({"s": 10, "x0": x, "x1": x + 45, "y0": 100 + i * 18})
    checks.append(("短表格单元格不是正文双栏", not has_columns(label_rows, 595, 10, 0.25)))

    # Checker mutations must alter the intended assertion without unrelated blockers.
    anchor_md = tmp / "anchors.md"
    anchor_text = ("# Chapter 1 Content\n\n"
                   "See Figure 7.2 and Table 7.2.\n\n"
                   "> Figure 7.2 Diagram\n\n> Table 7.2 Data\n\n"
                   "- First item.\n\n- Second item.\n")
    anchor_md.write_text(anchor_text, encoding="utf-8")
    _, anchor_items = inspect(anchor_md)
    checks.append(("英文复合编号及同号图表都能匹配", anchor_items["3a"]["status"] == "PASS"))
    mutations = [
        ("图锚点改名不被同号表抵消", anchor_text.replace("> Figure 7.2", "> Figure 7.3"), "3a"),
        ("两个列表项并一行被检出", anchor_text.replace("- First item.\n\n- Second", "- First item. - Second"), "6a"),
        ("有句号的英文正文不能自证题注", anchor_text.replace("> Figure 7.2 Diagram", "Figure 7.2 represents a model."), "3a"),
        ("长英文末段缺句号被检出", anchor_text + "\n" + "Long meaningful English prose " * 8, "5a"),
        ("长中文冒号前缺标点被检出", anchor_text + "\n" + "这是一段需要检查完整性的中文正文" * 14, "5a"),
    ]
    for idx, (name, content, sid) in enumerate(mutations):
        target = tmp / ("mutation-%d.md" % idx)
        target.write_text(content, encoding="utf-8")
        _, mutation_items = inspect(target)
        checks.append((name, mutation_items[sid]["status"] in ("BLOCKER", "MAJOR")
                       and all(v["status"] != "BLOCKER" or k == sid for k, v in mutation_items.items())))
    zh = tmp / "zh-anchors.md"
    zh.write_text("# 内容\n\n见图7-2和表7-2。\n\n> 表7-2 表题\n", encoding="utf-8")
    _, zh_items = inspect(zh)
    checks.append(("中文图锚点不能由同号表抵消", zh_items["3a"]["status"] in ("BLOCKER", "MAJOR")))
    chapter_dir = tmp / "chapters"
    chapter_dir.mkdir()
    (chapter_dir / "01.md").write_text("", encoding="utf-8")
    result, chapter_items = inspect(anchor_md, "--chapters-dir", chapter_dir)
    checks.append(("空章节不再通过守恒", result.returncode == 1 and chapter_items["1d"]["status"] == "BLOCKER"))
    (chapter_dir / "01.md").write_text("# Empty chapter\n", encoding="utf-8")
    _, chapter_items = inspect(anchor_md, "--chapters-dir", chapter_dir)
    checks.append(("只有标题的章节不再通过守恒", chapter_items["1d"]["status"] == "BLOCKER"))
    (chapter_dir / "01.md").write_text(anchor_text, encoding="utf-8")
    _, chapter_items = inspect(anchor_md, "--chapters-dir", chapter_dir)
    checks.append(("非空同源章节仍通过守恒", chapter_items["1d"]["status"] == "PASS"))
    result = run("check.py", anchor_md, "--chapters-dir", tmp / "missing-chapters")
    checks.append(("缺失章节目录退出2", result.returncode == 2))
    result = run("check.py", anchor_md, "--out", anchor_md)
    checks.append(("审计报告不能覆盖原Markdown", result.returncode == 2 and anchor_md.read_text(encoding="utf-8") == anchor_text))
    empty_leaf = tmp / "empty-leaf.md"
    empty_leaf.write_text("# Chapter 1\n\n## Empty\n\n## Content\n\nBody.\n", encoding="utf-8")
    result, empty_items = inspect(empty_leaf)
    checks.append(("小样本也检测空叶节", result.returncode == 1 and empty_items["7c"]["status"] == "BLOCKER"))
    empty_list = tmp / "empty-list.md"
    empty_list.write_text("# Content\n\nBody.\n\n-\n", encoding="utf-8")
    _, empty_list_items = inspect(empty_list)
    checks.append(("裸列表符号不是正常列表", empty_list_items["6b"]["status"] == "MINOR"))

    # A measured header must not dominate the body font histogram.
    from probe import probe_pdf, load_thresholds
    header_pdf = tmp / "header.pdf"
    doc = pymupdf.open()
    for i in range(3):
        p = doc.new_page()
        p.insert_textbox((40, 15, 550, 65), "REPEATED_HEADER " * 8, fontsize=9)
        p.insert_textbox((60, 130, 530, 300), "Unique body text %s. " % chr(65 + i) * 12, fontsize=12)
    doc.save(header_pdf)
    doc.close()
    header_base = probe_pdf(header_pdf, load_thresholds())
    checks.append(("页眉清洗先于正文字号统计", header_base.get("params", {}).get("body_size") == 12))

    # Every advertised configurable key is actually referenced, with no stale aliases.
    import ast
    thresholds = json.loads((SKILL / "assets" / "thresholds.json").read_text(encoding="utf-8"))
    used = set()
    for script in ("probe.py", "check.py", "convert_pdf.py"):
        tree = ast.parse((HERE / script).read_text(encoding="utf-8"))
        used.update(n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str))
    checks.append(("全部配置 key 都有代码读取", all(k in used for k in thresholds if not k.startswith("_"))))
    (tmp / "assertions.json").write_text(json.dumps(
        [{"name": name, "pass": bool(ok)} for name, ok in checks], ensure_ascii=False, indent=2), encoding="utf-8")
    bad = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(("  PASS  " if ok else "  FAIL  ") + name)
    if bad:
        print("\n输出片段：\n" + md[:600])
        return 1
    print("\n%d/%d 通过" % (len(checks), len(checks)))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
