"""Read sources, extract text, split into chunks that remember where they came from.

Project directories are only ever read. Python is parsed with ast (never imported or run).
"""
from __future__ import annotations

import ast
import hashlib
import logging
import os
import warnings
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterator

import settings

logging.getLogger("pypdf").setLevel(logging.ERROR)


@dataclass
class Chunk:
    id: str
    kind: str                 # "local" (papers, notes) or "code" (project directories)
    group: str                # selection unit: a doc file path, or a project name
    source: str               # display path
    text: str
    page: int | None = None
    section: str | None = None
    line_start: int | None = None
    line_end: int | None = None

    def label(self) -> str:
        if self.kind == "code" and self.section in ("data files", "project map"):
            return self.source
        if self.kind == "code" and (self.section or "").startswith("knowledge: "):
            return f"{self.source} ({self.section[len('knowledge: '):]})"
        if self.kind == "code" and self.section == "file card":
            return f"{self.source} (file card)"
        if self.kind == "code":
            return f"{self.source}:{self.line_start}-{self.line_end}"
        if self.page:
            return f"{self.source}, p.{self.page}"
        if self.section:
            return f"{self.source}, {self.section}"
        return self.source

    def to_dict(self) -> dict:
        return asdict(self)


def _cid(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


# ---------------------------------------------------------------- walking

def walk_files(root: Path, cfg: dict) -> Iterator[Path]:
    """Every non-hidden, non-link file under root, never entering an excluded directory."""
    if settings.in_hard_excluded(root):
        raise ValueError(f"Source {root} is inside an excluded directory")
    skip = settings.exclude_dirs(cfg)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            d for d in dirnames
            if not settings.in_hard_excluded(Path(d))
            and d.lower() not in skip
            and not d.startswith(".")
            and not os.path.islink(os.path.join(dirpath, d))
        )
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if name.startswith(".") or p.is_symlink() or settings.in_hard_excluded(p):
                continue
            yield p


def iter_files(root: Path, exts: set[str], cfg: dict, max_bytes: int | None = None) -> Iterator[Path]:
    """Yield the files under root that may be read: wanted type, not a secret, not an image or imaging file, not too big."""
    max_bytes = int(max_bytes or cfg.get("max_file_bytes", 400000))
    for p in walk_files(root, cfg):
        if p.suffix.lower() not in exts or settings.never_read(p) or settings.SECRET_FILE_RE.search(p.name):
            continue
        try:
            if p.stat().st_size > max_bytes:
                continue
        except OSError:
            continue
        yield p


# ---------------------------------------------------------------- text splitting

def split_text(text: str, size: int, overlap: int) -> list[str]:
    """Pack paragraphs up to size; hard-split oversized paragraphs with overlap."""
    paras = [p.strip() for p in text.replace("\r", "").split("\n\n") if p.strip()]
    out, cur = [], ""
    for p in paras:
        if len(p) > size:
            if cur:
                out.append(cur); cur = ""
            step = max(size - overlap, 1)
            out.extend(p[i:i + size] for i in range(0, len(p), step))
            continue
        if cur and len(cur) + len(p) + 2 > size:
            out.append(cur); cur = p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        out.append(cur)
    return out


def line_windows(lines: list[str], offset: int, win: int = 60, overlap: int = 10, max_chars: int = 4000):
    i = 0
    while i < len(lines):
        j = min(i + win, len(lines))
        block = lines[i:j]
        while sum(len(x) + 1 for x in block) > max_chars and len(block) > 5:
            block = block[: len(block) // 2]
            j = i + len(block)
        text = "\n".join(block)
        if text.strip():
            yield text, offset + i + 1, offset + j
        if j >= len(lines):
            break
        i = max(j - overlap, i + 1)


# ---------------------------------------------------------------- readers

_PDF_CACHE: dict = {}


def read_pdf(path: Path, source: str, group: str, cfg: dict) -> list[Chunk]:
    """Chunks of a PDF; an unchanged file (same size and modified time) is not read again."""
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size, source, group, cfg["retrieval"]["chunk_chars"], cfg["retrieval"]["overlap_chars"])
    except OSError:
        return _read_pdf(path, source, group, cfg)
    if key not in _PDF_CACHE:
        _PDF_CACHE[key] = _read_pdf(path, source, group, cfg)
    return list(_PDF_CACHE[key])


def _read_pdf(path: Path, source: str, group: str, cfg: dict) -> list[Chunk]:
    from pypdf import PdfReader

    size = cfg["retrieval"]["chunk_chars"]; ov = cfg["retrieval"]["overlap_chars"]
    chunks: list[Chunk] = []
    reader = PdfReader(str(path))
    for n, page in enumerate(reader.pages, start=1):
        try:
            txt = page.extract_text() or ""
        except Exception:
            txt = ""
        for part in split_text(settings.redact(txt), size, ov):
            chunks.append(Chunk(_cid(source, n, part[:60]), "local", group, source, part, page=n))
    return chunks


def read_markdown(path: Path, source: str, group: str, kind: str, cfg: dict) -> list[Chunk]:
    size = cfg["retrieval"]["chunk_chars"]; ov = cfg["retrieval"]["overlap_chars"]
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    sections: list[tuple[str, int, list[str]]] = []
    title, start, buf = path.name, 1, []
    for no, ln in enumerate(lines, start=1):
        if ln.lstrip().startswith("#") and ln.lstrip("# ").strip():
            if buf:
                sections.append((title, start, buf))
            title, start, buf = ln.lstrip("# ").strip(), no, [ln]
        else:
            buf.append(ln)
    if buf:
        sections.append((title, start, buf))
    out: list[Chunk] = []
    for title, s, body in sections:
        text = settings.redact("\n".join(body))
        pos = s
        for part in split_text(text, size, ov):
            n_lines = part.count("\n") + 1
            out.append(Chunk(_cid(source, pos, part[:60]), kind, group, source, part, section=title,
                             line_start=pos, line_end=pos + n_lines - 1))
            pos += n_lines
    return out


def read_python(path: Path, source: str, group: str, cfg: dict) -> list[Chunk]:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")   # old escape sequences in user code are not our concern
            tree = ast.parse(text)  # parse only; the code is never imported or executed
    except (SyntaxError, ValueError):
        return read_lines(path, source, group, cfg, text=text)
    max_chars = 4000
    spans: list[tuple[int, int]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            spans.append((start, node.end_lineno or node.lineno))
    chunks: list[Chunk] = []

    def add(a: int, b: int):
        block = lines[a - 1:b]
        body = "\n".join(block)
        if not body.strip():
            return
        if len(body) > max_chars:
            for t, ls, le in line_windows(block, a - 1, max_chars=max_chars):
                chunks.append(Chunk(_cid(source, ls, le), "code", group, source, settings.redact(t),
                                    line_start=ls, line_end=le))
        else:
            chunks.append(Chunk(_cid(source, a, b), "code", group, source, settings.redact(body),
                                line_start=a, line_end=b))

    cursor = 1
    for a, b in spans:
        if a > cursor:
            add(cursor, a - 1)
        add(a, b)
        cursor = b + 1
    if cursor <= len(lines):
        add(cursor, len(lines))
    return chunks


def read_lines(path: Path, source: str, group: str, cfg: dict, text: str | None = None) -> list[Chunk]:
    text = text if text is not None else path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    return [
        Chunk(_cid(source, ls, le), "code", group, source, settings.redact(t), line_start=ls, line_end=le)
        for t, ls, le in line_windows(lines, 0)
    ]


# ---------------------------------------------------------------- orchestration

def ingest_all(cfg: dict, log=print, extra: dict | None = None) -> list[Chunk]:
    """Read every configured source. `extra`, if given, receives the code map data under extra["codemap"]."""
    chunks: list[Chunk] = []

    for src in cfg.get("docs", []):
        base = settings.resolve(src["path"])
        if not base.exists():
            log(f"skip (missing): {base}")
            continue
        exts = {e.lower() for e in src["exts"]}
        for p in iter_files(base, exts, cfg, max_bytes=int(cfg.get("max_doc_bytes", 60_000_000))):
            rel = f"{base.name}/{p.relative_to(base)}"
            try:
                if p.suffix.lower() == ".pdf":
                    new = read_pdf(p, rel, rel, cfg)
                    if not new:
                        log(f"warning: no extractable text in {rel} (scanned PDF?)")
                else:
                    new = read_markdown(p, rel, rel, "local", cfg) if p.suffix.lower() == ".md" else [
                        Chunk(_cid(rel, i, t[:60]), "local", rel, rel, t)
                        for i, t in enumerate(split_text(settings.redact(p.read_text(errors="replace")),
                                                         cfg["retrieval"]["chunk_chars"],
                                                         cfg["retrieval"]["overlap_chars"]))
                    ]
                chunks.extend(new)
            except Exception as e:  # one bad file must not stop indexing
                log(f"warning: could not read {rel}: {e}")

    exts = {e.lower() for e in cfg["project_exts"]}
    for proj in cfg.get("projects", []):
        base = settings.resolve(proj["path"])
        if not base.exists():
            log(f"skip (missing): {base}")
            continue
        name = proj["name"]
        py_texts: dict[str, str] = {}
        others: list[str] = []
        for p in iter_files(base, exts, cfg):
            rel = f"{name}/{p.relative_to(base)}"
            try:
                suf = p.suffix.lower()
                if suf == ".py":
                    py_texts[p.relative_to(base).as_posix()] = p.read_text(encoding="utf-8", errors="replace")
                    chunks.extend(read_python(p, rel, name, cfg))
                elif suf == ".md":
                    others.append(p.relative_to(base).as_posix())
                    chunks.extend(read_markdown(p, rel, name, "code", cfg))
                else:
                    others.append(p.relative_to(base).as_posix())
                    chunks.extend(read_lines(p, rel, name, cfg))
            except Exception as e:
                log(f"warning: could not read {rel}: {e}")
        try:
            from codemap import build_project_map
            map_chunks, data = build_project_map(name, py_texts, others)
            chunks.extend(map_chunks)
            if extra is not None:
                extra.setdefault("codemap", []).append(data)
            if proj.get("nifti_headers"):
                from projectctx import data_files_summary
                summary = data_files_summary(base, cfg)
                if summary:
                    chunks.append(Chunk(_cid(name, "data files"), "code", name, f"{name}/(data files)", summary,
                                        section="data files"))
        except Exception as e:      # the map is a help, never a reason to fail indexing
            log(f"warning: could not build the project map for {name}: {e}")

    # de-duplicate ids deterministically
    seen: set[str] = set()
    for c in chunks:
        while c.id in seen:
            c.id = _cid(c.id, "dup")
        seen.add(c.id)
    return chunks
