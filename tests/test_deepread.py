"""Deep read: a large project is read in areas by several Claude workers, then merged into one project-knowledge document."""
import threading
import time
from types import SimpleNamespace

import pytest

import deepread
import knowledge
import settings
from projectctx import ProjectFile
from providers.base import ProviderError
from providers.fake import FakeProvider
from tests.test_wholeproject import build


def pf(rel, text="x = 1\n" * 40):
    return ProjectFile("proj", rel, text)


def files(*rels, text="x = 1\n" * 40):
    return {r: pf(r, text) for r in rels}


# ---------------------------------------------------------------- 1. split
def test_areas_keep_folders_together_and_respect_the_budget():
    fs = files("a/one.py", "a/two.py", "b/one.py", "b/two.py", "readme.md")
    one = fs["a/one.py"].tokens
    areas = deepread.plan_areas(fs, budget=one * 2)
    assert [[f.rel for f in a.files] for a in areas] == [["a/one.py", "a/two.py"], ["b/one.py", "b/two.py"], ["readme.md"]]
    assert all(a.tokens <= one * 2 for a in areas)


def test_a_file_bigger_than_the_budget_gets_its_own_area_and_nothing_is_dropped():
    fs = {**files("a/small.py"), "a/huge.py": pf("a/huge.py", "y = 2\n" * 4000)}
    areas = deepread.plan_areas(fs, budget=100)
    assert sorted(f.rel for a in areas for f in a.files) == ["a/huge.py", "a/small.py"]


def test_area_keys_change_only_when_the_files_change():
    fs = files("a/one.py", "b/one.py")
    k1 = [a.key for a in deepread.plan_areas(fs, 10_000)]
    assert k1 == [a.key for a in deepread.plan_areas(files("a/one.py", "b/one.py"), 10_000)]
    assert k1 != [a.key for a in deepread.plan_areas(files("a/one.py", "b/one.py", text="x = 2\n" * 40), 10_000)]


# ---------------------------------------------------------------- 2. read
def test_notes_must_have_the_marker_parts():
    ok = "=== FILES ===\n### a.py\nx\n=== SUMMARY ===\ns\n=== RESULTS ===\nnot stated"
    n = deepread.parse_notes(ok)
    assert n["FILES"].startswith("### a.py") and n["RESULTS"] == "not stated" and n["GLOSSARY"] == ""
    for bad in ("no markers at all", "=== SUMMARY ===\nonly a summary", "=== FILES ===\nonly files"):
        with pytest.raises(ValueError):
            deepread.parse_notes(bad)


def test_a_file_the_worker_forgot_is_listed_not_dropped(tmp_path):
    class Forgetful:
        def complete(self, system, prompt, timeout=None):
            return SimpleNamespace(text="=== FILES ===\n### a.py\ndescribed\n=== SUMMARY ===\ns", model="m")
    area = deepread.Area("k", "label", [pf("a.py"), pf("b.py")])
    notes, _ = deepread.read_area(Forgetful(), "proj", area, 5)
    assert "### b.py\nNot described by the reader" in notes["FILES"]


def test_a_worker_gets_one_retry_on_an_error_and_on_a_reply_without_markers():
    calls = []

    class Flaky:
        def complete(self, system, prompt, timeout=None):
            calls.append(prompt)
            if len(calls) == 1:
                raise ProviderError("busy")
            if len(calls) == 2:
                return SimpleNamespace(text="no markers", model="m")
            return SimpleNamespace(text="=== FILES ===\n### a.py\nd\n=== SUMMARY ===\ns", model="m")
    notes, _ = deepread.read_area(Flaky(), "proj", deepread.Area("k", "l", [pf("a.py")]), 5)
    assert len(calls) == 3 and notes["SUMMARY"] == "s" and "marker lines" in calls[2]


# ---------------------------------------------------------------- the whole run
def run(tmp_path, providers, **cfg_extra):
    eng = build(tmp_path, providers)
    eng.cfg["deep_read"] = {"area_tokens": 6, "workers": 2, **cfg_extra}
    return eng


def test_the_document_has_the_standard_sections_and_every_file(tmp_path, providers):
    eng = run(tmp_path, providers)
    fs = knowledge.current_files(eng.cfg, "proj")
    res = deepread.deep_read(providers["claude"], eng.cfg, "proj", fs)
    heads = [h for h, _ in knowledge.sections(res.doc)]
    assert heads[0] == "Overview" and "Architecture and data flow" in heads
    for rel in fs:
        assert f"### {rel}" in res.doc
    assert knowledge.looks_like_a_document(res.doc) and res.areas >= 2 and res.calls == res.areas + 1


def test_progress_is_reported_and_workers_run_in_parallel_but_never_more_than_allowed(tmp_path, providers):
    eng = run(tmp_path, providers, workers=2)
    fs = knowledge.current_files(eng.cfg, "proj")
    live, peak, lock = [0], [0], threading.Lock()
    real = providers["claude"].complete

    def slow(system, prompt, timeout=None):
        if system.startswith("You are one of several readers"):
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            time.sleep(0.05)
            with lock:
                live[0] -= 1
        return real(system, prompt, timeout)
    providers["claude"].complete = slow
    seen = []
    deepread.deep_read(providers["claude"], eng.cfg, "proj", fs, progress=lambda d, t, label: seen.append((d, t)))
    assert seen[0][0] == 0 and seen[-1][0] == seen[-1][1] and peak[0] <= 2


def test_a_stopped_run_continues_and_an_update_reads_only_changed_areas(tmp_path, providers):
    eng = run(tmp_path, providers, workers=1)
    fs = knowledge.current_files(eng.cfg, "proj")
    real = providers["claude"].complete
    n = {"areas": 0}

    def dies_on_second(system, prompt, timeout=None):
        if system.startswith("You are one of several readers"):
            n["areas"] += 1
            if n["areas"] >= 2:
                raise ProviderError("limit reached")
        return real(system, prompt, timeout)
    providers["claude"].complete = dies_on_second
    with pytest.raises(ProviderError):
        deepread.deep_read(providers["claude"], eng.cfg, "proj", fs)
    saved = deepread.load_cache("proj")
    assert len(saved) == 1, "the finished area is kept"
    providers["claude"].complete = real
    res = deepread.deep_read(providers["claude"], eng.cfg, "proj", fs)
    assert res.reused == 1 and res.calls == res.areas - 1 + 1
    again = deepread.deep_read(providers["claude"], eng.cfg, "proj", fs)
    assert again.reused == again.areas and again.calls == 1, "nothing changed: only the final overview is written"


def test_a_failed_overview_writes_nothing_and_keeps_the_notes(tmp_path, providers):
    eng = run(tmp_path, providers)
    fs = knowledge.current_files(eng.cfg, "proj")
    real = providers["claude"].complete
    providers["claude"].complete = lambda s, p, t=None: SimpleNamespace(text="no sections", model="m") if s.startswith("You are the final reader") else real(s, p, t)
    with pytest.raises(ValueError, match="overview"):
        deepread.deep_read(providers["claude"], eng.cfg, "proj", fs)
    assert deepread.load_cache("proj") and knowledge.load("proj") is None


def test_a_project_over_the_hard_limit_is_refused_before_anything_is_sent(tmp_path, providers):
    eng = run(tmp_path, providers, max_tokens=5)
    fs = knowledge.current_files(eng.cfg, "proj")
    with pytest.raises(ValueError, match="deep-read limit"):
        deepread.deep_read(providers["claude"], eng.cfg, "proj", fs)
    assert not providers["claude"].calls


# ---------------------------------------------------------------- through the engine
def test_the_consent_request_names_the_areas_and_gemini_may_not_read_code(tmp_path, providers):
    eng = run(tmp_path, providers)
    eng.cfg["whole_project"]["max_tokens"]["claude"] = 10
    req, send = eng.knowledge_request("proj")
    assert "areas" in req.detail and f"{len(send)} files" in req.detail
    assert [o.key for o in req.options if o.available] == ["claude", "ollama"]


def test_nothing_is_sent_when_the_user_cancels_a_deep_read(tmp_path, providers):
    eng = run(tmp_path, providers)
    eng.cfg["whole_project"]["max_tokens"]["claude"] = 10
    ok, msg = eng.build_knowledge("proj", lambda req: None)
    assert not ok and "Cancelled" in msg and not providers["claude"].calls and knowledge.load("proj") is None


def test_updating_a_deep_built_project_reads_only_what_changed(tmp_path, providers):
    eng = run(tmp_path, providers)
    eng.cfg["whole_project"]["max_tokens"]["claude"] = 10
    ok, _ = eng.build_knowledge("proj", lambda req: ["claude"])
    assert ok
    ok, msg = eng.build_knowledge("proj", lambda req: pytest.fail("nothing changed, so nothing is asked"), update=True)
    assert not ok and "Nothing has changed" in msg
    proj = settings.resolve(next(p for p in eng.cfg["projects"] if p["name"] == "proj")["path"])
    (proj / "src" / "model.py").write_text("def vae_loss(a):\n    return a * 2\n")
    before = len(providers["claude"].calls)
    ok, msg = eng.build_knowledge("proj", lambda req: ["claude"], update=True)
    assert ok and "reused from before" in msg
    area_calls = [c for c in providers["claude"].calls[before:] if c["system"].startswith("You are one of several readers")]
    assert 1 <= len(area_calls) < len(deepread.plan_areas(knowledge.current_files(eng.cfg, "proj"), 6))


def test_deleting_the_knowledge_deletes_the_deep_read_notes_too(tmp_path, providers):
    eng = run(tmp_path, providers)
    eng.cfg["whole_project"]["max_tokens"]["claude"] = 10
    eng.build_knowledge("proj", lambda req: ["claude"])
    assert deepread.has_cache("proj")
    knowledge.delete("proj")
    assert not deepread.has_cache("proj") and knowledge.load("proj") is None


# ---------------------------------------------------------------- reading with Ollama instead of Claude
def test_ollama_complete_sends_the_prompt_and_context_size_and_returns_the_text(monkeypatch):
    from providers import ollama
    seen = {}

    class R:
        def raise_for_status(self): pass
        def json(self): return {"message": {"content": "notes"}}

    def post(url, json=None, timeout=None):
        seen.update(url=url, body=json, timeout=timeout)
        return R()
    monkeypatch.setattr(ollama.requests, "post", post)
    monkeypatch.setattr(ollama, "installed_models", lambda *a, **k: ["m1"])
    p = ollama.OllamaProvider("m1")
    out = p.complete("SYS", "USER", 30, num_ctx=8192)
    b = seen["body"]
    assert out.text == "notes" and out.model == "m1" and seen["url"].endswith("/api/chat") and seen["timeout"] == 30
    assert b["stream"] is False and b["options"]["num_ctx"] == 8192 and [m["role"] for m in b["messages"]] == ["system", "user"]
    assert b["messages"][0]["content"] == "SYS" and b["messages"][1]["content"] == "USER"


def test_ollama_errors_become_a_provider_error(monkeypatch):
    from providers import ollama
    monkeypatch.setattr(ollama.requests, "post", lambda *a, **k: (_ for _ in ()).throw(OSError("refused")))
    monkeypatch.setattr(ollama, "installed_models", lambda *a, **k: ["m1"])
    with pytest.raises(ProviderError, match="Ollama error"):
        ollama.OllamaProvider("m1").complete("s", "p")


def test_a_deep_read_with_ollama_uses_only_ollama_and_always_reads_in_areas(tmp_path, providers):
    eng = run(tmp_path, providers)                                    # the project is small: Claude would read it in one request
    eng.cfg["deep_read"]["ollama"] = {"area_tokens": 6, "workers": 1, "num_ctx": 4096}
    seen = []
    ok, msg = eng.build_knowledge("proj", lambda req: (seen.append(req), ["ollama"])[1], provider="ollama")
    assert ok and "Deep read" in msg
    assert providers["ollama"].calls and not providers["claude"].calls and not providers["gemini"].calls
    assert "Ollama" in seen[0].detail and "on this machine" in seen[0].detail
    assert any(c["system"].startswith("You are one of several readers") for c in providers["ollama"].calls)


def test_ollama_cloud_models_are_described_as_leaving_the_machine(tmp_path, providers):
    eng = run(tmp_path, providers)
    providers["ollama"].locality_value = "cloud"
    req, _ = eng.knowledge_request("proj", provider="ollama")
    assert "leaves this machine" in req.detail


def test_approving_the_wrong_provider_sends_nothing(tmp_path, providers):
    eng = run(tmp_path, providers)
    ok, msg = eng.build_knowledge("proj", lambda req: ["claude"], provider="ollama")
    assert not ok and "Cancelled" in msg and not providers["ollama"].calls and not providers["claude"].calls


def test_an_unavailable_ollama_is_reported_and_nothing_is_asked(tmp_path, providers):
    eng = run(tmp_path, providers)
    providers["ollama"].available = lambda: (False, "Ollama is not running or has no models.")
    ok, msg = eng.build_knowledge("proj", lambda req: pytest.fail("must not ask"), provider="ollama")
    assert not ok and "Ollama is not available" in msg


def test_the_final_request_is_shortened_when_the_model_holds_little_and_the_document_says_so(tmp_path, providers):
    eng = run(tmp_path, providers)
    fs = knowledge.current_files(eng.cfg, "proj")
    areas, _ = deepread.plan_work(eng.cfg, "proj", fs)
    notes = {a.key: {"FILES": "### x", "SUMMARY": "s" * 3000, "RESULTS": "r" * 3000, "DECISIONS": "d" * 3000, "CAVEATS": "c" * 3000, "GLOSSARY": "g" * 3000}
             for a in areas}
    full, cut = deepread.fit_merge("proj", areas, notes, 10_000_000)
    assert not cut and "GLOSSARY:" in full
    small, cut = deepread.fit_merge("proj", areas, notes, 1500)
    assert cut and len(small) < len(full) and "GLOSSARY:" not in small
    res = deepread.deep_read(providers["claude"], eng.cfg, "proj", fs, merge_tokens=20)
    assert "written from shortened notes" in res.doc
