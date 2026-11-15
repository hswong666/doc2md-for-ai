#!/usr/bin/env python3
"""convert_pdf.py — 按 baseline.json 的参数把 PDF 转成 AI 可读的 Markdown。

这是单栏 PDF 的知识库裁剪参考实现；测量数据来自 baseline，算法容差在代码中。
改它之前先跑 probe；改完用 check.py 验证。

四条顺序不可颠倒：
1. 先清洗 chrome（页眉/页脚/页码），再统计
2. 正文基线逐页算，且排除 chrome 带
3. 脚注看内容+位置，注号用去标记前的原始文本判断
4. 标题、题注和书目在破坏性筛除前保护

用法：python scripts/convert_pdf.py book.pdf baseline.json out.md [--pages 1-20]
"""
import json
import re
import sys
from pathlib import Path

try:
    import pymupdf
except ImportError as exc:
    print("Missing dependency: %s" % exc, file=sys.stderr)
    sys.exit(5)
from captions import BIB_RE, match_caption
from probe import has_columns, load_thresholds

SUP_RE = re.compile(r"^\d{1,2}$")
PAGE_NO_RE = re.compile(r"^\d{1,3}$")
NUM_HEAD_RE = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+\S")


MONO_FONT = re.compile(r"(?i)mono|courier|consolas|menlo|code|terminal")
URL_TOKEN = r"(?:https?://[^\s、,，]+|(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?:/[^\s、,，]*)?)"


LIST_RE = re.compile(r"^(?:[-*+•▪●◦]|\d+[.)）]|[（(]\d+[)）]|[①-⑳])(?:\s+|(?=[\u4e00-\u9fff]))")
TERMINATORS = tuple("。！？：；.!?:;") + (")", "）", '"', "”")


def is_url_line(t: str) -> bool:
    """整行只由 URL/域名组成才算。注意不能用 \\w——它在 Python 里匹配中文。"""
    t = (t or "").strip()
    if not t or len(t) > 300:
        return False
    return bool(re.fullmatch(URL_TOKEN + r"(?:\s*[、,，]\s*" + URL_TOKEN + r")*", t))


def norm(t: str) -> str:
    return re.sub(r"\s+", "", t or "").lower()


def load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def page_lines(page, sup_size, strip_marks=True):
    rows = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            code_text = "".join(s["text"] for s in line["spans"]).rstrip()
            raw = code_text.strip()
            parts = []
            for s in line["spans"]:
                # 注号是上标：从正文里去掉，但 raw 保留给脚注判定用
                if strip_marks and sup_size is not None and s["size"] <= sup_size + 1.0 and SUP_RE.match(s["text"].strip() or "-"):
                    continue
                parts.append(s["text"])
            text = "".join(parts).strip()
            if not raw:
                continue
            if not text:
                text = raw
            mono_len = sum(len(sp["text"]) for sp in line["spans"] if MONO_FONT.search(sp["font"]))
            rows.append({
                "mono": mono_len > 0.5 * max(1, len(raw)),
                "code_text": code_text,
                "t": text, "t_raw": "".join(sp["text"] for sp in line["spans"]).strip(), "raw": raw, "s": round(max(x["size"] for x in line["spans"]), 2),
                "x0": line["bbox"][0], "y0": line["bbox"][1],
                "x1": line["bbox"][2], "y1": line["bbox"][3],
            })
    rows.sort(key=lambda r: (round(r["y0"], 1), r["x0"]))
    return rows


TABLE_SETTINGS = (
    None,                                                        # 默认：全框线
    {"vertical_strategy": "text", "horizontal_strategy": "lines"},
)


def table_boxes(doc, pages):
    """只使用有横线证据的 bbox，禁止 text/text 把正文猜成表格。"""
    try:
        import pdfplumber
    except ImportError:
        return {}, ["pdfplumber unavailable; table text retained"]
    out = {}
    warnings = []
    with pdfplumber.open(doc.name) as pdf:
        for p in pages:
            boxes = []
            page = pdf.pages[p - 1]
            strategies = [(page, st) for st in TABLE_SETTINGS]
            # Text-derived vertical edges stop at the first/last word, so they miss
            # the outer horizontal rules. Extend them only inside ruled strips.
            groups = {}
            for edge in page.edges:
                if edge["orientation"] == "h" and edge["x1"] - edge["x0"] > 60:
                    key = (round(edge["x0"] / 3), round(edge["x1"] / 3))
                    groups.setdefault(key, []).append(edge)
            for edges in groups.values():
                if len(edges) < 3:
                    continue
                bounds = (min(e["x0"] for e in edges), min(e["top"] for e in edges),
                          max(e["x1"] for e in edges), max(e["bottom"] for e in edges))
                if bounds[3] - bounds[1] > page.height * 0.75:
                    continue
                try:
                    crop = page.crop(bounds)
                    finder = crop.debug_tablefinder(TABLE_SETTINGS[1])
                except Exception:
                    warnings.append("Ruled-strip detection failed on page %d; review retained text" % p)
                    continue
                xs = sorted({e["x0"] for e in finder.edges if e["orientation"] == "v"})
                if len(xs) >= 3:
                    strategies.append((crop, {"vertical_strategy": "explicit",
                                              "explicit_vertical_lines": xs,
                                              "horizontal_strategy": "lines"}))
            for region, st in strategies:
                try:
                    found = region.find_tables() if st is None else region.find_tables(table_settings=st)
                except Exception:
                    warnings.append("Table detection failed on page %d; review retained text" % p)
                    continue
                for t in found:
                    bb = list(t.bbox)
                    if (bb[2] - bb[0]) < 60 or (bb[3] - bb[1]) < 30:   # 排除细条
                        continue
                    # 校验：必须是"多行多列且单元格多数非空"，否则 text/text 会把整页正文当表
                    try:
                        data = t.extract()
                    except Exception:
                        continue
                    if not data:
                        continue
                    ncols = max(len(r) for r in data)
                    if len(data) < 2 or ncols < 2:
                        continue
                    filled = sum(1 for r in data for c in r if c and str(c).strip())
                    if filled < 0.4 * len(data) * ncols:
                        continue
                    if (bb[3] - bb[1]) > 0.75 * page.height:            # 覆盖大半页，判定为误识别
                        continue
                    if not any(abs(bb[0]-b[0]) < 6 and abs(bb[1]-b[1]) < 6 for b in boxes):
                        boxes.append(bb)
            out[p] = boxes
    return out, warnings


def page_regions(page, rows):
    """只删除位图中的文字；矢量框不能可靠区分提示框和示意图，保留。"""
    boxes = []
    try:
        for info in page.get_image_info():
            b = info.get("bbox")
            if b and (b[2] - b[0]) > 30 and (b[3] - b[1]) > 30:
                boxes.append(tuple(b))
    except Exception:
        pass
    return boxes


def inside(row, boxes, tol=2):
    cx = (row["x0"] + row["x1"]) / 2
    cy = (row["y0"] + row["y1"]) / 2
    return any(b[0] - tol <= cx <= b[2] + tol and b[1] - tol <= cy <= b[3] + tol for b in boxes)


def min_number_depth(titles):
    depths = []
    for t in titles:
        m = NUM_HEAD_RE.match(t or "")
        if m:
            depths.append(m.group(1).count(".") + 1)
    return min(depths) if depths else 1


def heading_level(title, source, d_min=1, bm_level=2):
    m = NUM_HEAD_RE.match(title)
    if source == "numbering" and m:
        depth = m.group(1).count(".") + 1
        return min(depth - d_min + 2, 6)      # 最小深度落 ##，其余逐级下推
    return min(bm_level, 6)                   # 无编号：沿用书签层级（章级 = #）


def main():
    import argparse
    ap = argparse.ArgumentParser(description="按 baseline 参数把 PDF 转成 Markdown")
    ap.add_argument("pdf"); ap.add_argument("baseline"); ap.add_argument("out")
    ap.add_argument("--pages", default="1-", help="如 1-20；省略表示全书")
    ap.add_argument("--known-limits", default="",
                    help="已知缺陷，用分号分隔；会逐条写进 front matter 的 known_limits")
    a = ap.parse_args()
    pdf_path, base_path, out_path = a.pdf, a.baseline, a.out
    try:
        if "-" in a.pages:
            x, _, y = a.pages.partition("-")
            start = int(x or 1); end = int(y) if y else 10 ** 6
        else:
            start = end = int(a.pages)
        base = load(base_path)
        if base.get("_example_only"):
            raise ValueError("example baseline is not executable; run probe on the actual source")
        if base.get("kind") != "pdf" or base.get("verdict") not in ("ok", "low_confidence"):
            raise ValueError("baseline must describe a readable PDF")
        p = base["params"]
        split_ratio = float(load_thresholds()["probe.column_split_ratio"])
        body_size = p["body_size"]
        if not isinstance(body_size, (int, float)) or body_size <= 0:
            raise ValueError("body_size must be a positive measurement")
        if p.get("text_layer") != "text" or p.get("columns", {}).get("n") != 1:
            raise ValueError("only single-column text PDFs are supported; re-probe or use another converter")
        if Path(out_path).resolve() in (Path(pdf_path).resolve(), Path(base_path).resolve()):
            raise ValueError("output must differ from the source PDF and baseline")
        for key in ("superscript_size", "footnote_size", "paragraph_gap", "leading_median"):
            value = p.get(key)
            if value is not None and (not isinstance(value, (int, float)) or value <= 0):
                raise ValueError("%s must be positive or null" % key)
        head_sizes = p["heading_sizes"] or []
        if not isinstance(head_sizes, list) or any(not isinstance(s, (int, float)) or s <= 0 for s in head_sizes):
            raise ValueError("heading_sizes must be a list of positive measurements or null")
        bands = p["chrome_bands"]
        for key in ("header", "footer"):
            band = bands[key]
            if band is not None and (not isinstance(band, list) or len(band) != 2
                                     or not 0 <= band[0] <= band[1] <= 1):
                raise ValueError("invalid chrome band: %s" % key)
        hdr_end = (bands["header"] or [0, 0])[1]
        ftr_start = (bands["footer"] or [1, 1])[0]
        if hdr_end >= ftr_start:
            raise ValueError("header/footer bands overlap")
        level_source = p["heading_level_source"]
        doc = pymupdf.open(pdf_path)
        if doc.needs_pass or not (1 <= start <= min(end, doc.page_count)):
            raise ValueError("encrypted PDF or invalid page range")
        expected_bytes = base.get("artifact", {}).get("bytes")
        expected_pages = base.get("profile", {}).get("page", {}).get("count")
        if ((expected_bytes and expected_bytes != Path(pdf_path).stat().st_size)
                or (expected_pages and expected_pages != doc.page_count)):
            raise ValueError("baseline source size/page count differs; run probe again")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print("Input error: %s" % exc, file=sys.stderr)
        return 2
    sup_size = p.get("superscript_size")
    para_gap = p.get("paragraph_gap")

    pages = list(range(int(start), min(int(end), doc.page_count) + 1))
    # Probe samples pages; a table may occur outside its sample. Check selected pages.
    tboxes, limits = table_boxes(doc, pages)
    limits += [
        "Vector diagram labels and unruled/undetected tables may remain; review against the PDF",
        "Caption and list boundaries use layout heuristics; content fidelity requires source sampling",
    ]
    for key in ("footnote_size", "superscript_size", "paragraph_gap"):
        if p.get(key) is None:
            limits.append("%s is null; corresponding size/gap rule skipped" % key)
    for key in ("header", "footer"):
        if bands[key] is None:
            limits.append("%s band is null; running headers/footers may remain" % key)
    limits.extend(x.strip() for x in re.split(r"[;；]", a.known_limits) if x.strip())
    if start != 1 or pages[-1] != doc.page_count:
        limits.append("Page excerpt; first/last paragraph may be partial")

    # 书签 → 页内标题锚点；仅无书签时使用字号与编号联合回退。
    toc = doc.get_toc(simple=False)
    d_min = min_number_depth([t for _, t, _, _ in toc])
    anchors = {}
    for lvl, title, pno, dest in toc:
        if pno in pages and title:
            anchors.setdefault(pno, []).append((norm(title)[:12], title, lvl))

    out, buf, code = [], [], []
    prev_level = 0
    bibliography = False
    bibliography_level = None
    list_x = None
    matched_bookmarks = set()

    def flush_code():
        if code:
            out.append("```\n" + "\n".join(code) + "\n```")
            code.clear()

    def flush():
        if not buf:
            return
        text = ""
        for part in buf:
            # Chinese line wraps need no inserted space; Latin word boundaries do.
            joiner = "" if text and re.search(r"[\u4e00-\u9fff]$", text) and re.match(r"[\u4e00-\u9fff]", part) else " "
            text = (text + joiner + part).strip()
        buf.clear()
        out.append(text)

    for pno in pages:
        page = doc[pno - 1]
        h = page.rect.height
        rows = page_lines(page, sup_size)
        if has_columns(rows, page.rect.width, body_size, split_ratio):
            doc.close()
            print("Input error: multiple columns on page %d; no output written" % pno, file=sys.stderr)
            return 2
        # Protect headings, captions and bibliography before any deletion.
        for r in rows:
            anchor = next(((title, lvl) for key, title, lvl in anchors.get(pno, [])
                           if norm(r["t_raw"]).startswith(key)), None)
            if anchor:
                matched_bookmarks.add((pno, anchor[0]))
            if anchor is None and not toc and head_sizes and NUM_HEAD_RE.match(r["t_raw"]):
                if any(abs(r["s"] - hs) <= 0.8 for hs in head_sizes):
                    anchor = (r["t_raw"], 2)
            bib_title = bool(BIB_RE.fullmatch(r["t_raw"]))
            if bib_title:
                bibliography = True
                bibliography_level = anchor[1] if anchor else None
                anchor = anchor or (r["t_raw"], 2)
            elif anchor and bibliography and (bibliography_level is None or anchor[1] <= bibliography_level):
                bibliography = False
            r["head"] = anchor
            r["bib"] = bibliography
            r["structure"] = bool(anchor or match_caption(r["t_raw"]))
            r["protected"] = bool(r["structure"] or bibliography)
            if r["protected"]:
                r["t"] = r["t_raw"]
        # Chrome must not participate in the body/footnote baseline.
        rows = [r for r in rows if r["structure"] or hdr_end * h <= r["y0"] <= ftr_start * h]
        rows = [r for r in rows if r["structure"] or not (
            PAGE_NO_RE.fullmatch(r["t_raw"]) and ftr_start < 1 and r["y0"] > ftr_start * h - 6)]
        # 0) 脚注：优先按"页底分隔线"切（版式信号，与字号无关）；无分隔线才退回字号判据
        cut_y = None
        cands = []
        for dr in page.get_drawings():
            r = dr["rect"]
            if (r.height <= 2 and r.width > 60 and r.y0 > h * 0.70
                    and 0.15 < r.width / page.rect.width < 0.55):
                cands.append(r.y0)
        for y0 in sorted(cands, reverse=True):
            below = [r for r in rows if r["y0"] > y0]
            # 三道校验：数量少、总量小、且全部是"注文字号"（含页码）。
            # 只要下方出现正文或标题字号，就说明那是图框线/表格线，不是脚注分隔线。
            if not below or len(below) > 10 or sum(len(r["t"]) for r in below) >= 600:
                continue
            if not any(r["protected"] for r in below) and all(
                    r["s"] <= body_size - 1 or PAGE_NO_RE.match(r["t"]) for r in below):
                cut_y = y0
                break
        has_sep = cut_y is not None
        if has_sep:
            rows = [r for r in rows if r["protected"] or r["y1"] < cut_y - 1]
        body_rows = [r for r in rows if r["s"] >= body_size - 0.3
                     and hdr_end * h <= r["y0"] <= ftr_start * h]
        body_bottom = max((r["y1"] for r in body_rows), default=0)
        fn_max = p.get("footnote_size")
        note_rows = [r for r in rows if not r["protected"] and fn_max is not None
                     and r["s"] <= fn_max + 0.3 and r["y0"] >= body_bottom - 1]
        # 必须在删除之前判定"本页有脚注"，否则删除后恒为 False，注号会被还原
        page_has_note = has_sep or bool(note_rows)
        rows = [r for r in rows if r not in note_rows]
        if not page_has_note:
            for r in rows:
                r["t"] = r["t_raw"]

        # Chrome was removed before statistics; preserve explicitly protected rows.
        boxes = tboxes.get(pno, [])
        rows = [r for r in rows if r["protected"] or not inside(r, boxes)]
        head_map = {i: r["head"] for i, r in enumerate(rows) if r["head"]}
        # 有书签就不做字号回退：封面作者名、书名常是大字号，回退会造出假标题

        regions = page_regions(page, rows)
        prev_y1 = None
        crossed = False
        for i, r in enumerate(rows):
            if not r["protected"] and regions and inside(r, regions):
                crossed = True          # 区域内的文字整块丢弃，但记住"跨过了区域"
                continue
            # 以 # // $ 开头的行多半是代码注释或 shell，不能当 Markdown 标题
            if re.match(r"^\s*(#|//|\$ )", r["t"]) and i not in head_map and not r["bib"]:
                if not code:
                    flush()
                code.append(r["code_text"])
                prev_y1 = r["y1"]
                continue
            if r.get("mono") and not r["bib"] and i not in head_map:
                if not code:
                    flush()
                code.append(r["code_text"])
                prev_y1 = r["y1"]
                continue
            if code:
                flush_code()
            if len(re.findall(r"\.\s\.\s\.", r["t"])) >= 3:   # 目录点线行
                continue
            cap = match_caption(r["t"])
            if cap and not r["bib"]:
                label, num, title = cap
                flush(); flush_code()
                sep = "" if "\u4e00" <= label[-1] <= "\u9fff" else " "
                out.append("> %s%s%s %s" % (label, sep, num, title))
                list_x = None
                prev_y1 = r["y1"]
                continue
            if i in head_map:
                flush()
                list_x = None
                htitle, hlvl = head_map[i]
                lvl = heading_level(htitle, level_source, d_min, hlvl)
                if prev_level and lvl > prev_level + 1:   # 不跳级
                    lvl = prev_level + 1
                prev_level = lvl
                out.append("#" * lvl + " " + htitle)
                prev_y1 = r["y1"]
                continue
            marker = LIST_RE.match(r["t"])
            if marker and not r["bib"]:
                flush()
                buf.append("- " + r["t"][marker.end():].strip())
                list_x = r["x0"]
                prev_y1 = r["y1"]
                continue
            if list_x is not None and r["x0"] <= list_x + 1:
                flush()
                list_x = None
            if r["bib"]:
                flush()
                out.append(r["t_raw"])
                prev_y1 = r["y1"]
                continue
            # 只有上一行已经收句，才在行距变大处断段；否则视为同一段被排版拉开
            gap = 0 if crossed else (r["y0"] - prev_y1 if prev_y1 is not None else 0)
            crossed = False
            if (para_gap is not None and prev_y1 is not None and gap > para_gap * 0.45
                    and buf and buf[-1].rstrip().endswith(TERMINATORS)):
                flush()
            buf.append(r["t"])
            prev_y1 = r["y1"]
        flush_code()
        if buf and buf[-1].rstrip().endswith(TERMINATORS):
            flush()
        out.append("")          # 段落未收尾则跨页续接

    flush()
    flush_code()
    expected_bookmarks = {(pno, title) for pno, entries in anchors.items()
                          for _, title, _ in entries}
    missing_bookmarks = sorted(expected_bookmarks - matched_bookmarks)
    if missing_bookmarks:
        limits.append("Unmatched bookmarks on selected pages: %d/%d; headings may be missing: %s" % (
            len(missing_bookmarks), len(expected_bookmarks),
            ", ".join("%d:%s" % item for item in missing_bookmarks[:5])))
    meta = """---
title: %s
source_file: %s
conversion: doc2md-for-ai / PyMuPDF 逐行 + 书签锚定，参数取自 baseline.json
units: %s
removed: 检出的位图区域文字、表格 bbox 内文字、脚注及页眉页脚带
note: |
  页范围 %s-%s；按 baseline 转换，未知区域保留供复核。
  图/表题改写为块引用锚点，代码保留为围栏块；书目保护跨页。
%s---

""" % (json.dumps(Path(pdf_path).stem, ensure_ascii=False),
       json.dumps(Path(pdf_path).name, ensure_ascii=False),
       json.dumps({"body_pt": body_size, "heading_pt": head_sizes,
                   "footnote_pt": p.get("footnote_size"), "leading": p.get("leading_median"),
                   "paragraph_gap": para_gap}, ensure_ascii=False),
       start, pages[-1],
       "known_limits: |\n" + "\n".join("  " + line for x in dict.fromkeys(limits)
                                      for line in x.splitlines()) + "\n")

    Path(out_path).write_text(meta + "\n\n".join(out).strip() + "\n", encoding="utf-8")
    doc.close()
    print("页数 %d，输出字符 %d" % (len(pages), len(meta) + len("\n\n".join(out))))
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print("Input/processing error: %s" % exc, file=sys.stderr)
        sys.exit(2)
