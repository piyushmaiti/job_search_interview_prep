"""Project maps for code questions.

For each project this builds, from the Python syntax tree only (nothing is imported or run):
  - one overview chunk: the layout, entry points and the most imported files,
  - one card per Python file: what it defines, what it imports, and who calls what,
  - a small symbol table used to pull in the code a retrieved function calls.

The maps are ordinary code chunks, so the consent prompt and citations treat them like any other project code.
Call and import links are matched by name, so they are approximate and say so.
"""
from __future__ import annotations

import ast
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath

import settings
from ingest import Chunk, _cid

MAP_SECTIONS = ("project map", "file card")
_META_LINE = re.compile(r"^(author|authors|last modified|modified|created|date|copyright|version|license|updated|file|usage|@\w+)\b", re.I)
OVERVIEW_CHARS = 6000
CARD_CHARS = 3500
MAX_DEFS_PER_NAME = 3        # a name defined in more places than this is too generic to link
SKIP_NAMES = {"__init__", "__call__", "__repr__", "__str__", "forward", "main", "run", "get", "set", "update", "add"}


@dataclass
class Func:
    qual: str                # "VAE.encode" or "vae_loss"
    kind: str                # "class" | "function"
    line: int
    end: int
    sig: str
    doc: str
    calls: list[str] = field(default_factory=list)


@dataclass
class PyFile:
    path: str                # path inside the project, with forward slashes
    doc: str = ""
    imports: list[str] = field(default_factory=list)       # dotted module names as written
    funcs: list[Func] = field(default_factory=list)
    is_entry: bool = False
    n_lines: int = 0


def _first_line(text: str | None) -> str:
    return (text or "").strip().splitlines()[0].strip() if (text or "").strip() else ""


def _purpose(doc: str | None, path: str) -> str:
    """First useful line of a module docstring (many start with just the file name)."""
    name = PurePosixPath(path).name
    for line in (doc or "").strip().splitlines():
        line = line.strip()
        for sep in (" - ", " — ", ": "):
            if line.startswith(name + sep):
                line = line[len(name) + len(sep):].strip()
        if line and line not in (name, PurePosixPath(name).stem) and not _META_LINE.match(line) and not re.fullmatch(r"\S+\.py", line):
            return line
    return ""


def _call_name(node: ast.Call) -> str | None:
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _calls(node: ast.AST) -> list[str]:
    seen: dict[str, None] = {}
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            name = _call_name(n)
            if name and name not in seen:
                seen[name] = None
    return list(seen)


def _sig(node) -> str:
    try:
        return f"({ast.unparse(node.args)})"
    except Exception:
        return "(...)"


def parse_python(path: str, text: str) -> PyFile | None:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    info = PyFile(path=path, doc=_purpose(ast.get_docstring(tree), path), n_lines=text.count("\n") + 1)
    for node in tree.body:
        if isinstance(node, ast.Import):
            info.imports += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = ("." * node.level) + (node.module or "")
            info.imports.append(base)
            sep = "" if base.endswith(".") else "."
            info.imports += [f"{base}{sep}{a.name}" for a in node.names if a.name != "*"]
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            info.funcs.append(Func(node.name, "function", node.lineno, node.end_lineno or node.lineno,
                                   _sig(node), _first_line(ast.get_docstring(node)), _calls(node)))
        elif isinstance(node, ast.ClassDef):
            bases = ", ".join(ast.unparse(b) for b in node.bases) if node.bases else ""
            info.funcs.append(Func(node.name, "class", node.lineno, node.end_lineno or node.lineno,
                                   f"({bases})" if bases else "", _first_line(ast.get_docstring(node)), []))
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    info.funcs.append(Func(f"{node.name}.{sub.name}", "function", sub.lineno, sub.end_lineno or sub.lineno,
                                           _sig(sub), _first_line(ast.get_docstring(sub)), _calls(sub)))
        elif isinstance(node, ast.If):
            test = ast.unparse(node.test)
            if "__name__" in test and "__main__" in test:
                info.is_entry = True
    return info


# ---------------------------------------------------------------- linking

def _dotted(path: str) -> str:
    p = PurePosixPath(path).with_suffix("")
    parts = list(p.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def link_imports(files: dict[str, PyFile]) -> dict[str, set[str]]:
    """For each file, the project files it imports (matched by module name, so approximate)."""
    dotted = {_dotted(p): p for p in files}
    out: dict[str, set[str]] = {p: set() for p in files}
    for path, f in files.items():
        pkg = _dotted(path).split(".")[:-1] if not path.endswith("__init__.py") else _dotted(path).split(".")
        for raw in f.imports:
            level = len(raw) - len(raw.lstrip("."))
            name = raw.lstrip(".")
            if level:
                up = pkg[: len(pkg) - (level - 1)] if level - 1 <= len(pkg) else []
                name = ".".join(up + ([name] if name else []))
            if not name:
                continue
            for d, target in dotted.items():
                if target != path and (d == name or d.endswith("." + name)):
                    out[path].add(target)
    return out


def link_calls(files: dict[str, PyFile]):
    """defs: simple name -> [(file, qual, line)]; calls_to and called_by link functions through names that are defined once or twice."""
    defs: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
    for path, f in files.items():
        for fn in f.funcs:
            defs[fn.qual.split(".")[-1]].append((path, fn.qual, fn.line))
    calls_to: dict[tuple[str, str], list[tuple[str, str, int]]] = {}
    called_by: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for path, f in files.items():
        for fn in f.funcs:
            targets = []
            for name in fn.calls:
                if name in SKIP_NAMES:
                    continue
                cands = [d for d in defs.get(name, []) if (d[0], d[1]) != (path, fn.qual)]
                if 1 <= len(cands) <= MAX_DEFS_PER_NAME:
                    targets += cands
            calls_to[(path, fn.qual)] = targets
            for t in targets:
                called_by[(t[0], t[1])].append((path, fn.qual))
    return defs, calls_to, called_by


# ---------------------------------------------------------------- text

def _trim(text: str, cap: int) -> str:
    return text if len(text) <= cap else text[: cap - 40].rstrip() + "\n... (cut to keep the map short)"


def file_card(project: str, f: PyFile, imports_of: set[str], imported_by: set[str], calls_to, called_by) -> str:
    out = [f"FILE CARD: {project}/{f.path}  ({f.n_lines} lines)"]
    out.append(f"Purpose: {f.doc or '(no module docstring)'}")
    if imports_of:
        out.append("Imports from this project: " + ", ".join(sorted(imports_of)[:12]))
    if imported_by:
        out.append("Imported by: " + ", ".join(sorted(imported_by)[:12]))
    out.append("Defines:")
    for fn in f.funcs:
        indent = "    " if "." in fn.qual else "  "
        kind = "class" if fn.kind == "class" else "def"
        name = fn.qual.split(".")[-1]
        doc = f" - {fn.doc[:80]}" if fn.doc else ""
        out.append(f"{indent}{kind} {name}{fn.sig[:90]}  L{fn.line}-{fn.end}{doc}")
    rel = []
    for fn in f.funcs:
        for t in calls_to.get((f.path, fn.qual), [])[:4]:
            rel.append(f"  {fn.qual} calls {t[1]} [{t[0]}:{t[2]}]")
    if rel:
        out.append("Calls into other definitions (matched by name, approximate):")
        out += rel[:20]
    back = []
    for fn in f.funcs:
        for c in called_by.get((f.path, fn.qual), [])[:3]:
            back.append(f"  {fn.qual} is called by {c[1]} [{c[0]}]")
    if back:
        out.append("Called by (matched by name, approximate):")
        out += back[:20]
    return _trim("\n".join(out), CARD_CHARS)


def overview(project: str, files: dict[str, PyFile], others: list[str], imported_by: dict[str, set[str]]) -> str:
    all_paths = sorted(list(files) + others)
    py_lines = sum(f.n_lines for f in files.values())
    out = [f"PROJECT MAP: {project}",
           f"{len(all_paths)} files ({len(files)} Python with {py_lines} lines, {len(others)} other)."]
    by_dir: dict[str, list[str]] = defaultdict(list)
    for p in all_paths:
        parent = PurePosixPath(p).parent.parts
        by_dir["/".join(parent[:2]) + "/" if parent else "./"].append(p)
    out.append("Layout:")
    for d in sorted(by_dir):
        names = by_dir[d]
        out.append(f"  {d} ({len(names)} files)")
        for p in names[:12]:
            doc = files[p].doc if p in files else ""
            out.append(f"    {PurePosixPath(p).name}" + (f" - {doc[:70]}" if doc else ""))
        if len(names) > 12:
            out.append(f"    ... and {len(names) - 12} more")
    entries = [p for p, f in files.items() if f.is_entry]
    if entries:
        out.append("Entry points (run as scripts): " + ", ".join(sorted(entries)[:6])
                   + (f" and {len(entries) - 6} more" if len(entries) > 6 else ""))
    counts = Counter({p: len(v) for p, v in imported_by.items() if v})
    if counts:
        out.append("Most imported files: " + ", ".join(f"{p} ({n})" for p, n in counts.most_common(8)))
    out.append("Imports and calls are matched by name and are approximate.")
    return _trim("\n".join(out), OVERVIEW_CHARS)


# ---------------------------------------------------------------- build

def build_project_map(project: str, py_texts: dict[str, str], others: list[str]):
    """py_texts: path inside the project -> source text. Returns (chunks, data) for this project."""
    files = {}
    for path, text in py_texts.items():
        info = parse_python(path, text)
        if info:
            files[path] = info
    imports_of = link_imports(files)
    imported_by: dict[str, set[str]] = defaultdict(set)
    for p, targets in imports_of.items():
        for t in targets:
            imported_by[t].add(p)
    defs, calls_to, called_by = link_calls(files)

    chunks = [Chunk(_cid(project, "overview"), "code", project, f"{project}/(project map)",
                    settings.redact(overview(project, files, others, imported_by)), section="project map")]
    for path, f in sorted(files.items()):
        text = file_card(project, f, imports_of[path], imported_by.get(path, set()), calls_to, called_by)
        chunks.append(Chunk(_cid(project, path, "card"), "code", project, f"{project}/{path}", settings.redact(text),
                            section="file card"))
    data = {
        "symbols": {name: [[f"{project}/{p}", line] for p, _, line in d] for name, d in defs.items()},
        "calls": {f"{project}/{p}": [[fn.qual, fn.line, fn.end, [f"{project}/{t[0]}:{t[2]}" for t in calls_to.get((p, fn.qual), [])]]
                                      for fn in f.funcs] for p, f in files.items()},
    }
    return chunks, data


class CodeMap:
    """Symbol table and call links saved next to the index (.index/codemap.json)."""

    def __init__(self, data: dict | None = None):
        data = data or {}
        self.symbols: dict[str, list[list]] = data.get("symbols", {})
        self.calls: dict[str, list[list]] = data.get("calls", {})

    @classmethod
    def merge(cls, parts: list[dict]) -> "CodeMap":
        out = {"symbols": {}, "calls": {}}
        for d in parts:
            for name, v in d.get("symbols", {}).items():
                out["symbols"].setdefault(name, []).extend(v)
            out["calls"].update(d.get("calls", {}))
        return cls(out)

    def to_json(self) -> str:
        return json.dumps({"symbols": self.symbols, "calls": self.calls})

    def defined(self, name: str) -> list[tuple[str, int]]:
        return [(s, int(ln)) for s, ln in self.symbols.get(name, [])]

    def callees(self, source: str, line_start: int | None, line_end: int | None) -> list[tuple[str, int]]:
        """Definitions called by the functions that start inside [line_start, line_end] of this file."""
        out: list[tuple[str, int]] = []
        if line_start is None:
            return out
        for _qual, line, _end, targets in self.calls.get(source, []):
            if line_start <= line <= (line_end or line_start):
                for t in targets:
                    src, _, ln = t.rpartition(":")
                    if ln.isdigit() and (src, int(ln)) not in out:
                        out.append((src, int(ln)))
        return out
