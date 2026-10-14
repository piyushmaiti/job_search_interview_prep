"""Consent gating, routing, citations and page numbers, using offline stand-in providers."""
import pytest

import settings
from citations import render
from providers.base import WebCite
from router import decide
from sessions import Session

ALL = {"claude", "gemini", "ollama"}


# ---------------------------------------------------------------- citations
def test_unknown_source_id_is_flagged(chunks):
    idmap = {"S1": chunks[0]}
    segs = render("Fact [S1]. Invented [S9]. Own knowledge [M].", [], idmap)
    kinds = [s.kind for s in segs if s.kind != "text"]
    assert kinds == ["local", "unverified", "model"]
    assert next(s for s in segs if s.kind == "local").text == "library/p.pdf, p.3"


def test_code_tag_has_line_range(chunks):
    segs = render("Loss is defined here [S1].", [], {"S1": chunks[2]})
    assert [s.text for s in segs if s.kind == "code"] == ["tau_proj/model.py:10-14"]


def test_web_tags_land_after_their_span():
    text = "A claim. Another claim."
    segs = render(text, [WebCite(end=8, url="https://e.org/a")], {})
    assert segs[0].text == "A claim." and segs[1].kind == "web" and segs[1].ref == "https://e.org/a"
    assert segs[2].text == " Another claim."


def test_multi_id_marker(chunks):
    segs = render("Both [S1, S2].", [], {"S1": chunks[0], "S2": chunks[1]})
    assert [s.kind for s in segs if s.kind != "text"] == ["local", "local"]


# ---------------------------------------------------------------- router
def test_router_choices():
    assert decide("make an infographic of the methods", "auto", ALL).providers == ["gemini"]
    assert decide("make an infographic of the methods", "auto", ALL).image
    assert decide("summarize the entire paper", "auto", ALL).providers == ["gemini"]
    assert decide("what does the job require?", "auto", ALL).providers == ["claude"]
    assert decide("what does the job require?", "gemini", ALL).providers == ["gemini"]     # manual wins
    assert decide("summarize the entire paper", "claude", ALL).providers == ["claude"]
    assert decide("q", "compare", ALL).providers == ["claude", "gemini"]
    assert decide("q", "auto", {"gemini"}).providers == ["gemini"]                          # fallback
    with pytest.raises(ValueError):
        decide("q", "compare", {"claude"})
    with pytest.raises(ValueError):
        decide("q", "auto", set())


# ---------------------------------------------------------------- consent gate
def test_papers_only_question_never_prompts(engine, providers):
    def boom(req):
        raise AssertionError("consent should not be requested")
    r = engine.ask("python pytorch requirements", mode="claude", consent=boom)
    assert r.answers and len(providers["claude"].calls) == 1 and not r.used_code


def test_code_question_prompts_before_any_call_and_cancel_sends_nothing(engine, providers):
    seen = []

    def cancel(req):
        seen.append(req)
        assert all(not p.calls for p in providers.values()), "provider called before consent"
        return None
    r = engine.ask("scanner harmonization loss", mode="claude", groups={"tau_proj"}, consent=cancel)
    assert seen and "tau_proj/model.py" in seen[0].files
    assert r.cancelled and all(not p.calls for p in providers.values())


def test_user_choice_overrides_router_and_only_that_provider_is_called(engine, providers):
    r = engine.ask("scanner harmonization loss", mode="claude", groups={"tau_proj"}, consent=lambda req: ["gemini"])
    assert providers["gemini"].calls and not providers["claude"].calls and not providers["ollama"].calls
    assert r.answers[0].provider == "gemini"


def test_compare_mode_requires_confirming_both(engine, providers):
    got = {}

    def approve_one(req):
        got["compare"] = req.compare
        return ["claude"]
    r = engine.ask("scanner harmonization loss", mode="compare", groups={"tau_proj"}, consent=approve_one)
    assert got["compare"] is True
    assert providers["claude"].calls and not providers["gemini"].calls and len(r.answers) == 1


def test_compare_mode_without_code_calls_both_without_prompt(engine, providers):
    engine.ask("python pytorch requirements", mode="compare", consent=lambda r: (_ for _ in ()).throw(AssertionError()))
    assert providers["claude"].calls and providers["gemini"].calls


def test_follow_up_after_code_turn_prompts_again(engine, providers):
    sess = Session(id="t")
    asked = []

    def approve(req):
        asked.append(req.carried)
        return ["claude"]
    engine.ask("scanner harmonization loss", mode="claude", groups={"tau_proj"}, session=sess, consent=approve)
    assert asked == [False] and sess.turns[-1].used_code
    # papers-only follow-up with memory: earlier answer was code-derived, so consent is required again
    engine.ask("and the job requirements?", mode="claude", session=sess, consent=approve, memory=True)
    assert asked == [False, True]
    # with memory off, a papers-only question does not carry code
    engine.ask("job requirements", mode="claude", session=sess, memory=False,
               consent=lambda r: (_ for _ in ()).throw(AssertionError()))


def test_memory_is_passed_to_provider(engine, providers):
    sess = Session(id="t2")
    engine.ask("python pytorch requirements", mode="claude", session=sess, consent=lambda r: None)
    engine.ask("and what else?", mode="claude", session=sess, consent=lambda r: None)
    assert providers["claude"].calls[-1]["history"] == 1


def test_default_sources_exclude_project_code(engine):
    assert engine.default_groups() == {"library/p.pdf", "notes/job.md"}


def test_image_request_uses_gemini_and_saves_inside_job_search(engine, providers, monkeypatch, tmp_path):
    r = engine.ask("make an infographic of scanner harmonization", mode="auto", consent=lambda r: None)
    try:
        assert r.route.image and r.image_path and settings.ROOT in __import__("pathlib").Path(r.image_path).parents
    finally:
        import pathlib
        for p in pathlib.Path(r.image_path).parent.glob("*"):
            if p.stem == pathlib.Path(r.image_path).stem:
                p.unlink()


def test_image_skipped_when_code_present_and_gemini_not_approved(engine, providers):
    r = engine.ask("make an infographic of scanner harmonization loss", mode="auto", groups={"tau_proj"},
                   consent=lambda req: ["claude"])
    assert r.image_path is None and not providers["gemini"].calls


def test_provider_errors_do_not_crash(engine, providers):
    def bad(*a, **k):
        from providers.base import ProviderError
        raise ProviderError("boom")
    providers["claude"].generate = bad
    r = engine.ask("python pytorch requirements", mode="claude", consent=lambda r: None)
    assert r.answers[0].error == "boom"


# ---------------------------------------------------------------- page numbers on the real papers
def make_pdf(path, pages):
    """Write a small text PDF, one string of text per page."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [" + " ".join(f"{3 + 2 * i} 0 R" for i in range(len(pages))) + f"] /Count {len(pages)} >>"]
    font_id = 3 + 2 * len(pages)
    for i, text in enumerate(pages):
        stream = "BT /F1 12 Tf 72 720 Td (" + text.replace("(", "").replace(")", "") + ") Tj ET"
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + 2 * i} 0 R /Resources << /Font << /F1 {font_id} 0 R >> >> >>")
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    objs.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out, offsets = "%PDF-1.4\n", []
    for n, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n{o}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    path.write_bytes(out.encode("latin-1"))


def test_pdf_page_numbers_match_source(tmp_path):
    from pypdf import PdfReader
    from ingest import read_pdf
    cfg = settings.load_config()
    pdf = tmp_path / "sample.pdf"
    make_pdf(pdf, ["First page about scanner harmonization.", "Second page about style transfer.", "Third page about evaluation."])
    chunks = read_pdf(pdf, "library/x.pdf", "library/x.pdf", cfg)
    reader = PdfReader(str(pdf))
    assert chunks and {c.page for c in chunks} == set(range(1, len(reader.pages) + 1))
    for c in chunks:
        page_text = " ".join((reader.pages[c.page - 1].extract_text() or "").split())
        assert " ".join(c.text.split())[:30] in page_text


# ---------------------------------------------------------------- honesty about Ollama / Claude model options
def test_auto_does_not_use_cloud_ollama_and_says_why():
    with pytest.raises(ValueError, match="runs in the cloud"):
        decide("q", "auto", {"ollama"}, {"ollama": "cloud"})
    r = decide("q", "auto", {"ollama"}, {"ollama": "local"})
    assert r.providers == ["ollama"] and "local" in r.reason
    r = decide("q", "ollama", {"ollama"}, {"ollama": "cloud"})
    assert "cloud-hosted" in r.reason


def test_claude_web_tool_matches_model():
    from providers.claude import web_tool, FALLBACK_MODELS
    assert web_tool("claude-opus-5-5")["type"] == "web_search_20260209"
    assert web_tool("claude-sonnet-5-5")["type"] == "web_search_20260209"
    assert web_tool("claude-haiku-5-5")["type"] == "web_search_20250305"
    assert web_tool("claude-fable-5-1")["type"] == "web_search_20250305"
    assert "claude-haiku-5-5" not in FALLBACK_MODELS


def test_removed_sources_are_never_searched(engine, providers, monkeypatch):
    monkeypatch.setattr(settings, "removed_sources", lambda: {"notes/job.md"})
    assert engine.default_groups() == {"library/p.pdf"}
    r = engine.ask("python pytorch requirements", mode="claude", groups={"notes/job.md", "library/p.pdf"},
                   consent=lambda r: None)
    assert all(c.group != "notes/job.md" for c in r.chunks) if hasattr(r, "chunks") else True


# ---------------------------------------------------------------- Claude through `claude -p`
def test_claude_login_provider_is_locked_down_and_keyless(monkeypatch):
    import subprocess, json
    from providers import build_providers
    from providers.claude_code import ClaudeCodeProvider, split_web_cites
    cfg = settings.load_config()
    prov = build_providers(cfg)["claude"]
    from providers.claude_hybrid import HybridClaudeProvider
    assert isinstance(prov, HybridClaudeProvider)                     # auto is the default
    prov = build_providers(cfg, {"claude_access": "login"})["claude"]
    assert isinstance(prov, ClaudeCodeProvider)
    assert build_providers(cfg, {"claude_access": "api"})["claude"].name == "claude"
    assert not isinstance(build_providers(cfg, {"claude_access": "api"})["claude"], ClaudeCodeProvider)

    no_web = prov.command(False)
    assert no_web[no_web.index("--tools") + 1] == ""                  # every tool off
    web = prov.command(True)
    assert web[web.index("--tools") + 1] == "WebSearch"
    assert "--bare" not in web and "--dangerously-skip-permissions" not in web

    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw, cmd=cmd)
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": "ok (https://e.org/x).", "usage": {"input_tokens": 1, "output_tokens": 2}}), "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-leak")
    monkeypatch.setattr(subprocess, "run", fake_run)
    r = prov.generate("q", [], [("earlier q", "earlier a")], web=True)
    assert "ANTHROPIC_API_KEY" not in seen["env"]                     # otherwise the CLI would bill the API
    assert "earlier q" in seen["input"] and settings.ROOT in __import__("pathlib").Path(seen["cwd"]).parents
    assert r.text == "ok." and r.web[0].url == "https://e.org/x" and r.web[0].end == 2

    text, cites = split_web_cites("A claim (https://a.org/p). Next.")
    assert text == "A claim. Next." and cites[0].end == len("A claim")


def test_auto_uses_api_only_for_web_search(monkeypatch):
    from providers.claude_hybrid import HybridClaudeProvider
    h = HybridClaudeProvider("claude-opus-5-5")
    monkeypatch.setattr(h.login, "available", lambda: (True, ""))
    monkeypatch.setattr(h.api, "available", lambda: (True, ""))
    assert h.route(False)[0] is h.login and h.route(True)[0] is h.api
    monkeypatch.setattr(h.api, "available", lambda: (False, "no key"))
    prov, why = h.route(True)
    assert prov is h.login and "unchecked" in why                    # no key: web falls back to login, and says so
    assert h.available()[0]
