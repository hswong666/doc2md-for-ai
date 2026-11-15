#!/usr/bin/env python3
"""probe.py — 探测一本书的版面/结构指纹，产出 baseline.json。

设计纪律（改代码前必读）：
1. 推导不出的参数写 null；转换器跳过相应规则并声明限制。
2. 页带与分隔线位置用页高比例；字号、行距仍以点为单位。
3. 清洗先于统计：页码、水印、页眉不得参与正文基线计算。
4. 只回答"这本书长什么样"，不判断"该怎么转"。
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "assets"
THRESH_FILE = ASSETS / "thresholds.json"

EXIT_OK, EXIT_WARN, EXIT_INPUT, EXIT_ENCRYPTED, EXIT_NEEDS_OCR, EXIT_DEPS = 0, 1, 2, 3, 4, 5

NUM_RE = re.compile(r"\d+")
HEADING_NUM_RE = re.compile(r"^(\d+(?:\.\d+)*)")
CHAPTER_RE = re.compile(r"^(?:chapter|第)\s*(\d+)", re.I)


def load_thresholds() -> dict:
    with THRESH_FILE.open(encoding="utf-8") as f:
        data = json.load(f)
    return {k: v for k, v in data.items() if not k.startswith("_")}


def norm_text(t: str) -> str:
    """数字归一化成 N，用于跨页重复行检测（水印含日期/工号，精确比对会失配）。"""
    return NUM_RE.sub("N", re.sub(r"\s+", " ", t or "").strip())


def cluster_sizes(size_chars: dict, tol: float) -> list[dict]:
    """按字符量聚类字号，容差 tol（实测有 7.9/8.0、9.5/9.8 并存，不能精确匹配）。"""
    items = sorted(size_chars.items())
    clusters, cur = [], []
    for size, chars in items:
        if cur and size - cur[-1][0] <= tol:
            cur.append((size, chars))
        else:
            if cur:
                clusters.append(cur)
            cur = [(size, chars)]
    if cur:
        clusters.append(cur)
    out = []
    total = sum(c for _, c in items) or 1
    for group in clusters:
        chars = sum(c for _, c in group)
        out.append({
            "size": round(sum(s * c for s, c in group) / chars, 2),
            "char_mass": chars,
            "share": round(chars / total, 4),
            "members": [round(s, 2) for s, _ in group],
        })
    return sorted(out, key=lambda d: -d["char_mass"])


# ---------------------------------------------------------------- PDF


def page_lines(page) -> list[dict]:
    rows = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            text = "".join(s["text"] for s in line["spans"]).strip()
            if not text:
                continue
            rows.append({
                "t": text,
                "s": round(max(s["size"] for s in line["spans"]), 2),
                "x0": line["bbox"][0], "y0": line["bbox"][1],
                "x1": line["bbox"][2], "y1": line["bbox"][3],
            })
    rows.sort(key=lambda r: (round(r["y0"], 1), r["x0"]))
    return rows


def pick_pages(doc, th: dict) -> list[int]:
    total = doc.page_count
    n = min(int(th["probe.sample_pages"]), total)
    head = list(range(1, min(int(th["probe.head_pages"]), total) + 1))
    tail = list(range(max(1, total - int(th["probe.tail_pages"]) + 1), total + 1))
    rest = [i for i in range(1, total + 1) if i not in head and i not in tail]
    step = max(1, len(rest) // max(1, n - len(head) - len(tail)))
    body = rest[::step][: max(0, n - len(head) - len(tail))]
    dense = [t[2] for t in doc.get_toc(simple=True)[:400]][:2]
    pages = sorted(set(head + body + tail + dense))
    return pages[: n + 2]


def has_columns(rows, width, body_size, split_ratio):
    """Require disjoint left/right body lines on repeated shared baselines."""
    body = [r for r in rows if abs(r["s"] - body_size) <= 0.5]
    # Short table cells / diagram labels are not evidence of prose columns.
    prose = [r for r in body if r["x1"] - r["x0"] >= width * 0.18]
    left = [r for r in prose if r["x1"] < width * 0.52]
    right = [r for r in prose if r["x0"] >= width * 0.5]
    paired = sum(any(abs(a["y0"] - b["y0"]) < body_size * 0.5 for b in right) for a in left)
    return (paired >= 4 and len(left) >= len(body) * split_ratio
            and len(right) >= len(body) * split_ratio)


def chrome_bands(doc, per_page, th):
    repeat = {}
    for pno, rows in per_page.items():
        h = doc[pno - 1].rect.height
        for r in rows:
            yr = r["y0"] / h
            side = ("top" if yr < float(th["probe.band_top_ratio"]) else
                    "bottom" if yr > 1 - float(th["probe.band_bottom_ratio"]) else None)
            if side is None:
                continue
            key = norm_text(r["t"])
            if len(key) < 4 and key != "N":
                continue
            rec = repeat.setdefault((side, key), {"pages": set(), "ys": [], "sizes": Counter()})
            rec["pages"].add(pno)
            rec["ys"].append(r["y0"] / h)
            rec["sizes"][r["s"]] += 1
    tops, bottoms = [], []
    for (side, key), rec in repeat.items():
        if len(rec["pages"]) < max(2, float(th["probe.repeat_line_ratio"]) * len(per_page)):
            continue
        y = statistics.median(rec["ys"])
        item = {"text": key[:60], "pages": len(rec["pages"]), "y_ratio": round(y, 3),
                "size": rec["sizes"].most_common(1)[0][0]}
        if y < float(th["probe.band_top_ratio"]):
            tops.append(item)
        elif y > 1 - float(th["probe.band_bottom_ratio"]):
            bottoms.append(item)
    header = [0.0, round(max(i["y_ratio"] for i in tops) + 0.01, 3)] if tops else None
    footer = [round(min(i["y_ratio"] for i in bottoms) - 0.01, 3), 1.0] if bottoms else None
    # Occupancy alone cannot distinguish a changing header from real first lines.
    return header, footer, tops, bottoms


def probe_pdf(path: Path, th: dict) -> dict:
    import pymupdf

    doc = pymupdf.open(str(path))
    if doc.needs_pass:
        return {"verdict": "encrypted", "_exit": EXIT_ENCRYPTED,
                "warnings": [{"code": "ENCRYPTED", "msg": "PDF 需要密码，无法探测"}]}
    if doc.page_count == 0:
        return {"verdict": "invalid", "_exit": EXIT_INPUT,
                "warnings": [{"code": "EMPTY", "msg": "0 页"}]}

    pages = pick_pages(doc, th)
    tol = float(th["probe.size_cluster_tolerance"])
    per_page: dict[int, list[dict]] = {}
    size_chars: dict[float, int] = defaultdict(int)
    total_chars = 0
    for pno in pages:
        rows = page_lines(doc[pno - 1])
        per_page[pno] = rows
        for r in rows:
            size_chars[round(r["s"], 1)] += len(r["t"])
            total_chars += len(r["t"])

    chars_per_page = total_chars / max(1, len(pages))
    if chars_per_page < float(th["probe.min_chars_per_page"]):
        return {"verdict": "needs_ocr", "_exit": EXIT_NEEDS_OCR,
                "profile": {"page": {"count": doc.page_count, "text_chars_per_page": round(chars_per_page)}},
                "warnings": [{"code": "NO_TEXT_LAYER",
                              "msg": "抽样页平均 %.0f 字符/页，疑似扫描件，需先 OCR" % chars_per_page}]}

    header_band, footer_band, tops, bottoms = chrome_bands(doc, per_page, th)
    top_cut = header_band[1] if header_band else 0.0
    bot_cut = footer_band[0] if footer_band else 1.0
    size_chars.clear()
    for pno, rows in per_page.items():
        h = doc[pno - 1].rect.height
        for r in rows:
            if top_cut <= r["y0"] / h <= bot_cut:
                size_chars[round(r["s"], 1)] += len(r["t"])
    if not size_chars:
        doc.close()
        return {"verdict": "invalid", "_exit": EXIT_INPUT,
                "warnings": [{"code": "NO_BODY", "msg": "No text remains outside detected page bands"}]}
    clusters = cluster_sizes(dict(size_chars), tol)
    body = clusters[0]
    body_size = body["size"]

    # 书签质量：页内逐字命中（去空白后比较）
    toc = doc.get_toc(simple=False)
    hit_sizes: Counter = Counter()
    hits = 0
    numbered = 0
    max_depth = 0
    for lvl, title, page, dest in toc:
        title = (title or "").strip()
        if not title or page < 1 or page > doc.page_count:
            continue
        m = HEADING_NUM_RE.match(title)
        if m:
            numbered += 1
            max_depth = max(max_depth, m.group(1).count(".") + 1)
        rows = per_page.get(page)
        if rows is None:
            rows = page_lines(doc[page - 1])
            per_page[page] = rows
        key = re.sub(r"\s+", "", title).lower()[:14]
        if not key:
            continue
        for r in rows:
            if re.sub(r"\s+", "", r["t"]).lower().startswith(key):
                hits += 1
                hit_sizes[round(r["s"], 1)] += 1
                break
    hit_rate = round(hits / max(1, len(toc)), 3)
    heading_sizes = sorted({s for s, c in hit_sizes.items() if c >= 2}, reverse=True)
    heading_sizes = [s for s in heading_sizes if abs(s - body_size) > 0.3]

    # 标题层级来源：编号深度超过书签层级 → 必须按编号重建
    level_max = max((lvl for lvl, _, _, _ in toc), default=0)
    if max_depth >= level_max and max_depth >= 2 and numbered >= 0.5 * max(1, len(toc)):
        level_source = "numbering"
    elif hit_rate >= float(th["probe.bookmark_anchor_strict"]):
        level_source = "bookmark_level"
    else:
        level_source = "size"

    # 行距中位数（正文行）
    leads = []
    for pno, rows in per_page.items():
        h = doc[pno - 1].rect.height
        ys = [r["y0"] for r in rows if abs(r["s"] - body_size) <= 0.5
              and top_cut <= r["y0"] / h <= bot_cut]
        leads += [b - a for a, b in zip(ys, ys[1:]) if 0 < b - a < body_size * 3]
    leading = round(statistics.median(leads), 2) if leads else None
    gaps = [g for g in leads if leading and g > leading * 1.2]
    paragraph_gap = round(statistics.median(gaps), 2) if gaps else None

    # 分栏判定
    multi = 0
    for pno, rows in per_page.items():
        width = doc[pno - 1].rect.width
        if has_columns(rows, width, body_size, float(th["probe.column_split_ratio"])):
            multi += 1
    columns = {"n": 2 if multi else 1,
               "gutter_ratio": 0.5 if multi else None}

    # 页码形态
    page_number = "none"
    if tops and any("N" in i["text"] for i in tops):
        page_number = "fused_in_header"
    elif bottoms and any(re.fullmatch(r"N", i["text"].strip()) for i in bottoms):
        page_number = "independent"

    # 脚注字号：小于正文、且多数位于本页正文末行之下
    # 铁律：清洗先于统计。水印常与正文同字号（本书 10pt），若纳入会把正文基线顶到页底
    body_bottom_ratio = {}
    bot_cut = footer_band[0] if footer_band else 1.0
    top_cut = header_band[1] if header_band else 0.0
    for pno, rows in per_page.items():
        h = doc[pno - 1].rect.height
        ys = [r["y1"] for r in rows
              if r["s"] >= body_size - 0.2 and top_cut <= r["y1"] / h <= bot_cut]
        body_bottom_ratio[pno] = (max(ys) / h) if ys else 0.0
    below: dict[float, list[bool]] = defaultdict(list)
    top_lim = header_band[1] if header_band else 0.0
    bot_lim = footer_band[0] if footer_band else 1.0
    for pno, rows in per_page.items():
        h = doc[pno - 1].rect.height
        for r in rows:
            yr = r["y0"] / h
            if yr < top_lim or yr > bot_lim:   # 页眉/页脚带内的行不是脚注
                continue
            size = round(r["s"], 1)
            if size >= body_size - 0.3:
                continue
            if any(abs(size - hs) <= 0.3 for hs in heading_sizes):
                continue
            below[size].append(r["y0"] / h >= body_bottom_ratio[pno])
    footnote_size = None
    for size in sorted(below, reverse=True):
        flags = below[size]
        if len(flags) >= 3 and sum(flags) / len(flags) >= float(th["probe.footnote_below_body_ratio"]):
            footnote_size = size
            break
    sup_sizes: Counter = Counter()
    for pno in per_page:
        for block in doc[pno - 1].get_text("dict")["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block["lines"]:
                base = max((sp["size"] for sp in line["spans"]), default=0)
                for sp in line["spans"]:
                    if sp["size"] <= base - 1.0 and re.fullmatch(r"\d{1,2}", sp["text"].strip() or "-"):
                        sup_sizes[round(sp["size"], 1)] += 1
    superscript = sup_sizes.most_common(1)[0][0] if sup_sizes else None

    # 表格通道（抽样，pdfplumber 边框法）
    table_pages = 0
    checked = 0
    try:
        import pdfplumber

        with pdfplumber.open(str(path)) as pdf:
            for pno in pages[:12]:
                checked += 1
                try:
                    if pdf.pages[pno - 1].find_tables():
                        table_pages += 1
                except Exception:
                    pass
    except ImportError:
        pass
    if checked and table_pages / checked >= float(th["probe.table_page_ratio"]):
        table_mode = "ruled"
    else:
        table_mode = "none"

    # 脚注分隔线：出版排版规范，比字号稳定（实测 91/313 页有，宽度约占页宽 32%）
    sep_hits = []
    for pno in pages:
        pg = doc[pno - 1]
        H, W = pg.rect.height, pg.rect.width
        cand = []
        for dr in pg.get_drawings():
            r = dr["rect"]
            if r.height <= 2 and r.width > 60 and r.y0 > H * 0.55 and 0.15 < r.width / W < 0.75:
                cand.append((r.y0 / H, r.width / W))
        if cand:
            # 页内取最低的那条：脚注分隔线总在注文正上方，表格线通常更高
            sep_hits.append(max(cand, key=lambda t: t[0]))
    footnote_sep = None
    if len(sep_hits) >= max(2, 0.05 * len(pages)):
        ys = sorted(y for y, _ in sep_hits)
        ws = sorted(w for _, w in sep_hits)
        footnote_sep = {
            "pages_hit": len(sep_hits),
            "y_ratio": round(ys[len(ys) // 2], 3),
            "width_ratio": round(ws[len(ws) // 2], 3),
        }

    profile = {
        "page": {"count": doc.page_count, "width": round(doc[0].rect.width, 1),
                 "height": round(doc[0].rect.height, 1),
                 "text_chars_per_page": round(chars_per_page)},
        "size_clusters": clusters[:6],
        "bands": {"header": header_band, "footer": footer_band,
                  "repeated": (tops + bottoms)[:8]},
        "page_number": {"mode": page_number},
        "toc": {"count": len(toc), "level_max": level_max, "verbatim_hit_rate": hit_rate,
                "numbered_ratio": round(numbered / max(1, len(toc)), 3),
                "heading_sizes": heading_sizes},
        "columns": columns,
        "table_detect": {"pages_checked": checked, "pages_with_tables": table_pages},
        "footnote_separator": footnote_sep,
    }

    params = {
        "body_size": body_size,
        "body_tolerance": max(0.25, round((clusters[1]["size"] - body_size) / 2, 2)) if len(clusters) > 1 else 0.3,
        "heading_sizes": heading_sizes or None,
        "footnote_size": footnote_size,
        "footnote_separator_y_ratio": footnote_sep["y_ratio"] if footnote_sep else None,
        "superscript_size": superscript,
        "chrome_bands": {"header": header_band, "footer": footer_band},
        "page_number": page_number,
        "bookmark_anchor": ("text_anchor" if hit_rate >= float(th["probe.bookmark_anchor_strict"])
                            else "text_anchor+size_snap" if hit_rate >= float(th["probe.bookmark_anchor_loose"])
                            else "numbering"),
        "heading_level_source": level_source,
        "leading_median": leading,
        "paragraph_gap": paragraph_gap,
        "columns": columns,
        "text_layer": "text",
        "table_mode": table_mode,
    }
    meta = {
        "body_size": {"derivation": "字号直方图字符量最大簇", "confidence": body["share"]},
        "heading_sizes": {"derivation": "书签页内逐字命中行的字号集合", "confidence": hit_rate},
        "footnote_separator_y_ratio": {"derivation": "页底分隔线的页高比例（有则优先用它切脚注）",
                                       "confidence": 0.9 if footnote_sep else 0.0},
        "footnote_size": {"derivation": "小于正文且多数位于本页正文末行之下",
                          "confidence": 0.6 if footnote_size else 0.0},
        "chrome_bands": {"derivation": "归一化跨页重复行 + 位置带", "confidence": 0.7 if header_band else 0.0},
        "page_number": {"derivation": "页带内短行形态", "confidence": 0.8 if page_number != "none" else 0.2},
        "heading_level_source": {"derivation": "编号最大深度 %d vs 书签最大层级 %d" % (max_depth, level_max),
                                 "confidence": 0.9},
        "leading_median": {"derivation": "正文行 y0 差中位数", "confidence": 0.8},
        "paragraph_gap": {"derivation": "大于 1.2 倍行距的间隙中位数", "confidence": 0.7},
        "columns": {"derivation": "正文行左右半页分布", "confidence": 0.7},
        "table_mode": {"derivation": "pdfplumber 抽样页表框命中率", "confidence": 0.7},
    }

    warnings = []
    if header_band is None:
        warnings.append({"code": "HEADER_NONE", "msg": "未发现跨页重复的页眉行；禁止按固定 y 删页眉"})
    if footnote_size is None:
        warnings.append({"code": "FOOTNOTE_NONE", "msg": "未识别出脚注字号；不得套用其他书的阈值"})
    if page_number == "none":
        warnings.append({"code": "PAGENO_NONE", "msg": "未识别页码形态，可能导致页码残留"})
    if hit_rate < float(th["probe.bookmark_anchor_loose"]):
        warnings.append({"code": "TOC_AMBIGUOUS", "msg": "书签逐字命中率偏低，标题需按编号重建"})
    if multi:
        warnings.append({"code": "MULTI_COLUMN", "msg": "检测到多栏；自带转换器将拒绝，请使用分栏解析路线"})

    verdict = "low_confidence" if body["share"] < 0.5 else "ok"
    doc.close()
    return {"verdict": verdict, "_exit": EXIT_OK,
            "profile": profile, "params": params, "params_meta": meta,
            "warnings": warnings, "sampling": {"pages_sampled": len(pages), "full": False}}


# ---------------------------------------------------------------- EPUB


def probe_epub(path: Path, th: dict) -> dict:
    with zipfile.ZipFile(str(path)) as z:
        names = [n for n in z.namelist() if n.lower().endswith((".xhtml", ".html", ".htm"))]
        docs = {n: z.read(n).decode("utf-8", "ignore") for n in names}

    if not docs:
        return {"verdict": "invalid", "_exit": EXIT_INPUT,
                "warnings": [{"code": "EMPTY", "msg": "未找到 xhtml 文档"}]}

    joined = "\n".join(docs.values())
    heads = {f"h{i}": 0 for i in range(1, 7)}
    for m in re.finditer(r"<h([1-6])[\s>]", joined, re.I):
        heads["h" + m.group(1)] += 1
    foot_p = len(re.findall(r"<p[^>]*class=\"[^\"]*footnote", joined, re.I))
    inline = len(re.findall(r"<sup>\s*\[", joined, re.I))
    imgs = re.findall(r"<img[^>]*>", joined, re.I)
    captions = re.findall(r"(?:图|表)\s*\d+[-.]\d+|(?:figure|table)\s+\d+", joined, re.I)
    tables = len(re.findall(r"<table[\s>]", joined, re.I))
    classes = Counter(re.findall(r"class=\"([^\"]+)\"", joined))
    cjk = len(re.findall(r"[\u4e00-\u9fff]", joined))
    ufffd = joined.count("\ufffd")
    chars = len(re.sub(r"<[^>]+>", "", joined))

    # 题注在图片前还是后：统计两种相邻形态
    before = after = 0
    for m in re.finditer(r"<img[^>]*>", joined, re.I):
        head = joined[max(0, m.start() - 400):m.start()]
        tail = joined[m.end():m.end() + 400]
        if re.search(r"<(?:p|div)[^>]*>\s*(?:图|表)\s*\d+[-.]\d+", head, re.I):
            before += 1
        elif re.search(r"<(?:p|div)[^>]*>\s*(?:图|表)\s*\d+[-.]\d+", tail, re.I):
            after += 1
    caption_side = "both" if before and after else "before" if before else "after" if after else "none"

    profile = {
        "spine": {"docs": len(docs), "chars_total": chars},
        "headings": {"tags": heads, "max_level": max([i for i in range(1, 7) if heads["h%d" % i]] or [0])},
        "footnote": {"p_class_count": foot_p, "inline_link_count": inline,
                     "ratio_check": round(inline / foot_p, 2) if foot_p else None},
        "images": {"count": len(imgs), "caption_like": len(captions)},
        "tables": {"html_tables": tables},
        "css": {"top_classes": [c for c, _ in classes.most_common(8)]},
        "text_layer": {"chars": chars, "cjk_ratio": round(cjk / max(1, chars), 3), "ufffd": ufffd},
    }
    params = {
        "body_size": None,
        "heading_sizes": None,
        "footnote_size": None,
        "superscript_size": None,
        "chrome_bands": {"header": None, "footer": None},
        "page_number": "none",
        "bookmark_anchor": "dom_heading",
        "heading_level_source": "dom_heading",
        "leading_median": None,
        "columns": {"n": 1, "gutter_ratio": None},
        "text_layer": "text",
        "table_mode": "html" if tables else "image" if imgs else "none",
        "caption_side": caption_side,
        "footnote_marker": "inline_sup_link" if inline else None,
    }
    meta = {
        "bookmark_anchor": {"derivation": "EPUB 有语义标题标签，直接取 DOM", "confidence": 0.95},
        "heading_level_source": {"derivation": "h1–h6", "confidence": 0.95},
        "caption_side": {"derivation": "img 前/后 400 字符内是否有题注", "confidence": 0.7},
        "table_mode": {"derivation": "<table> 数量与 <img> 数量", "confidence": 0.8},
    }
    warnings = []
    if foot_p and inline and abs(inline / foot_p - 1) > 0.2:
        warnings.append({"code": "FOOTNOTE_RATIO", "msg": "行内脚注标记与尾注段落数不成比例"})
    if not tables and imgs:
        warnings.append({"code": "TABLE_IS_IMAGE", "msg": "无 HTML 表格且有图片，表格可能是图片，文字层为 0"})
    if heads["h1"] + heads["h2"] + heads["h3"] == 0:
        warnings.append({"code": "NO_HEADINGS", "msg": "未见 h1–h3，需按编号重建标题"})

    return {"verdict": "ok", "_exit": EXIT_OK, "profile": profile, "params": params,
            "params_meta": meta, "warnings": warnings,
            "sampling": {"docs_scanned": len(docs), "full": True}}


# ---------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description="探测 PDF/EPUB 的版面与结构指纹")
    ap.add_argument("input", help="PDF 或 EPUB 路径")
    ap.add_argument("-o", "--out", help="baseline.json 输出路径")
    ap.add_argument("--json", action="store_true", help="stdout 只输出 JSON")
    args = ap.parse_args()
    if args.out and Path(args.out).resolve() in (Path(args.input).resolve(), THRESH_FILE.resolve()):
        print("输出路径不得覆盖源文件或配置", file=sys.stderr)
        return EXIT_INPUT

    try:
        th = load_thresholds()
    except FileNotFoundError:
        print("找不到 thresholds.json（应与 scripts/ 同级的 assets/ 目录）", file=sys.stderr)
        return EXIT_INPUT

    path = Path(args.input)
    if not path.exists():
        print("文件不存在: %s" % path, file=sys.stderr)
        return EXIT_INPUT

    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            result = probe_pdf(path, th)
        elif suffix == ".epub":
            result = probe_epub(path, th)
        else:
            print("只支持 .pdf / .epub，收到 %s" % suffix, file=sys.stderr)
            return EXIT_INPUT
    except ImportError as exc:
        print("缺少依赖: %s（用 workbuddy 内置 python env）" % exc, file=sys.stderr)
        return EXIT_DEPS

    exit_code = result.pop("_exit", EXIT_OK)
    baseline = {
        "schema_version": "1.0",
        "kind": suffix.lstrip("."),
        "artifact": {"path": str(path.resolve()), "bytes": path.stat().st_size},
        "verdict": result.get("verdict", "ok"),
        "profile": result.get("profile", {}),
        "params": result.get("params", {}),
        "params_meta": result.get("params_meta", {}),
        "warnings": result.get("warnings", []),
        "sampling": result.get("sampling", {}),
    }

    if args.out:
        Path(args.out).write_text(json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(baseline, ensure_ascii=False, indent=2))
    else:
        print("verdict: %s   kind: %s   页/文档: %s" % (
            baseline["verdict"], baseline["kind"],
            baseline["profile"].get("page", {}).get("count")
            or baseline["profile"].get("spine", {}).get("docs")))
        for key, val in baseline["params"].items():
            print("  %-20s %s" % (key, json.dumps(val, ensure_ascii=False)))
        for w in baseline["warnings"]:
            print("  [%s] %s" % (w["code"], w["msg"]))
        if args.out:
            print("→ 已写入 %s" % args.out)
    return exit_code


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as exc:
        print("输入/配置无法处理: %s" % exc, file=sys.stderr)
        sys.exit(EXIT_INPUT)
