"""Turn a model's raw text into verified citation segments.

Local and code tags are built here from retrieval metadata. The model only emits ids such as [S2];
an id that was not retrieved is flagged, so a made-up source cannot appear as a real one.
Web tags come from the provider's citation data. [M] marks the model's own knowledge.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ingest import Chunk
from providers.base import WebCite

MARK_RE = re.compile(r"\[((?:S\d+|M)(?:\s*[,;]\s*(?:S\d+|M))*)\]|⟦W(\d+)⟧")


@dataclass
class Segment:
    kind: str                 # "text" | "local" | "code" | "web" | "model" | "unverified"
    text: str = ""            # plain text, or the tag label
    ref: str | None = None    # chunk id or url
    detail: str | None = None  # for explore citations: the cited lines, kept so a saved chat can still show them

    def plain(self) -> str:
        if self.kind == "text":
            return self.text
        return f"[{self.kind}: {self.text}]" if self.kind != "model" else "[model knowledge]"


def render(text: str, web: list[WebCite], chunks: dict[str, Chunk]) -> list[Segment]:
    """chunks maps the ids shown to the model (S1, S2...) to retrieved chunks."""
    # insert web placeholders from the end so earlier offsets stay valid
    by_pos: dict[int, list[int]] = {}
    for n, w in enumerate(web):
        by_pos.setdefault(min(max(w.end, 0), len(text)), []).append(n)
    for pos in sorted(by_pos, reverse=True):
        text = text[:pos] + "".join(f"⟦W{n}⟧" for n in by_pos[pos]) + text[pos:]

    segs: list[Segment] = []
    last = 0
    for m in MARK_RE.finditer(text):
        if m.start() > last:
            segs.append(Segment("text", text[last:m.start()]))
        last = m.end()
        if m.group(2) is not None:
            w = web[int(m.group(2))]
            segs.append(Segment("web", w.url, w.url))
            continue
        for tok in re.split(r"\s*[,;]\s*", m.group(1)):
            if tok == "M":
                segs.append(Segment("model", "model knowledge"))
            elif tok in chunks:
                c = chunks[tok]
                segs.append(Segment(c.kind, c.label(), c.id))
            else:
                segs.append(Segment("unverified", f"{tok} was not a retrieved source", tok))
    if last < len(text):
        segs.append(Segment("text", text[last:]))
    return _merge(segs)


def _merge(segs: list[Segment]) -> list[Segment]:
    out: list[Segment] = []
    for s in segs:
        if s.kind == "text" and out and out[-1].kind == "text":
            out[-1].text += s.text
        elif s.kind != "text" and out and out[-1].kind == s.kind and out[-1].ref == s.ref and s.kind in ("web", "model"):
            continue  # drop immediate duplicate tags
        else:
            out.append(Segment(s.kind, s.text, s.ref, s.detail))
    return out


EXPLORE_RE = re.compile(r"\[([A-Za-z0-9_.\-/() ]+?\.[A-Za-z0-9]+):(\d+)(?:\s*-\s*(\d+))?\]")
DETAIL_LINES, DETAIL_CHARS = 60, 6000


def check_explore_cite(root: Path, path: str, a: int, b: int) -> str | None:
    """The cited lines if the file is in the explored copy and the range is real, otherwise None."""
    if path.startswith("/") or ".." in Path(path).parts:
        return None
    try:
        f = (root / path).resolve()
        if root.resolve() not in f.parents or not f.is_file():
            return None
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    if not 1 <= a <= b <= len(lines):
        return None
    return "\n".join(lines[a - 1:min(b, a - 1 + DETAIL_LINES)])[:DETAIL_CHARS]


def render_explore(text: str, root: Path, chunks: dict[str, Chunk]) -> list[Segment]:
    """Like render, plus [path:a-b] citations written by explore mode. Each one is checked against the explored copy:
    a file that is not there, or lines past its end, are flagged instead of shown as a real source."""
    segs: list[Segment] = []
    last = 0
    for m in EXPLORE_RE.finditer(text):
        segs += render(text[last:m.start()], [], chunks)
        last = m.end()
        path, a = m.group(1).strip(), int(m.group(2))
        b = int(m.group(3) or a)
        label = f"{path}:{a}-{b}"
        detail = check_explore_cite(root, path, a, b)
        if detail is None:
            segs.append(Segment("unverified", f"{label} is not in the explored copy", label))
        else:
            segs.append(Segment("code", label, f"explore:{label}", detail))
    segs += render(text[last:], [], chunks)
    return _merge(segs)


def to_plain(segs: list[Segment]) -> str:
    return "".join(s.plain() if s.kind != "text" else s.text for s in segs)
