"""Keeps text that a model wrote (or a document supplied) from making the browser fetch anything by itself."""
from __future__ import annotations

import re

_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)|!\[([^\]]*)\]\[[^\]]*\]|<img\b[^>]*>", re.I)


def strip_images(text: str) -> str:
    """Replace markdown and HTML images with their alt text. An image URL is fetched by the browser without a click,
    so injected text could use one to carry your data to another site."""
    return _IMAGE.sub(lambda m: f"[image removed: {(m.group(1) or m.group(2) or '').strip()}]" if (m.group(1) or m.group(2)) else "[image removed]", text)


# ---------------------------------------------------------------- formulas
# The page draws LaTeX written between $...$ (in a line) or $$...$$ (on its own line). Models also write \( ... \) and \[ ... \],
# so those are turned into the dollar form, and a dollar sign used as money ("$120k") is escaped so it is not read as the start of a formula.
_CODE = re.compile(r"(```.*?```|`[^`\n]*`)", re.S)
_PAREN = re.compile(r"\\\((.+?)\\\)", re.S)
_BRACKET = re.compile(r"\\\[(.+?)\\\]", re.S)
_MONEY = re.compile(r"(?<![\\$\w])\$(?=\d[\d,]*(?:\.\d+)?[kKmMbB]?(?:\s+[A-Za-z]{2,}|[.,;:)!?](?!\d)|\s*$))", re.M)
_MATH = re.compile(r"(?<![\\$])\$\$.+?(?<!\\)\$\$|(?<![\\$])\$(?=[^\s$])[^$\n]+?(?<![\s\\])\$(?!\d)", re.S)
MARK_OPEN, MARK_CLOSE = "", ""
_MARKS = re.compile(f"{MARK_OPEN}\\d+{MARK_CLOSE}")


def _outside_code(text: str, fn) -> str:
    return "".join(part if i % 2 else fn(part) for i, part in enumerate(_CODE.split(text)))


def _prepare(part: str) -> str:
    part = _PAREN.sub(lambda m: f"${m.group(1).strip()}$", part)
    part = _BRACKET.sub(lambda m: f"\n$${m.group(1).strip()}$$\n", part)
    return _MONEY.sub(r"\\$", part)


def _keep_marks_out(part: str) -> str:
    """A citation mark placed inside a formula would cut it in two, so it moves to just after the formula."""
    def fix(m):
        found = _MARKS.findall(m.group(0))
        return _MARKS.sub("", m.group(0)) + "".join(found) if found else m.group(0)
    return _MATH.sub(fix, part)


def math(text: str) -> str:
    """Model text prepared for display so that formulas are drawn as LaTeX."""
    return _outside_code(text, _prepare)


def math_with_marks(text: str) -> str:
    """The same, for text that holds citation-mark placeholders (MARK_OPEN, number, MARK_CLOSE) that must stay outside formulas."""
    return _outside_code(text, lambda p: _keep_marks_out(_prepare(p)))
