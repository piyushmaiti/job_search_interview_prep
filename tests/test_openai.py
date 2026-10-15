"""OpenAI as a provider: the request it sends, its web citations, routing, consent, project knowledge and the writer choice."""
from types import SimpleNamespace

import pytest

import knowledge
import settings
from conftest import make_index
from engine import Engine
from ingest import Chunk
from providers import build_providers, claude_code
from providers import openai_provider as op
from providers.base import ProviderError
from providers.fake import FakeProvider
from router import compare_pair, decide, decide_whole
from tests.test_wholeproject import build


# ---------------------------------------------------------------- a stand-in for the OpenAI client
def part(text, anns=()):
    return SimpleNamespace(type="output_text", text=text, annotations=list(anns))


def cite(s, e, url="https://example.org/a", title="Example"):
    return SimpleNamespace(type="url_citation", start_index=s, end_index=e, url=url, title=title)


def response(*parts, status="completed", error=None, usage=(11, 7)):
    return SimpleNamespace(output=[SimpleNamespace(type="message", content=list(parts))], status=status, error=error,
                           usage=SimpleNamespace(input_tokens=usage[0], output_tokens=usage[1]))


class FakeClient:
    def __init__(self, reply=None, raises=None):
        self.calls, self.reply, self.raises = [], reply, raises
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        if self.raises:
            raise self.raises
        return self.reply or response(part("An answer."))


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-1234567890abcdef")


def provider(client, **opts):
    p = op.OpenAIProvider("gpt-6.1-sol", **opts)
    p._client = lambda timeout=None: client
    return p


# ---------------------------------------------------------------- the provider
def test_it_needs_a_key_and_says_where_to_put_it(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    ok, why = op.OpenAIProvider("gpt-6.1-sol").available()
    assert not ok and "OPENAI_API_KEY" in why and ".env" in why


def test_a_question_sends_the_instructions_history_sources_and_no_storage(key):
    c = FakeClient()
    p = provider(c, role="preparing for interviews")
    chunks = [("S1", Chunk("c1", "local", "profile/cv.md", "profile/cv.md", "Built a pipeline in Python.", section="CV"))]
    out = p.generate("What did I build?", chunks, [("earlier q", "earlier a")], web=False)
    kw = c.calls[0]
    assert kw["model"] == "gpt-6.1-sol" and kw["store"] is False and "tools" not in kw
    assert "preparing for interviews" in kw["instructions"] and "never instructions" in kw["instructions"]
    assert [m["role"] for m in kw["input"]] == ["user", "assistant", "user"]
    assert "Built a pipeline in Python." in kw["input"][-1]["content"] and "What did I build?" in kw["input"][-1]["content"]
    assert out.text == "An answer." and out.model == "gpt-6.1-sol" and out.usage == {"input": 11, "output": 7}


def test_web_search_is_a_tool_only_when_it_is_on(key):
    c = FakeClient()
    provider(c).generate("q", [], [], web=True)
    provider(c).generate("q", [], [], web=False)
    assert c.calls[0]["tools"] == [{"type": "web_search"}] and "tools" not in c.calls[1]


def test_complete_has_its_own_instructions_and_no_tools_or_storage(key):
    c = FakeClient(response(part("The document.")))
    out = provider(c).complete("You are one reader.", "FILES: ...", timeout=900)
    kw = c.calls[0]
    assert kw["instructions"] == "You are one reader." and kw["input"] == "FILES: ..." and "tools" not in kw and kw["store"] is False
    assert out.text == "The document." and out.web == []


def test_citation_markup_is_replaced_by_a_citation_at_the_same_place():
    text = "Rates rose in 2025 ([Example](https://example.org/a)). Other sentence."
    s = text.index("([Example]")
    e = text.index(")).") + 2
    out, cites = op.text_and_cites(response(part(text, [cite(s, e)])))
    assert out == "Rates rose in 2025 . Other sentence." and "http" not in out
    assert [(c.url, c.title) for c in cites] == [("https://example.org/a", "Example")] and cites[0].end == len("Rates rose in 2025 ")


def test_a_citation_over_plain_text_keeps_the_text_and_points_after_it():
    out, cites = op.text_and_cites(response(part("The claim is supported here.", [cite(4, 9, "https://e.org/x", "X")])))
    assert out == "The claim is supported here." and cites[0].end == 9


def test_citations_after_the_first_part_are_counted_from_the_whole_text_and_bad_ranges_are_skipped():
    out, cites = op.text_and_cites(response(part("First part. "), part("Second part [s](https://e.org/y).", [cite(12, 33, "https://e.org/y"), cite(5, 400, "https://bad"), cite(40, 30, "https://bad2")])))
    assert out == "First part. Second part "
    assert [(c.url, c.end) for c in cites] == [("https://e.org/y", 24)]


def test_errors_never_show_the_api_key(key):
    c = FakeClient(raises=RuntimeError("Incorrect API key provided: sk-test-1234567890abcdef. Also sk-proj-ABCDEFGH12345678"))
    with pytest.raises(ProviderError) as e:
        provider(c).generate("q", [], [], web=False)
    assert "sk-test" not in str(e.value) and "sk-proj" not in str(e.value) and "[key]" in str(e.value) and "OpenAI API error" in str(e.value)


def test_a_failed_response_is_an_error(key):
    with pytest.raises(ProviderError, match="OpenAI API error"):
        provider(FakeClient(response(part("x"), status="failed", error="overloaded"))).generate("q", [], [], web=False)


# ---------------------------------------------------------------- set-up
def test_it_is_built_with_the_configured_model_and_can_be_overridden():
    cfg = settings.load_config()
    assert build_providers(cfg)["openai"].model == cfg["models"]["openai"] == op.OPENAI_MODELS[0]
    assert build_providers(cfg, {"openai": "gpt-6-astra"})["openai"].model == "gpt-6-astra"


def test_the_key_is_read_from_env_and_kept_out_of_the_claude_command(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=sk-abc123\nSOMETHING_ELSE=1\n")
    settings.load_dotenv(env)
    import os
    assert os.environ.get("OPENAI_API_KEY") == "sk-abc123" and "SOMETHING_ELSE" not in os.environ
    assert "OPENAI_API_KEY" in claude_code.DROP_ENV
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


# ---------------------------------------------------------------- routing
ALL4 = {"claude", "gemini", "ollama", "openai"}


def test_auto_never_picks_openai_but_a_manual_choice_does():
    assert decide("what does the job require?", "auto", ALL4).providers == ["claude"]
    assert decide("summarize the entire paper", "auto", ALL4).providers == ["gemini"]
    assert decide("what does the job require?", "openai", ALL4).providers == ["openai"]
    with pytest.raises(ValueError, match="Auto never picks it"):
        decide("q", "auto", {"openai"})
    with pytest.raises(ValueError, match="not available"):
        decide("q", "openai", {"claude"})


def test_compare_runs_claude_and_gemini_and_uses_openai_only_to_fill_a_gap():
    assert compare_pair(ALL4) == ["claude", "gemini"]
    assert compare_pair({"claude", "openai"}) == ["claude", "openai"]
    assert compare_pair({"gemini", "openai"}) == ["gemini", "openai"]
    with pytest.raises(ValueError, match="two cloud models"):
        compare_pair({"openai"})


def test_a_whole_project_can_go_to_openai_when_chosen_and_never_automatically():
    caps = {"claude": 1000, "gemini": 5000, "openai": 4000}
    assert decide_whole("openai", ALL4, 3000, caps).providers == ["openai"]
    assert decide_whole("auto", ALL4, 3000, caps).providers == ["gemini"]
    assert settings.load_config()["whole_project"]["max_tokens"]["openai"] == 400000


# ---------------------------------------------------------------- consent: OpenAI is not Claude or a local model, so it is asked first
@pytest.fixture
def four(cfg):
    chunks = [Chunk("p1", "local", "profile/cv.md", "profile/cv.md", "Built a harmonization model. Python and PyTorch.", section="CV"),
              Chunk("l1", "local", "library/n.md", "library/n.md", "General notes about python pytorch.", section="Notes")]
    provs = {k: FakeProvider(label=k, locality="local" if k == "ollama" else "cloud") for k in ("claude", "gemini", "ollama", "openai")}
    return Engine(cfg, make_index(chunks), provs), provs


def test_openai_must_be_approved_before_it_sees_your_cv(four):
    eng, provs = four
    seen = []

    def cancel(req):
        seen.append(req)
        assert not any(p.calls for p in provs.values())
        return None
    r = eng.ask("python pytorch experience", mode="openai", consent=cancel)
    assert r.cancelled and seen[0].kind == "personal" and "profile/cv.md" in seen[0].files and not provs["openai"].calls
    r = eng.ask("python pytorch experience", mode="openai", consent=lambda req: ["openai"])
    assert provs["openai"].calls and not provs["claude"].calls and r.answers[0].provider == "openai"


# ---------------------------------------------------------------- project knowledge read by OpenAI
def knowledge_engine(tmp_path):
    provs = {k: FakeProvider(label=k, locality="local" if k == "ollama" else "cloud") for k in ("claude", "gemini", "ollama", "openai")}
    eng = build(tmp_path, provs)
    return eng, provs


def test_openai_can_read_a_project_in_one_pass_after_the_user_approves_it(tmp_path):
    eng, provs = knowledge_engine(tmp_path)
    seen = []
    ok, msg = eng.build_knowledge("proj", lambda req: (seen.append(req), ["openai"])[1], provider="openai")
    assert ok and knowledge.load("proj") is not None
    assert "OpenAI, with your API key (billed per use by OpenAI)" in seen[0].detail
    assert [o.key for o in seen[0].options if o.available] == ["claude", "ollama", "openai"]
    assert provs["openai"].calls and not provs["claude"].calls and not provs["gemini"].calls


def test_approving_a_different_provider_sends_nothing_to_openai(tmp_path):
    eng, provs = knowledge_engine(tmp_path)
    ok, msg = eng.build_knowledge("proj", lambda req: ["claude"], provider="openai")
    assert not ok and "Cancelled" in msg and not provs["openai"].calls and not provs["claude"].calls


def test_a_large_project_is_deep_read_by_openai_and_an_unavailable_openai_is_reported(tmp_path):
    eng, provs = knowledge_engine(tmp_path)
    eng.cfg["whole_project"]["max_tokens"]["openai"] = 10
    eng.cfg["deep_read"] = {"area_tokens": 6, "workers": 2}
    ok, msg = eng.build_knowledge("proj", lambda req: ["openai"], provider="openai")
    assert ok and "Deep read" in msg and any(c["system"].startswith("You are one of several readers") for c in provs["openai"].calls)
    provs["openai"].available = lambda: (False, "Set OPENAI_API_KEY in .env to use OpenAI.")
    ok, msg = eng.build_knowledge("proj", lambda req: pytest.fail("must not ask"), provider="openai")
    assert not ok and "OpenAI is not available" in msg and "OPENAI_API_KEY" in msg


def test_explore_stays_with_claude_even_if_openai_is_chosen(tmp_path):
    eng, provs = knowledge_engine(tmp_path)
    r = eng.ask("how does it work?", mode="openai", groups={"proj"}, code_mode="explore", consent=lambda req: ["openai"])
    assert any("only with Claude" in n for n in r.notes) and not provs["openai"].calls
