"""Provider consent for project code.

Whenever the context for a request contains project code (or follows a turn that did), the user must
choose which provider may receive it. There is no default and nothing is remembered between requests.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Callable

from providers.base import Provider


@dataclass
class ConsentOption:
    key: str
    label: str
    locality: str      # "local" | "cloud"
    available: bool
    note: str = ""


@dataclass
class ConsentRequest:
    files: list[str]            # code files whose text would be sent
    options: list[ConsentOption]
    compare: bool = False       # the user asked to compare two providers
    carried: bool = False       # code came from an earlier turn kept in memory
    scope: str = "snippets"     # "snippets" (retrieved pieces) or "whole" (every file of the project)
    detail: str = ""            # one sentence on the size of the request, shown in the prompt
    kind: str = "code"          # "code" (project files) or "personal" (your CV, profile and job text)


# A consent function returns the approved provider keys, or None to cancel. Nothing is sent on None.
ConsentFn = Callable[[ConsentRequest], "list[str] | None"]


def options_for(providers: dict[str, Provider]) -> list[ConsentOption]:
    out = []
    for key, p in providers.items():
        ok, why = p.available()
        out.append(ConsentOption(key, p.label(), p.locality, ok, why))
    return out


def cli_consent(req: ConsentRequest) -> list[str] | None:
    if not sys.stdin.isatty():
        what = "your personal documents" if req.kind == "personal" else "project code"
        print(f"This question includes {what} and needs your choice of provider. Not a terminal, so nothing was sent.")
        return None
    print("\nThis question will send " + ("your CV, profile and job text" if req.kind == "personal" else "project code") + " to a model.")
    if req.carried:
        print("(Code from an earlier turn in this chat is still part of the context.)")
    if req.detail:
        print(req.detail)
    shown = req.files[:12]
    print("Files:")
    for f in shown:
        print("  -", f)
    if len(req.files) > len(shown):
        print(f"  ... and {len(req.files) - len(shown)} more")
    for i, o in enumerate(req.options, start=1):
        flag = "" if o.available else f"  [unavailable: {o.note}]"
        print(f"  {i}. {o.label}{flag}")
    hint = "numbers separated by commas for several" if req.compare else "a number"
    raw = input(f"Which provider may receive it? Enter {hint}, or c to cancel: ").strip().lower()
    if raw in ("", "c", "cancel"):
        return None
    chosen = []
    for tok in raw.replace(" ", "").split(","):
        if tok.isdigit() and 1 <= int(tok) <= len(req.options):
            o = req.options[int(tok) - 1]
            if o.available:
                chosen.append(o.key)
    if not req.compare:
        chosen = chosen[:1] if len(chosen) == 1 else []
    return chosen or None
