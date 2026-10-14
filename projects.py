"""Adding and removing code projects from the app, instead of editing config.local.yaml by hand.

A project is a folder of code the tool may read (never write). It is saved in config.local.yaml, which git ignores and which only
exists on this computer. Adding a project sends nothing anywhere; every question that would send code still asks which provider
may receive it."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

import ingest
import settings

NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,39}")
FORBIDDEN_PARTS = {".ssh", ".gnupg", ".aws", ".kube", ".docker"}


def lan_mode() -> bool:
    """True when the app was started with --lan (open to other devices). Projects cannot be added then, because the folder is read on this computer."""
    return bool(os.environ.get("INTERVIEW_PREP_LAN"))


def default_name(path: Path) -> str:
    n = re.sub(r"[^A-Za-z0-9_.\-]+", "_", path.name).strip("._-")
    return n[:40] or "project"


@dataclass
class Preview:
    readable: int
    skipped: int
    size_bytes: int
    examples: list[str]


def _local() -> dict:
    p = settings.LOCAL_CONFIG
    if not p.is_file():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _save_local(data: dict) -> None:
    p = settings.assert_writable(settings.LOCAL_CONFIG)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# Written by the app (Add a code project). Your own paths; never committed.\n" + yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
                 encoding="utf-8")


def local_names() -> list[str]:
    """The projects added through the app or by hand in config.local.yaml, in order."""
    return [p["name"] for p in (_local().get("projects") or []) if isinstance(p, dict) and p.get("name")]


def check_path(raw: str) -> Path:
    """The folder as an absolute path. Raises ValueError, in words for the user, when it must not be used."""
    raw = (raw or "").strip().strip("\"'")
    if not raw:
        raise ValueError("Give the folder that holds the code.")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        raise ValueError("Give the full path, for example /home/you/code/my_project or ~/code/my_project.")
    try:
        p = p.resolve()
    except OSError as e:
        raise ValueError(f"Could not open that path: {e}") from e
    if not p.is_dir():
        raise ValueError("That folder does not exist, or it is a file.")
    if p == Path(p.anchor) or p == Path.home().resolve():
        raise ValueError("Pick the folder of one project, not your whole computer or home folder.")
    if any(part in FORBIDDEN_PARTS for part in p.parts):
        raise ValueError("That folder holds keys or credentials and cannot be added.")
    if settings.in_hard_excluded(p):
        raise ValueError("That folder is marked as patient data and is never read.")
    for q in [settings.TOOL_DIR, settings.WORKSPACE_DIR, settings.SESSIONS_DIR, settings.KNOWLEDGE_DIR, settings.INDEX_DIR, settings.CACHE_DIR]:
        q = Path(q).resolve()
        if p == q or q in p.parents or p in q.parents:       # inside this tool's folder, or a folder that contains it (and so your private files)
            raise ValueError("That folder overlaps this tool's own folder, where your private files live. Pick the code project itself.")
    return p


def preview(path: Path, cfg: dict | None = None) -> Preview:
    """What would be read: counts only, nothing is read into the index or sent anywhere."""
    cfg = cfg or settings.load_config()
    exts = {e.lower() for e in cfg["project_exts"]}
    files = list(ingest.iter_files(path, exts, cfg))
    every = [p for p in ingest.walk_files(path, cfg)]
    size = 0
    for f in files:
        try:
            size += f.stat().st_size
        except OSError:
            pass
    return Preview(len(files), max(len(every) - len(files), 0), size, [f.relative_to(path).as_posix() for f in files[:6]])


def add(raw_path: str, name: str = "") -> str:
    """Save a project in config.local.yaml. Returns its name. Raises ValueError with a message for the user."""
    if lan_mode():
        raise ValueError("Projects can only be added when the app runs on this computer alone, not with --lan.")
    path = check_path(raw_path)
    name = (name or "").strip() or default_name(path)
    if not NAME_RE.fullmatch(name):
        raise ValueError("Use a short name of letters, digits, dots, dashes or underscores.")
    cfg = settings.load_config()
    taken = {p["name"] for p in cfg.get("projects", [])}
    if name in taken:
        raise ValueError(f"A project called {name} already exists. Choose another name.")
    for p in cfg.get("projects", []):
        if settings.resolve(p["path"]).resolve() == path:
            raise ValueError(f"That folder is already added as {p['name']}.")
    data = _local()
    data["projects"] = list(data.get("projects") or []) + [{"name": name, "path": str(path)}]
    _save_local(data)
    return name


def remove(name: str) -> None:
    """Take a project out of config.local.yaml. The code on disk is never touched."""
    if lan_mode():
        raise ValueError("Projects can only be changed when the app runs on this computer alone.")
    data = _local()
    keep = [p for p in (data.get("projects") or []) if not (isinstance(p, dict) and p.get("name") == name)]
    if len(keep) == len(data.get("projects") or []):
        raise ValueError(f"{name} was not added through the app or config.local.yaml.")
    data["projects"] = keep
    _save_local(data)
