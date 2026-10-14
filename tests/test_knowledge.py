"""Project knowledge: built once by Claude, checked against the code, kept current, and used automatically."""
import pytest

import knowledge
import settings
from knowledge import sections, select, verify_citations
from projectctx import ProjectFile
from tests.test_wholeproject import SENTINEL, build

DOC = """# Project knowledge: proj

## Overview
A demo project about scanners.

## Architecture and data flow
Data flows from the loader to the model.

## Files
### src/model.py
Defines the VAE and vae_loss [src/model.py:1-2].

### src/loader.py
Reads the scans and normalises them.

## Experiments, results and numbers
The wide model scored 0.0108 and the winner 0.0093.

## Glossary
wide: doubled channels.
"""


def put(name="proj"):
    knowledge.save(name, DOC, {"project": name, "built_at": "2026-01-01 00:00", "hashes": {}})


# ---------------------------------------------------------------- the document
def test_sections_split_by_heading_and_files_by_file():
    heads = [h for h, _ in sections(DOC)]
    assert heads == ["Overview", "Architecture and data flow", "Files > src/model.py", "Files > src/loader.py",
                     "Experiments, results and numbers", "Glossary"]


def test_select_always_has_the_overview_and_the_matching_sections():
    put()
    chunks = select("proj", "what was the winner score of the wide model?")
    heads = [c.section.removeprefix(knowledge.KNOWLEDGE_PREFIX) for c in chunks]
    assert heads[0] == "Overview" and "Experiments, results and numbers" in heads
    assert "Files > src/loader.py" not in heads                       # nothing matched it
    assert all(c.kind == "code" and c.source == "proj/(project knowledge)" and "(project knowledge)" in c.label() for c in chunks)


def test_select_respects_the_size_limit_and_missing_knowledge():
    put()
    assert len(select("proj", "wide winner model scanner", max_chars=60)) == 1       # only the overview fits
    assert select("other", "anything") == []


def test_citations_are_checked_against_the_files():
    files = {"a.py": ProjectFile("p", "a.py", "one\ntwo\nthree\n")}
    doc, ok, bad = verify_citations("Real [a.py:2-3]. Past end [a.py:9-12]. Unknown [b.py:1-1]. Single [a.py:1].", files)
    assert (ok, bad) == (2, 2)
    assert "[a.py:9-12 (unverified)]" in doc and "[b.py:1-1 (unverified)]" in doc and "[a.py:2-3]" in doc


def test_a_project_name_cannot_escape_the_knowledge_folder():
    for bad in ("../x", "a/b", ""):
        with pytest.raises(ValueError):
            knowledge.save(bad, "x", {})


# ---------------------------------------------------------------- building
def test_nothing_is_sent_before_consent_and_cancel_saves_nothing(tmp_path, providers):
    eng = build(tmp_path, providers)
    seen = []

    def deny(req):
        seen.append(req)
        assert all(not p.calls for p in providers.values())
        return None
    ok, msg = eng.build_knowledge("proj", deny)
    assert not ok and "Cancelled" in msg and knowledge.load("proj") is None
    assert seen[0].scope == "knowledge" and "will read" in seen[0].detail and "proj/src/model.py" in seen[0].files
    assert not any("patient" in f for f in seen[0].files)
    assert all(not p.calls for p in providers.values())


def test_only_claude_and_ollama_are_offered_to_read_it_never_gemini(tmp_path, providers):
    got = {}

    def look(req):
        got.update({o.key: o.available for o in req.options})
        return None
    build(tmp_path, providers).build_knowledge("proj", look)
    assert got == {"claude": True, "gemini": False, "ollama": True}


def test_build_saves_a_checked_document_and_sends_numbered_clean_files(tmp_path, providers):
    eng = build(tmp_path, providers)
    ok, msg = eng.build_knowledge("proj", lambda req: ["claude"])
    prompt = providers["claude"].calls[0]["prompt"]
    assert ok and "citations checked" in msg and not providers["gemini"].calls
    assert "   1| def vae_loss" in prompt and "# Demo" in prompt                     # numbered lines
    for private in (SENTINEL, "abcdefghijklmnop", "hunter2hunter2", "zzzzzzzzzzzz"):
        assert private not in prompt
    doc, info = knowledge.load("proj")
    assert settings.TOOL_DIR in knowledge._paths("proj")[0].parents
    assert "(unverified)" in doc and info["verified"] > 0 and info["unverified"] == 1       # the page-9999 citation was caught
    assert set(info["hashes"]) == {"README.md", "src/model.py", "src/notes.txt"}


def test_status_notices_changed_added_and_removed_files_and_update_sends_only_those(tmp_path, providers):
    eng = build(tmp_path, providers)
    eng.build_knowledge("proj", lambda req: ["claude"])
    assert not knowledge.status(eng.cfg, "proj").stale
    proj = tmp_path / "proj"
    (proj / "src" / "model.py").write_text("def vae_loss():\n    return 2\n")
    (proj / "src" / "new.py").write_text("x = 1\n")
    (proj / "src" / "notes.txt").unlink()
    st = knowledge.status(eng.cfg, "proj")
    assert st.changed == ["src/model.py"] and st.added == ["src/new.py"] and st.removed == ["src/notes.txt"] and st.n_changed == 3
    ok, msg = eng.build_knowledge("proj", lambda req: ["claude"], update=True)
    prompt = providers["claude"].calls[-1]["prompt"]
    assert ok and msg.startswith("Updated")
    assert "return 2" in prompt and "x = 1" in prompt and "# Demo" not in prompt             # unchanged files are not sent again
    assert "CURRENT DOCUMENT" in prompt and "- src/notes.txt" in prompt
    assert not knowledge.status(eng.cfg, "proj").stale


def test_update_with_no_changes_sends_nothing(tmp_path, providers):
    eng = build(tmp_path, providers)
    eng.build_knowledge("proj", lambda req: ["claude"])
    n = len(providers["claude"].calls)
    ok, msg = eng.build_knowledge("proj", lambda req: ["claude"], update=True)
    assert not ok and "Nothing has changed" in msg and len(providers["claude"].calls) == n


def test_a_project_too_large_for_one_pass_is_read_in_areas_after_asking(tmp_path, providers):
    eng = build(tmp_path, providers)
    eng.cfg["whole_project"]["max_tokens"]["claude"] = 10
    eng.cfg["deep_read"] = {"area_tokens": 20, "workers": 2}
    seen = []
    ok, msg = eng.build_knowledge("proj", lambda req: (seen.append(req), ["claude"])[1])
    assert ok and "Deep read" in msg and seen and "areas" in seen[0].detail and "lot of your Claude plan" in seen[0].detail
    assert knowledge.load("proj") is not None


def test_a_bad_reply_is_not_saved(tmp_path, providers):
    from providers.base import ProviderResult
    providers["claude"].complete = lambda *a, **k: ProviderResult(text="Sorry, I cannot do that.")
    ok, msg = build(tmp_path, providers).build_knowledge("proj", lambda req: ["claude"])
    assert not ok and "usable document" in msg and knowledge.load("proj") is None


def test_knowledge_needs_the_claude_login(tmp_path, providers):
    from providers.claude import ClaudeProvider
    providers["claude"] = ClaudeProvider("claude-opus-5-5")
    ok, msg = build(tmp_path, providers).build_knowledge("proj", lambda req: pytest.fail("must not ask"))
    assert not ok and "Claude Code login" in msg


def test_delete_removes_both_files(tmp_path, providers):
    put()
    knowledge.delete("proj")
    assert knowledge.load("proj") is None and not list(settings.KNOWLEDGE_DIR.glob("proj.*"))


# ---------------------------------------------------------------- using it
def built(tmp_path, providers):
    eng = build(tmp_path, providers)
    assert eng.build_knowledge("proj", lambda req: ["claude"])[0]
    providers["claude"].calls.clear()
    return eng


def test_questions_use_the_knowledge_automatically_and_the_prompt_lists_it(tmp_path, providers):
    eng = built(tmp_path, providers)
    asked = []
    r = eng.ask("how does the vae_loss work?", mode="claude", groups={"proj"}, consent=lambda req: asked.append(req) or ["claude"])
    call = providers["claude"].calls[0]
    assert any("(project knowledge)" in ref for ref in call["refs"]) and "A demo project with 3 files" in call["text"]
    assert "proj/(project knowledge)" in asked[0].files
    assert any("Using project knowledge for proj" in n for n in r.notes)
    assert r.used_code


def test_a_changed_file_adds_an_out_of_date_note(tmp_path, providers):
    eng = built(tmp_path, providers)
    (tmp_path / "proj" / "src" / "model.py").write_text("def vae_loss():\n    return 3\n")
    r = eng.ask("how does the vae_loss work?", mode="claude", groups={"proj"}, consent=lambda req: ["claude"])
    assert any("1 files changed since" in n for n in r.notes)


def test_knowledge_can_be_switched_off_and_is_not_added_to_whole_or_explore(tmp_path, providers):
    eng = built(tmp_path, providers)
    eng.ask("q", mode="claude", groups={"proj"}, consent=lambda req: ["claude"], use_knowledge=False)
    assert not any("(project knowledge)" in ref for ref in providers["claude"].calls[-1]["refs"])
    eng.ask("q", mode="claude", groups={"proj"}, consent=lambda req: ["claude"], code_mode="whole")
    assert not any("(project knowledge)" in ref for ref in providers["claude"].calls[-1]["refs"])


def test_projects_without_knowledge_are_unchanged(tmp_path, providers):
    eng = build(tmp_path, providers)
    r = eng.ask("how does the vae_loss work?", mode="claude", groups={"proj"}, consent=lambda req: ["claude"])
    assert not any("project knowledge" in n for n in r.notes)
