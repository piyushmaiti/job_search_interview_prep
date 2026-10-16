"""Questions and answers the person chose to keep, from Research and from the mock interview.

Each item is one small JSON file in the role's prep folder (workspace/prep/<role>/saved/), so deleting the role deletes them, they are
never indexed as sources, and nothing leaves this computer. Nothing is saved unless the person presses Save."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import candidate
import prep
import settings

KINDS = ("research", "mock")
_ID = re.compile(r"(research|mock)-\d{8}-\d{6}(-\d+)?")


def _dir(role: candidate.Role) -> Path:
    return prep.prep_dir(role) / "saved"


def save(role: candidate.Role, kind: str, item: dict) -> str:
    """Keep one item. Returns its id."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind: {kind!r}")
    d = settings.assert_writable(_dir(role))
    d.mkdir(parents=True, exist_ok=True)
    base = f"{kind}-{time.strftime('%Y%m%d-%H%M%S')}"
    sid, n = base, 1
    while (d / f"{sid}.json").exists():
        n += 1
        sid = f"{base}-{n}"
    data = {**item, "id": sid, "kind": kind, "saved_at": time.strftime("%Y-%m-%d %H:%M")}
    settings.assert_writable(d / f"{sid}.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return sid


def items(role: candidate.Role, kind: str) -> list[dict]:
    """The kept items of one kind, newest first. A file that cannot be read is skipped."""
    d = _dir(role)
    out = []
    for p in sorted(d.glob(f"{kind}-*.json"), reverse=True) if d.is_dir() else []:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("id") == p.stem:
            out.append(data)
    return out


def delete(role: candidate.Role, sid: str) -> None:
    """Remove one kept item. Only ids this module made are accepted."""
    if not _ID.fullmatch(sid or ""):
        raise ValueError(f"not a saved item: {sid!r}")
    settings.assert_writable(_dir(role) / f"{sid}.json").unlink(missing_ok=True)
