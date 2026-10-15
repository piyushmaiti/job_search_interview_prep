#!/usr/bin/env python
"""Command-line interface.

  python ask.py "your question"                  one question
  python ask.py --chat                           conversation with memory (saved in sessions/)
  python ask.py --reindex                        rebuild the index
  python ask.py --status                         show index and provider status
Options: --model auto|claude|gemini|openai|ollama|compare   --claude-model ID   --gemini-model ID   --openai-model ID   --ollama-model NAME
         --no-web   --no-memory   --sources "Library"   --resume SESSION_ID   --demo
"""
from __future__ import annotations

import argparse
import sys

import settings

settings.setup_env()

from consent import cli_consent                     # noqa: E402
from engine import Engine, TurnResult               # noqa: E402
from index import Index                             # noqa: E402
from providers import build_providers               # noqa: E402
from sessions import Session                        # noqa: E402

COLORS = {"local": "34", "web": "32", "code": "33", "model": "90", "unverified": "31"}


def fmt(res: TurnResult, color: bool = True) -> str:
    out = []
    if res.route:
        out.append(f"[route] {', '.join(res.route.providers)}: {res.route.reason}")
    for n in res.notes:
        out.append(f"[note] {n}")
    for a in res.answers:
        out.append(f"\n--- {a.provider} ({a.model}) ---")
        if a.error:
            out.append(f"Error: {a.error}")
            continue
        text = ""
        for s in a.segments:
            if s.kind == "text":
                text += s.text
            else:
                tag = s.plain()
                text += f"\033[{COLORS.get(s.kind, '0')}m{tag}\033[0m" if color else tag
        out.append(text)
    if res.image_path:
        out.append(f"\n[image] saved to {res.image_path} (AI-generated draft; check it against the sources)")
    return "\n".join(out)


def parse_groups(index: Index, cfg: dict, names: str | None, with_code: bool = False) -> set[str] | None:
    if not names and not with_code:
        return None            # engine default: papers and notes only
    if not names:
        return {g for g in index.groups()}   # everything, including all project code
    wanted = {n.strip().lower() for n in names.split(",") if n.strip()}
    out: set[str] = set()
    for d in cfg["docs"]:
        if d["name"].lower() in wanted:
            prefix = settings.resolve(d["path"]).name + "/"
            out |= {g for g in index.groups() if g.startswith(prefix)}
    for p in cfg["projects"]:
        if p["name"].lower() in wanted:
            out.add(p["name"])
    return out or None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Interview Prep Tool: prepare for interviews from your own documents", formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("question", nargs="*")
    ap.add_argument("--chat", action="store_true")
    ap.add_argument("--reindex", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--model", default=None, choices=["auto", "claude", "gemini", "openai", "ollama", "compare"])
    ap.add_argument("--claude-access", choices=["auto", "login", "api"], help="auto = login, API key only for web search; login = claude -p only; api = API key only")
    ap.add_argument("--claude-model"); ap.add_argument("--gemini-model"); ap.add_argument("--openai-model"); ap.add_argument("--ollama-model")
    ap.add_argument("--no-web", action="store_true"); ap.add_argument("--no-memory", action="store_true")
    ap.add_argument("--sources", help="comma-separated source names from config.yaml, for example: Library,my_project")
    ap.add_argument("--code-mode", choices=["retrieve", "whole", "explore"], default="retrieve",
                    help="retrieve = best-matching pieces (default); whole = every readable file of the selected projects in one request; explore = Claude opens files itself in a read-only copy (Claude login only)")
    ap.add_argument("--knowledge-provider", choices=["claude", "openai", "ollama"], default="claude", help="who reads the project for --build-knowledge or --update-knowledge (default claude)")
    ap.add_argument("--build-knowledge", metavar="PROJECT", help="Claude reads the whole project once and writes a lasting knowledge document (asks first)")
    ap.add_argument("--update-knowledge", metavar="PROJECT", help="update the knowledge document from the files that changed (asks first)")
    ap.add_argument("--knowledge-status", action="store_true", help="show which projects have knowledge built, and whether it is current")
    ap.add_argument("--code", action="store_true", help="also search project code (you will be asked which provider may receive it)")
    ap.add_argument("--resume"); ap.add_argument("--k", type=int)
    ap.add_argument("--demo", action="store_true", help="use offline stand-in models (no API keys, no network)")
    a = ap.parse_args(argv)

    cfg = settings.load_config()
    index = Index.ensure(cfg, rebuild=a.reindex)
    if a.demo:
        from providers.fake import FakeProvider
        providers = {"claude": FakeProvider(label="claude", locality="cloud"), "gemini": FakeProvider(label="gemini", locality="cloud"),
                     "ollama": FakeProvider(label="ollama", locality="local"), "openai": FakeProvider(label="openai", locality="cloud")}
    else:
        providers = build_providers(cfg, {"claude": a.claude_model, "claude_access": a.claude_access, "gemini": a.gemini_model, "ollama": a.ollama_model, "openai": a.openai_model})
    eng = Engine(cfg, index, providers)

    if a.build_knowledge or a.update_knowledge:
        done, msg = eng.build_knowledge(a.build_knowledge or a.update_knowledge, consent=cli_consent, update=bool(a.update_knowledge), provider=a.knowledge_provider,
                                       progress=lambda d, t, label: print(f"  reading {label} ({d} of {t})"))
        print(msg)
        return 0 if done else 1
    if a.knowledge_status:
        import knowledge, projectctx
        for name in projectctx.project_names(cfg):
            st = knowledge.status(cfg, name)
            print(f"{name:24} " + (f"built {st.built_at}, {st.n_files} files, " + (f"{st.n_changed} changed since" if st.stale else "up to date")
                                    + f", {st.verified} citations checked, {st.unverified} unverified" if st.built else "not built"))
        return 0
    if a.status or (a.reindex and not a.question and not a.chat):
        m = index.meta
        print(f"index: {m['n_chunks']} chunks ({m['n_local']} papers/notes, {m['n_code']} code) via {m['backend']}:{m['model']}, built {m['built_at']}")
        for k, p in providers.items():
            ok, why = p.available()
            print(f"{k:7} {p.label():70} {'ready' if ok else 'not ready: ' + why}")
        return 0

    groups = parse_groups(index, cfg, a.sources, a.code)
    session = Session.load(a.resume) if a.resume else Session()
    color = sys.stdout.isatty()
    kw = dict(mode=a.model, web=False if a.no_web else None, memory=False if a.no_memory else None, groups=groups, k=a.k, code_mode=a.code_mode)

    if a.chat:
        print(f"Session {session.id}. Commands: /model NAME, /web on|off, /quit")
        mode, web = a.model, kw["web"]
        while True:
            try:
                q = input("\nyou> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not q:
                continue
            if q in ("/quit", "/q"):
                break
            if q.startswith("/model "):
                mode = q.split(None, 1)[1]; print(f"model: {mode}"); continue
            if q.startswith("/web "):
                web = q.split(None, 1)[1] == "on"; print(f"web: {web}"); continue
            kw.update(mode=mode, web=web)
            print(fmt(eng.ask(q, session=session, consent=cli_consent, **kw), color))
        print(f"Saved: {settings.SESSIONS_DIR / (session.id + '.md')}")
        return 0

    if not a.question:
        ap.print_help()
        return 1
    one_shot = session if a.resume else None   # one-off questions are not saved; --chat and --resume are
    print(fmt(eng.ask(" ".join(a.question), session=one_shot, consent=cli_consent, **kw), color))
    return 0


if __name__ == "__main__":
    sys.exit(main())
