#!/usr/bin/env python3
"""check.py — 转换后自检：按 11 条标准判这份 Markdown 能不能给 AI 用。

11 条标准：8 条自动（进退出码）+ 3 条抽样（只列清单，不进退出码）。
验收阈值来自 assets/thresholds.json；语法和算法结构常量在代码中。

退出码：0 通过 / 1 有 blocker / 2 输入错误 / 3 证据不足（可判覆盖率 < 下限）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from captions import BIB_RE, REFERENCE, identity, match_caption

ASSETS = Path(__file__).resolve().parent.parent / "assets"
THRESH_FILE = ASSETS / "thresholds.json"

EXIT_OK, EXIT_BLOCKER, EXIT_INPUT, EXIT_INSUFFICIENT = 0, 1, 2, 3

NOTE_RE = re.compile(r"^\[\d{1,3}\]\s")
HEAD_NUM_RE = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+\S")
CHAPTER_RE = re.compile(r"^(?:chapter\s*(\d+)|第\s*(\d+)\s*章)", re.I)
TERM = tuple(".!?:;。！？：；") + ("）", ")", "”", '"', "]")


def load_thresholds() -> dict:
    with THRESH_FILE.open(encoding="utf-8") as f:
        data = json.load(f)
    return {k: v for k, v in data.items() if not k.startswith("_")}


def split_front_matter(text: str):
    """行首锚定解析 YAML，不用 split('---')——正文里真的有 --- 分隔线。"""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            meta = {}
            for raw in lines[1:i]:
                if ":" in raw and not raw.startswith((" ", "\t")):
                    key, _, val = raw.partition(":")
                    meta[key.strip()] = val.strip()
            return meta, "\n".join(lines[i + 1:])
    return {}, text


def strip_fences(text: str) -> str:
    """把 ``` 围栏内的内容清空（保留行数）。代码里的 # 不是标题、代码行不是段落。"""
    out, inside = [], False
    for line in text.split("\n"):
        if line.strip().startswith("```"):
            inside = not inside
            out.append("")
            continue
        out.append("" if inside else line)
    return "\n".join(out)


def parse_blocks(body: str):
    """返回 [(起始行号, 文本)]，块以空行分隔。"""
    blocks, buf, start = [], [], 0
    for idx, line in enumerate(body.split("\n")):
        if line.strip():
            if not buf:
                start = idx
            buf.append(line)
        elif buf:
            blocks.append((start, "\n".join(buf)))
            buf = []
    if buf:
        blocks.append((start, "\n".join(buf)))
    return blocks


def headings_of(body: str):
    out = []
    for idx, line in enumerate(body.split("\n")):
        m = re.match(r"^(#{1,6})\s+(\S.*)$", line)
        if m:
            out.append({"line": idx, "level": len(m.group(1)), "text": m.group(2).strip()})
    return out


def norm_block(t: str) -> str:
    return re.sub(r"\d+", "N", re.sub(r"\s+", " ", t)).strip()


def is_heading(block: str) -> bool:
    return bool(re.match(r"^#{1,6}\s", block))


def is_list(block: str) -> bool:
    return bool(re.match(r"^\s*(?:[-*+]|\d+[.)])\s", block))


def is_quote(block: str) -> bool:
    return block.lstrip().startswith(">")


def ends_sentence(t: str) -> bool:
    return t.rstrip().endswith(TERM)


class Report:
    def __init__(self):
        self.items = []
        self.stds = {}

    def add(self, sid, title, status, expectation, actual, fix, evidence=""):
        self.items.append({
            "id": sid, "title": title, "status": status,
            "expect": expectation, "actual": actual, "fix": fix, "evidence": evidence,
        })
        std = re.match(r"\d+", sid).group(0)
        self.stds.setdefault(std, []).append(status)

    @property
    def judged(self):
        return sum(1 for st in self.stds.values() if any(x != "UNVERIFIED" for x in st))

    @property
    def unverified(self):
        return [k for k, v in sorted(self.stds.items(), key=lambda kv: int(kv[0])) if all(x == "UNVERIFIED" for x in v)]

    def blockers(self):
        return [i for i in self.items if i["status"] == "BLOCKER"]


# ------------------------------------------------------------------ 标准实现


def std1(report, heads, th):
    h1 = [h for h in heads if h["level"] == 1]
    nums = []
    for h in h1:
        m = CHAPTER_RE.match(h["text"])
        if m:
            nums.append(int(m.group(1) or m.group(2)))
    if len(nums) >= 2:
        want = set(range(min(nums), max(nums) + 1))
        missing = sorted(want - set(nums))
        dup = sorted(k for k, v in Counter(nums).items() if v > 1)
        if missing or dup:
            report.add("1a", "章节编号连续", "BLOCKER",
                       "已观测最小章号至最大章号无缺无重",
                       "缺 %s 重 %s" % (missing or "无", dup or "无"),
                       "转换时章节丢失或重复，检查分章切分与末章截断")
        else:
            report.add("1a", "章节编号连续", "PASS", "无缺号无重号", "第 %d–%d 章齐全" % (min(nums), max(nums)),
                       "")
    else:
        report.add("1a", "章节编号连续", "UNVERIFIED", "可识别章号", "H1 中未识别到章号", "若产物无章号，依赖标准 7 判断分块质量")

    jumps, prev = [], None
    for h in heads:
        if prev is not None and h["level"] - prev > int(th["std1.max_level_jump"]):
            jumps.append((h["line"] + 1, prev, h["level"]))
        prev = h["level"]
    if jumps:
        report.add("1b", "标题层级不跳级", "BLOCKER", "相邻标题 level 增量 ≤ 1",
                   "%d 处跳级，如第 %d 行 %d→%d" % (len(jumps), jumps[0][0], jumps[0][1], jumps[0][2]),
                   "按编号段数重建层级，不要照搬书签 level", "行 %d" % jumps[0][0])
    else:
        report.add("1b", "标题层级不跳级", "PASS", "增量 ≤ 1", "无跳级", "")

    if len(heads) < int(th["sample.min_headings"]):
        report.add("1c", "深层节点占比", "UNVERIFIED", "达到标题样本下限",
                   "仅 %d 个标题（节选）" % len(heads), "对全文重跑")
    elif heads:
        depth = Counter(h["level"] for h in heads)
        top = max(depth)
        share = depth[top] / len(heads)
        if top < 5:
            report.add("1c", "深层节点占比", "PASS", "层级 ≤ 4 时不适用",
                       "最深 H%d，占 %.1f%%（结构正常）" % (top, share * 100), "")
        elif share > float(th["std1.max_depth_share"]):
            report.add("1c", "深层节点占比", "MAJOR",
                       "最深层级节点 ≤ %d%%" % (float(th["std1.max_depth_share"]) * 100),
                       "H%d 占 %.1f%%（%d/%d）" % (top, share * 100, depth[top], len(heads)),
                       "切块时按编号段数判层级，不要一律按 ### 切")
        else:
            report.add("1c", "深层节点占比", "PASS", "≤ 5%", "H%d 占 %.1f%%" % (top, share * 100), "")


def std2(report, body, blocks, th):
    lines = [l for l in body.split("\n") if l.strip()]
    noise_pat = [
        r"^\s*\|", r"!\[", r"^Order \d+ by ", r"^\d{1,3}\s*[•|]\s*\S+$",
        r"^\d{1,3}$", r"^C\s?H\s?A\s?P\s?T\s?E\s?R\s*\d+$",
    ]
    noise = [l for l in lines if any(re.search(p, l) for p in noise_pat)]
    ratio = len(noise) / max(1, len(lines))
    if ratio > float(th["std2.noise_ratio_max"]):
        report.add("2a", "版面噪声比例", "MAJOR",
                   "< %d%% 行" % (float(th["std2.noise_ratio_max"]) * 100),
                   "%.2f%%（%d/%d 行）" % (ratio * 100, len(noise), len(lines)),
                   "按内容白名单剔除页眉页脚水印，不要用固定 y 阈值",
                   "首例：%s" % noise[0][:40] if noise else "")
    else:
        report.add("2a", "版面噪声比例", "PASS", "< 1% 行", "%.2f%%（%d 行）" % (ratio * 100, len(noise)), "")

    first_head = next((i for i, b in enumerate(blocks) if is_heading(b[1])), None)
    if first_head is None:
        report.add("2b", "首个标题前无噪声块", "UNVERIFIED", "有标题", "文档无标题", "先修标题层级")
    else:
        prefix = [b for b in blocks[:first_head]
                  if not is_heading(b[1]) and len(b[1]) < 200]
        if len(prefix) >= int(th["std2.prefix_noise_run"]):
            report.add("2b", "首个标题前无噪声块", "BLOCKER",
                       "首个标题前非正文块 < %d" % int(th["std2.prefix_noise_run"]),
                       "%d 个短块（版权/CIP/元数据）" % len(prefix),
                       "删除封面后的版权页与元数据块，它们正好压在 embedding 最敏感处",
                       "行 %d 起" % (prefix[0][0] + 1))
        else:
            report.add("2b", "首个标题前无噪声块", "PASS", "< 3 个", "%d 个" % len(prefix), "")

    long_blocks = [b for b in blocks if len(b[1]) >= int(th["std2.dup_block_min_chars"])]
    dup = [k for k, v in Counter(norm_block(b[1]) for b in long_blocks).items() if v > 1]
    dup_ratio = len(dup) / max(1, len(long_blocks))
    if dup_ratio > float(th["std2.dup_block_ratio_max"]):
        report.add("2c", "重复长块比例", "MINOR",
                   "< %d%%" % (float(th["std2.dup_block_ratio_max"]) * 100),
                   "%.2f%%（%d 类）" % (dup_ratio * 100, len(dup)),
                   "元数据块与正文重复，删掉 front matter 之外的描述块")
    else:
        report.add("2c", "重复长块比例", "PASS", "< 0.5%", "%.2f%%" % (dup_ratio * 100), "")


def std3(report, body, th):
    anchors, refs = set(), set()
    for line in body.split("\n"):
        s = line.strip()
        cap = match_caption(s)
        if cap:
            anchors.add(identity(cap[0], cap[1]))
            continue
        # Even a quote can contain a reference; only actual captions self-exclude.
        for m in REFERENCE.finditer(s):
            refs.add(identity(m["label"], m["num"]))
    if not refs:
        report.add("3a", "图表引用有锚点", "UNVERIFIED", "存在图表引用", "正文未引用任何图表号",
                   "无引用时该项恒真，不判通过")
        return
    missing = sorted(refs - anchors)
    ratio = len(missing) / max(1, len(refs))
    if missing and ratio > float(th["std3.blocker_ratio"]):
        report.add("3a", "图表引用有锚点", "BLOCKER",
                   "引用集合 ⊆ 锚点集合",
                   "缺 %d 个：%s" % (len(missing), ", ".join(" ".join(m) for m in missing[:6])),
                   "补 `> 图X-Y 标题` 锚点，或把正文里的引用改成文字描述")
    elif missing:
        report.add("3a", "图表引用有锚点", "MAJOR",
                   "引用集合 ⊆ 锚点集合",
                   "%d/%d 个引用无对应锚点（%.0f%%）：%s" % (
                       len(missing), len(refs), ratio * 100,
                       ", ".join(" ".join(m) for m in missing[:6])),
                   "少量悬空引用可接受；若正文靠图讲结论，需补锚点")
    else:
        report.add("3a", "图表引用有锚点", "PASS", "引用 ⊆ 锚点",
                   "%d 个引用全部有锚点" % len(refs), "")


def std4(report, blocks, th):
    prose = []
    bib_level = None
    for b in blocks:
        if is_heading(b[1]):
            level = len(b[1]) - len(b[1].lstrip("#"))
            title = b[1].lstrip("#").strip()
            if BIB_RE.fullmatch(title):
                bib_level = level
            elif bib_level is not None and level <= bib_level:
                bib_level = None
        elif bib_level is None:
            prose.append(b)
    notes = [(b[0] + 1, b[1][:50]) for b in prose if NOTE_RE.match(b[1].strip())]
    web_notes = [b for b in prose
                 if re.match(r"^\d{1,3}\s+\S", b[1].strip())
                 and "http" in b[1] and len(b[1]) < 400]
    if len(notes) > int(th["std4.footnote_sample_hits_max"]):
        report.add("4a", "脚注未混入正文", "BLOCKER",
                   "无 `[N]` 开头段落", "%d 处，如第 %d 行" % (len(notes), notes[0][0]),
                   "脚注要么整体删除，要么统一移到章末，不得混在正文流里",
                   "行 %d" % notes[0][0])
    elif len(web_notes) > int(th["std4.web_note_hits_max"]):
        report.add("4a", "脚注未混入正文", "MAJOR", "无页底注文段落",
                   "%d 段疑似注文（数字开头且含 URL）" % len(web_notes),
                   "检查页底 8pt 块是否被当正文保留", "行 %d" % (web_notes[0][0] + 1))
    else:
        report.add("4a", "脚注未混入正文", "PASS", "无注文段落", "未发现", "")


def std5(report, blocks, th):
    paras = [(i, b) for i, b in enumerate(blocks)
             if len(b[1]) >= int(th["std5.min_para_chars"])
             and not is_heading(b[1]) and not is_list(b[1]) and not is_quote(b[1])
             and not b[1].lstrip().startswith(("|", "```"))]
    trunc = []
    for i, (ln, text) in paras:
        if ends_sentence(text):
            continue
        nxt = blocks[i + 1][1].strip() if i + 1 < len(blocks) else ""
        # 下一块是列表/标题/图锚点/短标签，都算正常的"段落结束"，不是断句
        if nxt and (nxt[:1].islower() or is_list(nxt) or is_heading(nxt)
                    or nxt.startswith(">") or len(nxt) < 40):
            continue
        trunc.append(ln + 1)
    ratio = len(trunc) / max(1, len(paras))
    if ratio > float(th["std5.truncation_ratio_max"]):
        report.add("5a", "段落完整（无断句）", "BLOCKER",
                   "断句段落 < %d%%" % (float(th["std5.truncation_ratio_max"]) * 100),
                   "%.2f%%（%d/%d）" % (ratio * 100, len(trunc), len(paras)),
                   "对照源文检查缺标点或跨页断句；文末未收句也必须保留，不能静默丢弃",
                   "首例行 %d" % trunc[0] if trunc else "")
    else:
        report.add("5a", "段落完整（无断句）", "PASS",
               "< %d%%" % (float(th["std5.truncation_ratio_max"]) * 100),
               "%.2f%%（%d 处）" % (ratio * 100, len(trunc)), "")

    joined = body_join(blocks)
    dropcap = []
    for b in blocks:
        m = re.match(r"^([A-Z])\s+([a-z]{1,6})\b", b[1].strip())
        if not m:
            continue
        word = (m.group(1) + m.group(2))
        if re.search(r"\b%s\b" % re.escape(word.capitalize()), joined):
            dropcap.append(b[0] + 1)
    report.add("5b", "无首字母下沉粘连", "PASS" if not dropcap else "MAJOR",
               "0 处", "%d 处" % len(dropcap),
               "合并 `D` + `ata` 为 `Data`", ("行 %d" % dropcap[0]) if dropcap else "")

    hyphen = len(re.findall(r"[a-z]- [a-z]", body_join(blocks)))
    report.add("5c", "无断词残留", "PASS" if hyphen == 0 else "MINOR",
               "0 处", "%d 处 `informa- tion` 型" % hyphen,
               "行尾连字符跨行时按整词合并")


def body_join(blocks):
    return "\n".join(b[1] for b in blocks)


def std6(report, body, th):
    items = [l for l in body.split("\n") if is_list(l) or l.strip() == "-"]
    residue = [l for l in items if re.search(r"\S\s+(?:[-*+•▪●◦]|\d+[.)])\s+\S", l)]
    empty = [l for l in items if l.strip() in ("-", "- ")]
    ratio = len(residue) / max(1, len(items))
    if residue and ratio > float(th["std6.bullet_residue_ratio_blocker"]):
        report.add("6a", "无同一行重复列表标记", "BLOCKER", "重复标记项 = 0",
                   "%d/%d 项（%.1f%%）" % (len(residue), len(items), ratio * 100),
                   "扫描列表时遇下一个符号标记即断项")
    elif residue:
        report.add("6a", "无同一行重复列表标记", "MAJOR", "重复标记项 = 0",
                   "%d/%d 项" % (len(residue), len(items)),
                   "少量残留多来自图内文字，可在该行人工确认")
    else:
        report.add("6a", "无同一行重复列表标记", "PASS" if items else "UNVERIFIED", "0 项",
                   "%d 个列表行未发现重复标记；标记已丢失的合并不可判" % len(items), "")
    report.add("6b", "无空列表项", ("PASS" if items else "UNVERIFIED") if not empty else "MINOR",
               "0 个", "%d 个" % len(empty), "空项不输出")


def std7(report, blocks, heads, th):
    # Empty leaf headings remain testable even in a short excerpt.
    empty = 0
    for i, (ln, text) in enumerate(blocks):
        if not is_heading(text):
            continue
        level = len(text) - len(text.lstrip("#"))
        if i + 1 == len(blocks):
            empty += 1
        elif is_heading(blocks[i + 1][1]):
            next_level = len(blocks[i + 1][1]) - len(blocks[i + 1][1].lstrip("#"))
            empty += next_level <= level
    empty_ratio = empty / max(1, len(heads))
    report.add("7c", "无空叶节",
               ("PASS" if heads else "UNVERIFIED") if empty == 0 else
               "BLOCKER" if empty_ratio > float(th["std7.max_empty_ratio"]) else "MINOR",
               "空叶节占比不超过配置上限", "%d 个（%.1f%%）" % (empty, empty_ratio * 100),
               "对照源文恢复缺失内容；不要用摘要掩盖丢失")
    if len(heads) < int(th["sample.min_headings"]):
        for sid in ("7a", "7b"):
            report.add(sid, "节长度分布", "UNVERIFIED", "达到标题样本下限",
                       "仅 %d 个标题" % len(heads), "对全文重跑")
        return
    if not heads:
        report.add("7a", "可切块", "UNVERIFIED", "有标题", "无标题", "先修结构")
        return
    levels = Counter(h["level"] for h in heads if h["level"] > 1)
    if not levels:
        report.add("7a", "可切块", "UNVERIFIED", "有二级及以下标题", "只有一级标题",
                   "整章无子标题，切块只能按段落")
        return
    content_level = levels.most_common(1)[0][0]
    sections, cur = [], None
    for ln, text in blocks:
        if is_heading(text):
            lvl = len(text) - len(text.lstrip("#"))
            if lvl <= content_level:
                if cur:                 # 0 长度节（章末）不计入分布
                    sections.append(cur)
                cur = 0
            elif cur is not None:
                cur += len(text)
            continue
        if cur is not None:
            cur += len(text)
    if cur:
        sections.append(cur)
    if not sections:
        report.add("7a", "可切块", "UNVERIFIED", "可切出节", "未切出任何节", "")
        return

    long_ratio = sum(1 for s in sections if s > int(th["std7.max_section_chars"])) / len(sections)
    frag_ratio = sum(1 for s in sections if s < int(th["std7.min_section_chars"])) / len(sections)

    long_status = "MAJOR" if long_ratio > float(th["std7.max_long_ratio"]) else "PASS"
    frag_status = "MAJOR" if frag_ratio > float(th["std7.max_fragment_ratio"]) else "PASS"
    report.add("7a", "无超长节", long_status, "> %d 字符的节 ≤ %d%%" % (
        int(th["std7.max_section_chars"]), float(th["std7.max_long_ratio"]) * 100),
        "%.1f%%（%d/%d 节）" % (long_ratio * 100, sum(1 for s in sections if s > int(th["std7.max_section_chars"])), len(sections)),
        "按段落或句群二次切分，块上限 1500 字符")
    report.add("7b", "无碎片节", frag_status, "< %d 字符的节 ≤ %d%%" % (
        int(th["std7.min_section_chars"]), float(th["std7.max_fragment_ratio"]) * 100),
        "%.1f%%（%d/%d 节）" % (frag_ratio * 100, sum(1 for s in sections if s < int(th["std7.min_section_chars"])), len(sections)),
        "并入同父节点的下一节，或与其前后块合并")


def std8(report, heads, th):
    numbered = []
    for h in heads:
        m = HEAD_NUM_RE.match(h["text"])
        if m:
            numbered.append((h["line"], m.group(1)))
    if len(numbered) < 3:
        report.add("8a", "编号可定位", "UNVERIFIED", "标题带编号", "编号标题不足 3 条",
                   "无编号时无法回溯章节结构")
        return
    have = {n for _, n in numbered}
    for h in heads:
        m = CHAPTER_RE.match(h["text"])
        if m:
            have.add(str(int(m.group(1) or m.group(2))))
    bad = []
    for ln, num in numbered:
        parts = num.split(".")
        for k in range(1, len(parts)):
            parent = ".".join(parts[:k])
            if parent not in have:
                bad.append((ln + 1, num, parent))
                break
    if bad:
        report.add("8a", "编号可定位", "MAJOR", "每个 N.M.K 都有父级 N.M",
                   "%d 处缺父级，如 %s 缺 %s" % (len(bad), bad[0][1], bad[0][2]),
                   "检查标题吸附是否错位，或该节父标题被误删", "行 %d" % bad[0][0])
    else:
        report.add("8a", "编号可定位", "PASS", "父级齐全", "%d 条编号标题父级完整" % len(numbered), "")


def std_chapters(report, body, chdir):
    """非空分章须是全书连续子串；不能证明章节集合完整或章尾未截断。"""
    files = [f for f in sorted(Path(chdir).glob("*.md")) if f.name.upper() != "INDEX.MD"]
    if not files:
        report.add("1d", "分章守恒", "UNVERIFIED", "有分章文件", "目录为空", "确认 --chapters-dir 路径")
        return
    flat = re.sub(r"\s+", " ", body)
    broken = []
    for f in files:
        _, cbody = split_front_matter(f.read_text(encoding="utf-8"))
        key = re.sub(r"\s+", " ", cbody.strip())
        content = re.sub(r"(?m)^#{1,6}\s+.*$", "", cbody).strip()
        if not key or not content or key not in flat:
            broken.append(f.name)
    if broken:
        report.add("1d", "分章守恒", "BLOCKER",
                   "每章正文都是全书的连续子串",
                   "%d/%d 章不守恒：%s" % (len(broken), len(files), ", ".join(broken[:3])),
                   "分章与全书不同源，或末章被截断；用同一次构建产出两者")
    else:
        report.add("1d", "分章守恒", "PASS", "全部为全书子串",
                   "%d 章全部守恒" % len(files), "")


# ------------------------------------------------------------------ 主流程


def chinese_ratio(text: str) -> float:
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    return cjk / max(1, len(text))


def main() -> int:
    ap = argparse.ArgumentParser(description="转换后自检：Markdown 是否达到 AI 可读标准")
    ap.add_argument("md", help="待审 Markdown")
    ap.add_argument("--baseline", help="probe 产出的 baseline.json（可选）")
    ap.add_argument("--chapters-dir", help="分章目录（可选，用于守恒校验）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", help="把报告写到文件")
    args = ap.parse_args()

    path = Path(args.md)
    if args.out and Path(args.out).resolve() in (
            path.resolve(), Path(args.baseline).resolve() if args.baseline else path.resolve(), THRESH_FILE.resolve()):
        print("报告输出路径不得覆盖输入或配置", file=sys.stderr)
        return EXIT_INPUT
    if not path.is_file():
        print("文件不存在: %s" % path, file=sys.stderr)
        return EXIT_INPUT
    try:
        th = load_thresholds()
    except (OSError, ValueError) as exc:
        print("thresholds.json 无法读取: %s" % exc, file=sys.stderr)
        return EXIT_INPUT
    if args.chapters_dir and not any(
            f.name.upper() != "INDEX.MD" for f in Path(args.chapters_dir).glob("*.md")):
        print("分章目录不存在或没有 Markdown 章节", file=sys.stderr)
        return EXIT_INPUT

    # baseline 规模门禁：换书后误用旧参数包，规模会明显对不上
    if args.baseline:
        bpath = Path(args.baseline)
        if not bpath.exists():
            print("baseline 不存在: %s" % bpath, file=sys.stderr)
            return EXIT_INPUT
        try:
            base = json.loads(bpath.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("baseline 不是合法 JSON", file=sys.stderr)
            return EXIT_INPUT
        prof = base.get("profile", {})
        expect = 0
        if base.get("kind") == "pdf":
            page = prof.get("page", {})
            expect = int(page.get("text_chars_per_page", 0)) * int(page.get("count", 0))
        else:
            expect = int(prof.get("spine", {}).get("chars_total", 0))
        actual = len(path.read_text(encoding="utf-8"))
        if expect and abs(actual - expect) / expect > float(th["baseline.scale_tolerance"]):
            print("baseline 与产物规模不符：期望约 %d 字符，实际 %d 字符（偏差 %.0f%%）。"
                  "疑似换了书却复用旧 baseline，请重跑 probe。"
                  % (expect, actual, abs(actual - expect) / expect * 100), file=sys.stderr)
            return EXIT_INPUT

    raw = path.read_text(encoding="utf-8")
    meta, body = split_front_matter(raw)
    analysis = strip_fences(body)          # 结构判定只看围栏外
    blocks = parse_blocks(analysis)
    heads = headings_of(analysis)

    report = Report()
    std1(report, heads, th)
    std2(report, analysis, blocks, th)
    std3(report, analysis, th)
    std4(report, blocks, th)
    std5(report, blocks, th)
    std6(report, analysis, th)
    std7(report, blocks, heads, th)
    std8(report, heads, th)
    if args.chapters_dir:
        std_chapters(report, body, args.chapters_dir)

    # 抽样组：永远 UNVERIFIED，只提示做法
    for sid, title, how in (
        ("9", "块可自足", "抽 10 个块看首句是否依赖上文代词"),
        ("10", "语言观感", "扫 U+FFFD、全角拉丁字母、单词粘连"),
        ("11", "内容保真", "抽 2 段与源文件逐字对照"),
    ):
        report.add(sid, title, "UNVERIFIED", how, "需人工/模型抽样", "按 standards.md 的抽样清单执行")

    # 编码体检：并入标准 10 的可算部分，不额外占一个标准名额
    if "\ufffd" in body:
        report.add("10a", "无替换字符", "UNVERIFIED", "0 个 U+FFFD",
                   "%d 个（编码或字体映射问题）" % body.count("\ufffd"),
                   "用 UTF-8 重新导出源文件")

    coverage = report.judged / 11
    blockers = report.blockers()
    insufficient = coverage < float(th["coverage.min_ratio"])

    lines = []
    lines.append("# Markdown 入库审计报告")
    lines.append("")
    lines.append("文件：`%s`（%d 字符，%d 标题，%d 块）" % (path.name, len(body), len(heads), len(blocks)))
    lines.append("可判覆盖率：**%d/11**（UNVERIFIED：%s）" % (
        report.judged, ",".join(report.unverified) if report.unverified else "无"))
    lines.append("")
    for label, statuses in (("Blocker", ("BLOCKER",)), ("Major", ("MAJOR",)), ("Minor", ("MINOR",)),
                            ("通过", ("PASS",)), ("未判", ("UNVERIFIED",))):
        rows = [i for i in report.items if i["status"] in statuses]
        if not rows:
            continue
        lines.append("## %s（%d）" % (label, len(rows)))
        for i in rows:
            mark = {"BLOCKER": "✗", "MAJOR": "!", "MINOR": "·", "PASS": "✓", "UNVERIFIED": "?"}[i["status"]]
            lines.append("- %s [%s] %s：期望 %s；实际 %s%s" % (
                mark, i["id"], i["title"], i["expect"], i["actual"],
                ("（%s）" % i["evidence"]) if i["evidence"] else ""))
            if i["status"] in ("BLOCKER", "MAJOR") and i["fix"]:
                lines.append("  - 修法：%s" % i["fix"])
        lines.append("")

    lines.append("## 下一步")
    if blockers:
        lines.append("- 有 %d 个 blocker，按上面顺序修；修完重跑本脚本" % len(blockers))
    elif insufficient:
        lines.append("- 无 blocker，但可判覆盖率 %.0f%% 低于下限，**不得视为通过**；人工复核缺证据项。baseline 只做规模门禁，不提高覆盖率" % (coverage * 100))
    else:
        lines.append("- 无 blocker；major 项请在 front matter 的 `known_limits` 逐条声明后入库")
    text = "\n".join(lines)

    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    if args.json:
        print(json.dumps({"coverage": round(coverage, 3), "line_basis": "body-relative, 1-based",
                          "limits": ["No source completeness proof", "Merged lists with lost markers are unverifiable"],
                          "items": report.items},
                         ensure_ascii=False, indent=2))
    else:
        print(text)

    if blockers:
        return EXIT_BLOCKER
    if insufficient:
        return EXIT_INSUFFICIENT
    return EXIT_OK


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print("输入/配置无法处理: %s" % exc, file=sys.stderr)
        sys.exit(EXIT_INPUT)
