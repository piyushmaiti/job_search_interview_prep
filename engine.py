"""Ties retrieval, routing, consent, providers and citations together.

Order of operations matters: retrieve -> route -> consent (if code is involved) -> call providers.
No provider is called before consent has been given.
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import explore
import knowledge
import projectctx
import candidate
import settings
from citations import Segment, render, render_explore, to_plain
from consent import ConsentFn, ConsentRequest, cli_consent, options_for
from index import Index, is_file_question, matches_any
from ingest import Chunk
from providers.base import Provider, ProviderError
from router import Route, decide, decide_whole, is_image_request
from sessions import Session, TurnRecord


@dataclass
class AnswerOut:
    provider: str
    model: str
    segments: list[Segment] = field(default_factory=list)
    plain: str = ""
    error: str | None = None
    usage: dict = field(default_factory=dict)


@dataclass
class TurnResult:
    question: str
    route: Route | None = None
    answers: list[AnswerOut] = field(default_factory=list)
    chunks: dict[str, Chunk] = field(default_factory=dict)
    used_code: bool = False
    cancelled: bool = False
    image_path: str | None = None
    notes: list[str] = field(default_factory=list)


class Engine:
    def __init__(self, cfg: dict, index: Index, providers: dict[str, Provider]):
        self.cfg, self.index, self.providers = cfg, index, providers

    def available(self) -> set[str]:
        return {k for k, p in self.providers.items() if p.available()[0]}

    def default_groups(self) -> set[str]:
        """Papers and notes only. Project code is searched only when the user selects it."""
        return {g for g, info in self.index.groups().items() if info["kind"] == "local"} - settings.removed_sources()

    # ------------------------------------------------------------ retrieval
    def _retrieve(self, question, history, groups, route: Route, k: int | None) -> tuple[list[Chunk], list[str]]:
        notes: list[str] = []
        r = self.cfg["retrieval"]
        if route.whole_docs:
            chunks, trunc = self.index.whole_documents(groups, r["whole_doc_char_cap"])
            if trunc:
                notes.append("Documents were longer than the size limit, so the end was left out.")
            if chunks:
                return chunks, notes
            notes.append("No whole documents were selected; used the closest passages instead.")
        query = question
        if history and len(question.split()) < 10:
            query = f"{history[-1].question} {question}"   # short follow-ups borrow the previous question
        hits = self.index.search(query, k or r["k"], groups, diverse=is_file_question(question))
        if r.get("pinned_docs") and matches_any(question, r.get("pinned_when")):   # a rule you set in the config; off by default
            have = {c.id for c in hits}
            pinned = [c for c in self.index.pinned_docs(r["pinned_docs"], groups) if c.id not in have]
            if pinned:
                hits = hits + pinned
                names = ", ".join(sorted({c.source.rsplit("/", 1)[-1] for c in pinned}))
                notes.append(f"A pinned-document rule matched, so {names} was included in full.")
        hits = self.index.expand_small_docs(hits, groups, int(r.get("whole_small_docs_chars", 24000)))
        return self.index.expand_code(hits, groups), notes

    # ------------------------------------------------------------ main entry
    def ask(self, question: str, mode: str | None = None, web: bool | None = None,
            groups: set[str] | None = None, session: Session | None = None,
            consent: ConsentFn = cli_consent, memory: bool | None = None, k: int | None = None,
            code_mode: str = "retrieve", use_knowledge: bool | None = None) -> TurnResult:
        mode = mode or self.cfg.get("default_mode", "auto")
        web = self.cfg["web"]["default"] if web is None else web
        memory = self.cfg["chat"]["memory"] if memory is None else memory
        res = TurnResult(question)
        if groups is None:
            groups = self.default_groups()
        groups = set(groups) - settings.removed_sources()

        names, whole = [], None
        if code_mode == "explore":
            names = [n for n in projectctx.project_names(self.cfg) if n in groups]
            if names and not is_image_request(question):
                return self._explore(question, mode, groups, names, session, consent, memory, k, res)
            res.notes.append("Explore needs a project ticked in Sources, so normal retrieval was used." if not names
                             else "Image requests use normal retrieval, so explore was skipped.")
            names = []
        if code_mode == "whole":
            names = [n for n in projectctx.project_names(self.cfg) if n in groups]
            if not names:
                res.notes.append("Whole project needs a project ticked in Sources, so normal retrieval was used.")
            elif is_image_request(question):
                res.notes.append("Image requests use normal retrieval, so whole project was skipped.")
            else:
                whole = projectctx.collect(self.cfg, names)
                if not whole.files:
                    res.notes.append("The selected project has no readable files, so normal retrieval was used.")
                    whole = None
        try:
            if whole:
                route = decide_whole(mode, self.available(), whole.tokens, projectctx.caps(self.cfg))
            else:
                route = decide(question, mode, self.available(), {k: p.locality for k, p in self.providers.items()})
        except ValueError as e:
            res.notes.append(str(e))
            return res
        res.route = route

        recent = session.recent(self.cfg["chat"]["memory_turns"]) if (session and memory) else []
        history = [(t.question, t.answer_plain) for t in recent]

        if whole:
            self._fit(whole, route.providers, res)
            if whole.skipped:
                res.notes.append(f"Left out as low-value: {len(whole.skipped)} files (lock files, logs, .tex diagrams).")
        doc_chunks, notes = self._retrieve(question, recent, groups - set(names) if whole else groups, route, k)
        res.notes += notes
        use_knowledge = self.cfg.get("knowledge", {}).get("use", True) if use_knowledge is None else use_knowledge
        know = []
        if use_knowledge and not whole:           # a whole-project request already contains everything
            know, knotes = self._knowledge(question, groups)
            res.notes += knotes

        def assemble():
            """Project files first (when the whole project is sent), then the retrieved papers and notes."""
            chunks = (whole.chunks() if whole else []) + know + doc_chunks
            numbered = [(f"S{i}", c) for i, c in enumerate(chunks, start=1)]
            res.chunks = {sid: c for sid, c in numbered}
            return chunks, numbered

        chunks, numbered = assemble()

        code_files = sorted({c.source for c in chunks if c.kind == "code"})
        carried = any(t.used_code for t in recent)
        res.used_code = bool(code_files)

        # ---- consent gate: nothing below this point runs until the user has chosen a provider
        providers = list(route.providers)
        personal = sorted({c.source for c in chunks if candidate.is_workspace(c.group)})
        personal_out = bool(personal) and any(not self._personal_ok(p) for p in providers)
        if code_files or carried or personal_out:
            files = list(code_files) or (["(project code from an earlier turn in this chat)"] if carried else [])
            if personal_out:
                files += personal

            options = options_for(self.providers)
            for o in options:
                if whole and o.key == "ollama":
                    o.available, o.note = False, "too small for a whole project"
            detail = (f"Whole project: {len(whole.files)} files, about {whole.tokens:,} tokens from {', '.join(names)}, "
                      "will be sent in one request.") if whole else ""
            if personal_out:
                detail = (detail + " " if detail else "") + "Your CV, profile or job text is part of this request. Only Claude, or a local Ollama model, gets it without asking."
            approved = consent(ConsentRequest(files, options, compare=(mode == "compare"), carried=carried and not code_files,
                                              scope="whole" if whole else "snippets", detail=detail,
                                              kind="code" if (code_files or carried) else "personal"))
            if not approved:
                res.cancelled = True
                res.notes.append("Cancelled. Nothing was sent to any model.")
                return res
            providers = [p for p in approved if p in self.providers]
            if whole:
                self._fit(whole, providers, res)         # the choice may differ from the route, so fit the project again
                chunks, numbered = assemble()
            if route.image and "gemini" not in providers:
                route.image = False
                res.notes.append("Image skipped: Gemini was not approved for this code.")
        res.used_code = res.used_code or carried

        # ---- call the approved providers (in parallel when comparing)
        def run(name: str) -> AnswerOut:
            p = self.providers[name]
            try:
                out = p.generate(question, numbered, history, web and p.supports_web)
                segs = render(out.text, out.web, res.chunks)
                return AnswerOut(name, out.model or p.model, segs, to_plain(segs), None, out.usage)
            except ProviderError as e:
                return AnswerOut(name, p.model, error=str(e))
            except Exception as e:  # keep the app alive on SDK surprises
                return AnswerOut(name, p.model, error=f"{type(e).__name__}: {e}")

        if len(providers) > 1:
            with ThreadPoolExecutor(max_workers=len(providers)) as ex:
                res.answers = list(ex.map(run, providers))
        else:
            res.answers = [run(n) for n in providers]

        # ---- optional infographic
        first_ok = next((a for a in res.answers if not a.error), None)
        if route.image and first_ok and "gemini" in providers:
            try:
                res.image_path = self._infographic(question, first_ok, res.chunks)
            except ProviderError as e:
                res.notes.append(str(e))

        if session is not None:
            self._record(session, res, route)
        return res

    # ------------------------------------------------------------ project knowledge
    def _personal_ok(self, key: str) -> bool:
        """Your CV, profile and job text go to Claude, or to an Ollama model that runs on this machine, without a question."""
        p = self.providers.get(key)
        return key == "claude" or (key == "ollama" and getattr(p, "locality", "cloud") == "local")

    def _knowledge(self, question: str, groups: set[str]) -> tuple[list[Chunk], list[str]]:
        """The parts of each built knowledge document that match the question, for the ticked projects."""
        chunks, notes = [], []
        limit = int(self.cfg.get("knowledge", {}).get("max_chars", 20000))
        for name in projectctx.project_names(self.cfg):
            if name not in groups or not knowledge.load(name):
                continue
            chunks += knowledge.select(name, question, limit)
            st = knowledge.status(self.cfg, name)
            note = f"Using project knowledge for {name} (built {st.built_at})."
            if st.stale:
                note += f" {st.n_changed} files changed since; press Update in Sources to refresh it."
            notes.append(note)
        return chunks, notes

    def knowledge_request(self, name: str, update: bool = False):
        """What building (or updating) the knowledge for a project would send, as a consent request, plus the files."""
        files = knowledge.current_files(self.cfg, name)
        if not files:
            return None, []
        if update:
            st = knowledge.status(self.cfg, name)
            send = [files[r] for r in st.changed + st.added if r in files]
        else:
            send = list(files.values())
        tokens = sum(f.tokens for f in send)
        options = options_for(self.providers)
        for o in options:
            if o.key != "claude":
                o.available, o.note = False, "project knowledge is written by Claude Code"
        detail = (f"{'Update' if update else 'Build'} project knowledge for {name}: Claude, through your Claude Code login, "
                  f"will read {len(send)} files (about {tokens:,} tokens) and write a document about them, which is kept on this machine.")
        return ConsentRequest([f"{name}/{f.rel}" for f in send], options, scope="knowledge", detail=detail), send

    def build_knowledge(self, name: str, consent: ConsentFn = cli_consent, update: bool = False) -> tuple[bool, str]:
        """Claude reads the whole project once and writes the knowledge document. Returns (done, message)."""
        login = _claude_login(self.providers.get("claude"))
        ok, why = login.available() if login else (False, "")
        if not ok:
            return False, "Project knowledge needs Claude through your Claude Code login (the `claude` command). " + (why or "Set Claude access to Auto or Login.")
        if name not in projectctx.project_names(self.cfg):
            return False, f"{name} is not a project in the config."
        existing = knowledge.load(name) if update else None
        update = update and existing is not None
        files_now = knowledge.current_files(self.cfg, name)
        if not files_now:
            return False, f"{name} has no readable files."
        req, send = self.knowledge_request(name, update)
        if update and not send and not knowledge.status(self.cfg, name).removed:
            return False, "Nothing has changed since the knowledge was built."
        tokens = sum(f.tokens for f in send) + (knowledge.estimate_tokens(len(existing[0])) if update else 0)
        cap = projectctx.caps(self.cfg)["claude"]
        if tokens > cap:
            return False, f"{name} is too large for a single pass ({tokens:,} tokens; the limit is {cap:,}). Raise whole_project.max_tokens.claude or use Explore."
        approved = consent(req)
        if not approved or "claude" not in approved:
            return False, "Cancelled. Nothing was sent to any model."
        timeout = int(self.cfg.get("knowledge", {}).get("timeout_seconds", 1200))
        try:
            if update:
                st = knowledge.status(self.cfg, name)
                res = login.complete(knowledge.UPDATE_SYSTEM, knowledge.update_prompt(name, existing[0], send, st.removed), timeout)
            else:
                res = login.complete(knowledge.BUILD_SYSTEM, knowledge.build_prompt(name, send), timeout)
        except ProviderError as e:
            return False, str(e)
        text = res.text.strip()
        if not knowledge.looks_like_a_document(text):
            return False, "Claude did not return a usable document. Nothing was saved. Try again."
        doc, n_ok, n_bad = knowledge.verify_citations(text, files_now)
        knowledge.save(name, doc, knowledge.record(name, doc, files_now, res.model, n_ok, n_bad))
        return True, (f"{'Updated' if update else 'Built'} project knowledge for {name}: {len(files_now)} files, "
                      f"{knowledge.estimate_tokens(len(doc)):,} tokens. {n_ok} citations checked against the code, {n_bad} unverified.")

    # ------------------------------------------------------------ explore
    def _explore(self, question, mode, groups, names, session, consent, memory, k, res: TurnResult) -> TurnResult:
        """Claude reads a sanitized, read-only copy of the ticked projects through Claude Code. Order: check, consent, copy, ask, delete."""
        if mode in ("gemini", "ollama", "compare"):
            res.notes.append("Explore works only with Claude, through your Claude Code login. Choose Auto or Claude.")
            return res
        explorer = _explorer(self.providers.get("claude"))
        ok, why = explorer.available() if explorer else (False, "")
        if not ok:
            res.notes.append("Explore needs Claude through your Claude Code login (the `claude` command). "
                             + (why or "Set Claude access to Auto or Login in Settings."))
            return res
        res.route = Route(["claude"], "explore: Claude reads a read-only copy of the project through your login")
        recent = session.recent(self.cfg["chat"]["memory_turns"]) if (session and (self.cfg["chat"]["memory"] if memory is None else memory)) else []
        history = [(t.question, t.answer_plain) for t in recent]
        plan = explore.plan(self.cfg, names)
        if not plan.files:
            res.notes.append("The selected project has no readable files, so nothing was explored.")
            return res
        doc_chunks, notes = self._retrieve(question, recent, groups - set(names), res.route, k)
        res.notes += notes
        numbered = [(f"S{i}", c) for i, c in enumerate(doc_chunks, start=1)]
        res.chunks = {sid: c for sid, c in numbered}
        res.used_code = True

        options = options_for(self.providers)
        for o in options:
            if o.key != "claude":
                o.available, o.note = False, "explore works only with Claude Code"
        detail = (f"Explore: Claude may open any of {len(plan.files)} files (about {plan.tokens:,} tokens) from "
                  f"{', '.join(plan.projects)}, using read-only tools, through your Claude Code login. It decides what to open, "
                  "so how much is sent is not known in advance. This can take several minutes.")
        approved = consent(ConsentRequest(plan.files, options, compare=False, carried=False, scope="explore", detail=detail))
        if not approved or "claude" not in approved:
            res.cancelled = True
            res.notes.append("Cancelled. Nothing was sent to any model.")
            return res
        timeout = int(self.cfg.get("explore", {}).get("timeout_seconds", explore.DEFAULT_TIMEOUT))
        try:
            root = explore.stage(self.cfg, names)
            out = explorer.explore(question, numbered, history, root, timeout)
            segs = render_explore(out.text, root, res.chunks)
            ans = AnswerOut("claude", out.model or explorer.model, segs, to_plain(segs), None, out.usage)
        except ProviderError as e:
            ans = AnswerOut("claude", explorer.model, error=str(e))
        except Exception as e:
            ans = AnswerOut("claude", explorer.model, error=f"{type(e).__name__}: {e}")
        finally:
            explore.cleanup()                       # the copy is never kept
        res.answers = [ans]
        if session is not None:
            self._record(session, res, res.route)
        return res

    # ------------------------------------------------------------ helpers
    def _fit(self, whole, providers: list[str], res: TurnResult) -> None:
        """Trim the project to the smallest limit among the providers that will read it, and say what was left out."""
        limits = projectctx.caps(self.cfg)
        cap = min(limits.get(p, 100_000) for p in providers)
        dropped = whole.trim_to(cap)
        if dropped:
            shown = ", ".join(d.split("/", 1)[-1] for d in dropped[:5]) + (f" and {len(dropped) - 5} more" if len(dropped) > 5 else "")
            res.notes.append(f"The project is over {cap:,} tokens, so the {len(dropped)} largest files were left out: {shown}.")

    def _infographic(self, question: str, ans: AnswerOut, chunks: dict[str, Chunk]) -> str:
        prompt = build_brief(question, ans)
        gem = self.providers["gemini"]
        png = gem.generate_image(prompt, self.cfg["models"]["gemini_image"])
        out = settings.assert_writable(settings.OUTPUTS_DIR)
        out.mkdir(parents=True, exist_ok=True)
        stem = f"{time.strftime('%Y%m%d-%H%M%S')}_{re.sub(r'[^a-z0-9]+', '-', question.lower())[:40].strip('-')}"
        (out / f"{stem}.png").write_bytes(png)
        sources = sorted({s.text for s in ans.segments if s.kind in ('local', 'code', 'web')})
        (out / f"{stem}.md").write_text(
            f"# AI-generated draft\n\nModel: {self.cfg['models']['gemini_image']}\n\nPrompt:\n\n{prompt}\n\n"
            "Sources used:\n\n" + "\n".join(f"- {s}" for s in sources) + "\n\nCheck small text and numbers against the sources.\n",
            encoding="utf-8")
        return str(out / f"{stem}.png")

    def _record(self, session: Session, res: TurnResult, route: Route):
        first = next((a for a in res.answers if not a.error), None)
        session.turns.append(TurnRecord(
            question=res.question,
            answer_plain=first.plain if first else "",
            used_code=res.used_code,
            route=route.reason,
            answers=[{"provider": a.provider, "model": a.model, "plain": a.plain, "error": a.error,
                      "segments": [vars(s) for s in a.segments]} for a in res.answers],
            image=res.image_path,
            notes=res.notes,
        ))
        session.save()


def _claude_login(p):
    """The provider object that can write the knowledge document (Claude through the login), or None."""
    if p is None:
        return None
    if hasattr(p, "complete"):
        return p
    login = getattr(p, "login", None)
    return login if hasattr(login, "complete") else None


def _explorer(p):
    """The provider object that can run explore (Claude through the login), or None."""
    if p is None:
        return None
    if hasattr(p, "explore"):
        return p
    login = getattr(p, "login", None)
    return login if hasattr(login, "explore") else None


def build_brief(question: str, ans: AnswerOut) -> str:
    """Short structured brief for the image model, built only from the cited answer."""
    body = "".join(s.text for s in ans.segments if s.kind == "text").strip()
    body = re.sub(r"\s+", " ", body)[:1500]
    return (
        "Create a clean, minimal infographic with a plain light background, a short title, and 3 to 5 labeled blocks. "
        "Use large, legible text, very few words, and no tiny numbers. Use only the content below; do not add facts.\n\n"
        f"Topic: {question}\n\nContent: {body}"
    )
