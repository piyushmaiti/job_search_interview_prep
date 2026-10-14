"""Explore mode: Claude opens files of a project itself, but only inside a sanitized, read-only copy.

The copy is built with the same rules as indexing (excluded folders, secret files, size limit, never any image or
imaging file), with secrets redacted, no links, and a map of each project. Claude Code runs inside that copy with
only the Read, Grep and Glob tools, so it cannot see your other files, run commands, edit anything or reach the web.
The copy is deleted after each answer.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

import settings
from codemap import build_project_map
from ingest import iter_files
from projectctx import data_files_summary, estimate_tokens

STAGE_DIR = settings.CACHE_DIR / "explore"
MAP_FILE = "PROJECT_MAP.md"
DATA_FILE = "DATA_FILES.txt"
DEFAULT_TIMEOUT = 900
DEFAULT_MAX_CHARS = 30_000_000


@dataclass
class Plan:
    """What an explore question would let Claude read. Nothing is copied yet."""
    files: list[str] = field(default_factory=list)       # "project/path" for every file in the copy
    chars: int = 0
    projects: list[str] = field(default_factory=list)

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.chars)


def _read_files(cfg: dict, names: list[str]):
    """(project, path inside the project, redacted text, python?) for every readable file."""
    exts = {e.lower() for e in cfg["project_exts"]}
    for proj in cfg.get("projects", []):
        if proj["name"] not in names:
            continue
        base = settings.resolve(proj["path"])
        if not base.exists():
            continue
        for p in iter_files(base, exts, cfg):
            yield proj, base, p.relative_to(base).as_posix(), settings.redact(p.read_text(encoding="utf-8", errors="replace"))


def plan(cfg: dict, names: list[str]) -> Plan:
    out = Plan()
    for proj, base, rel, text in _read_files(cfg, names):
        out.files.append(f"{proj['name']}/{rel}")
        out.chars += len(text)
        if proj["name"] not in out.projects:
            out.projects.append(proj["name"])
    for proj in cfg.get("projects", []):
        if proj["name"] in out.projects:
            out.files += [f"{proj['name']}/{MAP_FILE}"] + ([f"{proj['name']}/{DATA_FILE}"] if proj.get("nifti_headers") else [])
    return out


def stage(cfg: dict, names: list[str]) -> Path:
    """Build the copy and return its folder. An older copy is removed first."""
    root = settings.assert_writable(STAGE_DIR / "current")
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    limit = int(cfg.get("explore", {}).get("max_chars", DEFAULT_MAX_CHARS))
    written = 0
    py: dict[str, dict[str, str]] = {}
    others: dict[str, list[str]] = {}
    for proj, base, rel, text in _read_files(cfg, names):
        written += len(text)
        if written > limit:
            break
        dest = root / proj["name"] / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        if rel.endswith(".py"):
            py.setdefault(proj["name"], {})[rel] = text
        else:
            others.setdefault(proj["name"], []).append(rel)
    for proj in cfg.get("projects", []):
        name = proj["name"]
        if name not in names or not (root / name).exists():
            continue
        chunks, _ = build_project_map(name, py.get(name, {}), others.get(name, []))
        (root / name / MAP_FILE).write_text("\n\n".join(c.text for c in chunks), encoding="utf-8")
        if proj.get("nifti_headers"):
            summary = data_files_summary(settings.resolve(proj["path"]), cfg)
            if summary:
                (root / name / DATA_FILE).write_text(summary, encoding="utf-8")
    return root


def cleanup() -> None:
    shutil.rmtree(settings.assert_writable(STAGE_DIR / "current"), ignore_errors=True)


EXPLORE_SYSTEM = """You are exploring a read-only copy of one or more software projects to answer the user's question. \
Start with PROJECT_MAP.md in each project folder (and DATA_FILES.txt if there is one), then use Glob, Grep and Read to open only \
the files you need. You cannot run code, edit files or reach anything outside this folder. Open at most about 25 files.

Citation rules:
- After a claim you got from code, cite the lines you read as [path:start-end], with the path relative to this folder, for example \
[my_project/src/model.py:10-24]. Use only line numbers you actually saw, and never cite a file you did not open.
- Claims from provided SOURCES (papers, notes) are cited with their ids, for example [S2]. Claims from your own knowledge get [M].
- If you cannot find the answer, say so plainly instead of guessing. Never repeat secrets or credentials. Be concise and concrete."""

