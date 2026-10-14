"""Project knowledge: Claude reads a whole project once and writes a lasting document about it.

The document (knowledge/<project>.md, private and never committed) covers the project's purpose,
architecture, every file, the results with their numbers, the design decisions and the caveats. Later questions
use the parts of it that match the question, together with the best-matching real code, so Claude does not have
to be given the whole project again. Every file and line the document cites is checked against the real code.
A fingerprint of each file is kept, so the app can say which files changed since the document was written and
update only those.
"""
from __future__ import annotations

import functools
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import settings
from ingest import Chunk, _cid
from projectctx import ProjectFile, collect, estimate_tokens

KNOWLEDGE_PREFIX = "knowledge: "
CITE_RE = re.compile(r"\[([A-Za-z0-9_.\-/() ]+?\.[A-Za-z0-9]+):(\d+)(?:\s*-\s*(\d+))?\]")

BUILD_SYSTEM = """You are building a lasting knowledge base about one software or research project, so that a later assistant who \
will NOT see all the code again can answer detailed technical questions about it. Read every file provided, then write one Markdown \
document with exactly these sections, in this order:

# Project knowledge: <project name>
## Overview
What the project is for, who it is for, its main parts and its current state.
## Architecture and data flow
How the parts fit together and how data moves from input to output, naming the files involved.
## Files
One "### <path>" subsection per file: its purpose, its key classes and functions with their signatures, important constants and \
settings, what it reads and writes, and how it connects to other files. Group trivial files together.
## Experiments, results and numbers
Every variant, run or result the files report, with the exact numbers, what they measure and where they come from. Use tables.
## Design decisions and rationale
Why things were built the way they were, as far as the files say.
## Caveats, limitations and open questions
What the authors flag, what is unfinished, and anything unclear or inconsistent in the files.
## Glossary
What the project's own names and abbreviations mean.

Rules:
- Be specific and factual. Use the exact names, numbers, hyperparameters and file names as they appear. Never invent anything; if the \
files do not say, write "not stated".
- Cite the code you rely on as [path:start-end], with the path relative to the project root exactly as shown in the file header and \
line numbers you saw in the numbered lines. Never cite lines you did not see.
- Do not repeat secrets or credentials. Be thorough but compact: no filler, no repetition."""

UPDATE_SYSTEM = """You maintain a lasting knowledge document about a project. You are given the current document, the files that \
changed or were added since it was written (full text with numbered lines), and the list of files that were removed. Return the \
COMPLETE updated document in the same structure and style, changing only what the new files affect. Update the citations for the \
changed files to the new line numbers, drop anything about removed files, and keep everything else as it is. Cite as \
[path:start-end] using line numbers you saw. Never invent anything; do not repeat secrets. Return only the document."""


@dataclass
class Status:
    built: bool = False
    built_at: str = ""
    model: str = ""
    n_files: int = 0
    changed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    verified: int = 0
    unverified: int = 0

    @property
    def stale(self) -> bool:
        return self.n_changed > 0

    @property
    def n_changed(self) -> int:
        return len(self.changed) + len(self.added) + len(self.removed)


# ---------------------------------------------------------------- storage
def _paths(name: str) -> tuple[Path, Path]:
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+", name):
        raise ValueError(f"not a project name: {name!r}")
    d = settings.KNOWLEDGE_DIR
    return d / f"{name}.md", d / f"{name}.json"


def load(name: str) -> tuple[str, dict] | None:
    md, js = _paths(name)
    try:
        return md.read_text(encoding="utf-8"), json.loads(js.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save(name: str, doc: str, info: dict) -> None:
    md, js = _paths(name)
    settings.assert_writable(md.parent).mkdir(parents=True, exist_ok=True)
    settings.assert_writable(md).write_text(doc, encoding="utf-8")
    settings.assert_writable(js).write_text(json.dumps(info, indent=1), encoding="utf-8")


def delete(name: str) -> None:
    for p in _paths(name):
        settings.assert_writable(p).unlink(missing_ok=True)
    settings.assert_writable(settings.KNOWLEDGE_DIR / f"{name}.deep.json").unlink(missing_ok=True)      # the deep-read notes, if any


@functools.lru_cache(maxsize=4096)
def fingerprint(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def current_files(cfg: dict, name: str) -> dict[str, ProjectFile]:
    return {f.rel: f for f in collect(cfg, [name]).files}


def status(cfg: dict, name: str) -> Status:
    got = load(name)
    if not got:
        return Status()
    _doc, info = got
    old, now = info.get("hashes", {}), current_files(cfg, name)
    return Status(True, info.get("built_at", ""), info.get("model", ""), len(old),
                  sorted(r for r in now if r in old and fingerprint(now[r].text) != old[r]),
                  sorted(r for r in now if r not in old), sorted(r for r in old if r not in now),
                  info.get("verified", 0), info.get("unverified", 0))


# ---------------------------------------------------------------- building
def numbered(text: str) -> str:
    return "\n".join(f"{i:>4}| {ln}" for i, ln in enumerate(text.splitlines(), start=1))


def files_block(files: list[ProjectFile]) -> str:
    return "\n\n".join(f'<file path="{f.rel}">\n{numbered(f.text)}\n</file>' for f in files)


def build_prompt(name: str, files: list[ProjectFile]) -> str:
    return f"PROJECT NAME: {name}\n\nFILES:\n{files_block(files)}\n\nWrite the knowledge document now."


def update_prompt(name: str, doc: str, files: list[ProjectFile], removed: list[str]) -> str:
    gone = "\n".join(f"- {r}" for r in removed) or "(none)"
    return (f"PROJECT NAME: {name}\n\nCURRENT DOCUMENT:\n{doc}\n\nREMOVED FILES:\n{gone}\n\n"
            f"CHANGED OR ADDED FILES:\n{files_block(files) or '(none)'}\n\nReturn the complete updated document now.")


def verify_citations(doc: str, files: dict[str, ProjectFile]) -> tuple[str, int, int]:
    """Mark every [path:a-b] that does not match a real file and line range. Returns (document, verified, unverified)."""
    counts = {"ok": 0, "bad": 0}
    lengths = {rel: f.text.count("\n") + 1 for rel, f in files.items()}

    def check(m: re.Match) -> str:
        path, a = m.group(1).strip(), int(m.group(2))
        b = int(m.group(3) or a)
        if path in lengths and 1 <= a <= b <= lengths[path]:
            counts["ok"] += 1
            return m.group(0)
        counts["bad"] += 1
        return f"[{path}:{a}-{b} (unverified)]"
    return CITE_RE.sub(check, doc), counts["ok"], counts["bad"]


def looks_like_a_document(text: str) -> bool:
    return text.lstrip().startswith("# ") and "## Overview" in text and len(text) > 300


def record(name: str, doc: str, files: dict[str, ProjectFile], model: str, ok: int, bad: int) -> dict:
    return {"project": name, "built_at": time.strftime("%Y-%m-%d %H:%M"), "model": model, "verified": ok, "unverified": bad,
            "hashes": {rel: fingerprint(f.text) for rel, f in files.items()}, "tokens": estimate_tokens(len(doc))}


# ---------------------------------------------------------------- using it
def sections(doc: str) -> list[tuple[str, str]]:
    """(heading, text) pieces: one per ## section, with the Files section split into one piece per file."""
    out: list[tuple[str, str]] = []
    h2 = h3 = None
    buf: list[str] = []

    def flush():
        text = "\n".join(buf).strip()
        if h2 and "\n" in text:                  # a heading with nothing under it is skipped
            out.append((f"{h2} > {h3}" if h3 else h2, text))
    for line in doc.splitlines():
        if line.startswith("## "):
            flush()
            h2, h3, buf = line[3:].strip(), None, [line]
        elif line.startswith("### ") and h2 == "Files":
            flush()
            h3, buf = line[4:].strip(), [line]
        else:
            buf.append(line)
    flush()
    return out


STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "is", "are", "was", "were", "it", "its", "this", "that", "what",
        "how", "does", "do", "did", "with", "for", "from", "by", "as", "at", "be", "can", "which", "why", "me", "my", "you"}


def _words(text: str) -> list[str]:
    from index import code_tokens
    return [w for w in code_tokens(text) if w not in STOP and len(w) > 1]


def select(name: str, question: str, max_chars: int = 20000) -> list[Chunk]:
    """The Overview plus the sections that best match the question, as code chunks (they come from the project's code)."""
    got = load(name)
    if not got:
        return []
    from sklearn.feature_extraction.text import TfidfVectorizer
    secs = sections(got[0])
    if not secs:
        return []
    vec = TfidfVectorizer(analyzer=_words, sublinear_tf=True)
    mat = vec.fit_transform([f"{h}\n{t}" for h, t in secs])
    scores = (mat @ vec.transform([question]).T).toarray().ravel()
    order = sorted(range(len(secs)), key=lambda i: -scores[i])
    chosen, used = [], 0
    for i in [next((j for j, (h, _) in enumerate(secs) if h == "Overview"), None)] + order:
        if i is None or i in chosen or (scores[i] <= 0 and secs[i][0] != "Overview"):
            continue
        if used + len(secs[i][1]) > max_chars and chosen:
            continue
        chosen.append(i)
        used += len(secs[i][1])
    return [Chunk(_cid(name, "knowledge", secs[i][0]), "code", name, f"{name}/(project knowledge)", secs[i][1],
                  section=KNOWLEDGE_PREFIX + secs[i][0]) for i in chosen]
