"""Saved chat sessions (JSON files in sessions)."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

import settings


@dataclass
class TurnRecord:
    question: str
    answer_plain: str          # first answer, with tags as text, used for follow-up memory
    used_code: bool = False
    route: str = ""
    answers: list[dict] = field(default_factory=list)   # [{provider, model, segments:[...], error}]
    image: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class Session:
    id: str = field(default_factory=lambda: time.strftime("%Y%m%d-%H%M%S"))
    turns: list[TurnRecord] = field(default_factory=list)
    role: str = ""             # the role this chat belongs to (empty = none)

    def recent(self, n: int) -> list[TurnRecord]:
        return self.turns[-n:] if n > 0 else []

    def save(self) -> Path:
        d = settings.assert_writable(settings.SESSIONS_DIR)
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{self.id}.json"
        p.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        (d / f"{self.id}.md").write_text(self.to_markdown(), encoding="utf-8")
        return p

    @classmethod
    def load(cls, sid: str) -> "Session":
        raw = json.loads((settings.SESSIONS_DIR / f"{sid}.json").read_text())
        return cls(id=raw["id"], turns=[TurnRecord(**t) for t in raw["turns"]], role=raw.get("role", ""))

    @staticmethod
    def list_ids(role: str | None = None) -> list[str]:
        """Saved chats, newest first. With a role, only the chats of that role."""
        d = settings.SESSIONS_DIR
        if not d.exists():
            return []
        ids = sorted((p.stem for p in d.glob("*.json")), reverse=True)
        if role is None:
            return ids
        out = []
        for i in ids:
            try:
                if json.loads((d / f"{i}.json").read_text()).get("role", "") == role:
                    out.append(i)
            except (OSError, ValueError):
                continue
        return out

    @staticmethod
    def delete(sid: str) -> None:
        """Remove a saved chat (both files). Only plain ids made of letters, digits and dashes are accepted."""
        if not sid or not all(c.isalnum() or c == "-" for c in sid):
            raise ValueError(f"not a session id: {sid!r}")
        for ext in (".json", ".md"):
            settings.assert_writable(settings.SESSIONS_DIR / f"{sid}{ext}").unlink(missing_ok=True)

    @staticmethod
    def title(sid: str) -> str:
        """The first question of a saved chat, for the list in the interface."""
        try:
            raw = json.loads((settings.SESSIONS_DIR / f"{sid}.json").read_text())
            q = " ".join(raw["turns"][0]["question"].split())
        except (OSError, ValueError, KeyError, IndexError):
            return sid
        return f"{sid}  {q[:40]}{'...' if len(q) > 40 else ''}"

    def to_markdown(self) -> str:
        out = [f"# Session {self.id}", ""]
        for t in self.turns:
            out += [f"## {t.question}", "", f"_route: {t.route}_", ""]
            for a in t.answers:
                out += [f"**{a['provider']}** ({a.get('model', '')})", "", a.get("plain") or a.get("error") or "", ""]
            if t.image:
                out += [f"Image: {t.image}", ""]
        return "\n".join(out)


class Replay(Session):
    """The chat as it was before one turn, used to answer that turn again with another model. It is never written to disk."""

    def save(self) -> Path:
        return Path()
