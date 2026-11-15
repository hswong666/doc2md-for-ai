"""Shared caption grammar; figure/table identity includes type and full number."""
import re

BIB_RE = re.compile(
    r"^(?:(?:\d+(?:\.\d+)*\.?)\s+)?"
    r"(References|Further Reading|Works Cited|Bibliography|参考文献|延伸阅读)\s*[:：]?$", re.I
)
NUMBER = r"\d+(?:[.\-\u2013]\d+)*"
REFERENCE = re.compile(
    r"(?P<label>Figure|Table|图|表)\s*(?P<num>" + NUMBER + r")(?!\d)", re.I
)
CAPTION = re.compile(
    r"^(?P<label>Figure|Table|图|表)\s*(?P<num>" + NUMBER
    + r")\s*[.:：]?\s+(?P<title>\S.{0,89})$", re.I
)


def identity(label, number):
    kind = "figure" if label.lower() in ("figure", "图") else "table"
    return kind, re.sub(r"[-\u2013]", ".", number)


def match_caption(text):
    text = (text or "").strip()
    text = re.sub(r"^>\s*", "", text)
    match = CAPTION.fullmatch(text)
    if not match or len(text) > 110 or text.endswith((".", "。", "!", "！", "?", "？")):
        return None
    return match["label"], match["num"], match["title"].strip()
