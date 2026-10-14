"""Explore mode: the sanitized copy, the locked-down Claude command, checked citations, and consent."""
import json
import os
import subprocess

import pytest

import explore
import settings
from citations import render_explore
from engine import Engine
from index import Index, TfidfEmbedder, _doc_text
from ingest import Chunk
from providers.base import ProviderError
from providers.claude_code import ClaudeCodeProvider
from sessions import Session
from tests.test_wholeproject import SENTINEL, cfg_for, make_project


@pytest.fixture(autouse=True)
def scratch_stage(monkeypatch):
    monkeypatch.setattr(explore, "STAGE_DIR", settings.TOOL_DIR / ".test_scratch" / "explore")


def files_in(root):
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


# ---------------------------------------------------------------- the copy
def test_the_copy_holds_only_allowed_redacted_files_with_a_map(tmp_path):
    cfg = cfg_for(make_project(tmp_path), nifti_headers=True)
    root = explore.stage(cfg, ["proj"])
    names = files_in(root)
    assert "proj/README.md" in names and "proj/src/model.py" in names and "proj/PROJECT_MAP.md" in names
    assert "proj/DATA_FILES.txt" in names
    assert not [n for n in names if n.endswith((".nii", ".gz", ".jpg", ".png", ".npy", ".env"))]
    assert not [n for n in names if "patient" in n or "secret" in n or n.endswith("big.py")]
    text = "\n".join((root / n).read_text() for n in names)
    for private in (SENTINEL, "abcdefghijklmnop", "hunter2hunter2", "zzzzzzzzzzzz"):
        assert private not in text
    assert not [p for p in root.rglob("*") if p.is_symlink()]
    assert settings.TOOL_DIR in root.parents and "shape 82x64x130" in (root / "proj" / "DATA_FILES.txt").read_text()


def test_a_new_copy_replaces_the_old_one_and_cleanup_removes_it(tmp_path):
    cfg = cfg_for(make_project(tmp_path))
    root = explore.stage(cfg, ["proj"])
    (root / "leftover.txt").write_text("old")
    root2 = explore.stage(cfg, ["proj"])
    assert root2 == root and not (root / "leftover.txt").exists()
    explore.cleanup()
    assert not root.exists()


def test_plan_lists_files_without_copying(tmp_path):
    p = explore.plan(cfg_for(make_project(tmp_path)), ["proj"])
    assert "proj/src/model.py" in p.files and "proj/PROJECT_MAP.md" in p.files and p.tokens > 0
    assert not (explore.STAGE_DIR / "current").exists()


# ---------------------------------------------------------------- the command
def test_the_explore_command_can_only_read():
    cmd = ClaudeCodeProvider("claude-opus-5-5").explore_command()
    assert "--restricted" in cmd and cmd[cmd.index("--tools") + 1] == "Read,Grep,Glob"
    assert cmd[cmd.index("--allowedTools") + 1] == "Read,Grep,Glob"
    joined = " ".join(cmd)
    for banned in ("Bash", "Edit", "Write", "WebFetch", "WebSearch", "--dangerously-skip-permissions", "--add-dir", "--bare"):
        assert banned not in joined


def test_explore_runs_inside_the_copy_without_api_keys(tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw, cmd=cmd)
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": "done", "usage": {}}), "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-leak")
    monkeypatch.setattr(subprocess, "run", fake_run)
    res = ClaudeCodeProvider("claude-opus-5-5").explore("how does it work?", [], [("earlier", "answer")], tmp_path, timeout=77)
    assert seen["cwd"] == tmp_path and "ANTHROPIC_API_KEY" not in seen["env"] and seen["timeout"] == 77
    assert "how does it work?" in seen["input"] and "earlier" in seen["input"] and res.text == "done"


def test_explore_errors_and_timeouts_are_reported(tmp_path, monkeypatch):
    def timeout(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)
    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(ProviderError, match="ran out of time"):
        ClaudeCodeProvider("m").explore("q", [], [], tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "boom"))
    with pytest.raises(ProviderError, match="no usable answer"):
        ClaudeCodeProvider("m").explore("q", [], [], tmp_path)


# ---------------------------------------------------------------- citations
def test_citations_are_checked_against_the_copy(tmp_path):
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "a.py").write_text("one\ntwo\nthree\n")
    (tmp_path.parent / "outside.py").write_text("x\n")
    chunks = {"S1": Chunk("c1", "local", "g", "notes/n.md", "text", section="Intro")}
    segs = render_explore("Real [proj/a.py:2-3]. Line past the end [proj/a.py:9-12]. Missing file [proj/b.py:1-2]. "
                          "Escape [../outside.py:1-1]. Absolute [/etc/hosts.conf:1-1]. Source [S1]. Own knowledge [M].", tmp_path, chunks)
    kinds = [s.kind for s in segs if s.kind != "text"]
    assert kinds == ["code", "unverified", "unverified", "unverified", "unverified", "local", "model"]
    real = next(s for s in segs if s.kind == "code")
    assert real.text == "proj/a.py:2-3" and real.ref == "explore:proj/a.py:2-3" and real.detail == "two\nthree"


def test_single_line_citations_work(tmp_path):
    (tmp_path / "a.py").write_text("one\ntwo\n")
    seg = next(s for s in render_explore("See [a.py:2].", tmp_path, {}) if s.kind == "code")
    assert seg.text == "a.py:2-2" and seg.detail == "two"


# ---------------------------------------------------------------- engine, consent
def build(tmp_path, providers, **project_extra):
    cfg = cfg_for(make_project(tmp_path), **project_extra)
    emb = TfidfEmbedder()
    chunks = [Chunk("d1", "local", "docs/a.md", "docs/a.md", "Notes about imaging scanners and harmonization.")]
    idx = Index(chunks, emb.encode_docs([_doc_text(c) for c in chunks]), emb,
                {"backend": "tfidf", "model": "tfidf", "built_at": "now", "n_chunks": 1, "n_local": 1, "n_code": 0})
    return Engine(cfg, idx, providers)


def test_nothing_is_copied_or_sent_before_consent_and_cancel_sends_nothing(tmp_path, providers):
    eng = build(tmp_path, providers)
    seen = []

    def deny(req):
        seen.append(req)
        assert all(not p.calls for p in providers.values()) and not (explore.STAGE_DIR / "current").exists()
        return None
    r = eng.ask("how does it work?", groups={"proj"}, consent=deny, code_mode="explore")
    assert seen[0].scope == "explore" and "may open any of" in seen[0].detail and "proj/src/model.py" in seen[0].files
    assert not any("patient" in f for f in seen[0].files)
    assert r.cancelled and all(not p.calls for p in providers.values()) and not (explore.STAGE_DIR / "current").exists()


def test_only_claude_is_offered_for_explore(tmp_path, providers):
    got = {}

    def look(req):
        got.update({o.key: o.available for o in req.options})
        return None
    build(tmp_path, providers).ask("q", groups={"proj"}, consent=look, code_mode="explore")
    assert got == {"claude": True, "gemini": False, "ollama": False}


def test_approved_explore_reads_only_the_clean_copy_then_deletes_it(tmp_path, providers):
    eng = build(tmp_path, providers)
    r = eng.ask("how does it work?", groups={"proj"}, consent=lambda req: ["claude"], code_mode="explore")
    call = providers["claude"].calls[0]
    assert call["explore"] and not providers["gemini"].calls and not providers["ollama"].calls
    assert "proj/src/model.py" in call["files"] and SENTINEL not in call["text"] and "hunter2hunter2" not in call["text"]
    assert not (explore.STAGE_DIR / "current").exists(), "the copy is deleted after the answer"
    kinds = [s.kind for s in r.answers[0].segments if s.kind != "text"]
    assert kinds == ["code", "model", "unverified"]                    # the page-9999 citation is flagged, not shown as real
    code = next(s for s in r.answers[0].segments if s.kind == "code")
    assert code.detail and r.used_code and r.route.providers == ["claude"]


def test_a_follow_up_after_explore_asks_again(tmp_path, providers):
    eng, sess, asked = build(tmp_path, providers), Session(id="explore-test"), []

    def approve(req):
        asked.append(req.carried)
        return ["claude"]
    eng.ask("how does it work?", groups={"proj"}, session=sess, consent=approve, code_mode="explore")
    assert sess.turns[-1].used_code
    eng.ask("and the loss?", groups=set(), session=sess, consent=approve, memory=True)
    assert asked == [False, True]


@pytest.mark.parametrize("mode", ["gemini", "ollama", "compare"])
def test_explore_with_another_model_is_refused_without_sending(tmp_path, providers, mode):
    r = build(tmp_path, providers).ask("q", mode=mode, groups={"proj"}, consent=lambda req: ["claude"], code_mode="explore")
    assert any("only with Claude" in n for n in r.notes) and all(not p.calls for p in providers.values())


def test_explore_needs_the_claude_login(tmp_path, providers):
    from providers.claude import ClaudeProvider
    providers["claude"] = ClaudeProvider("claude-opus-5-5")             # API key only: no explore
    r = build(tmp_path, providers).ask("q", groups={"proj"}, consent=lambda req: ["claude"], code_mode="explore")
    assert any("Claude Code login" in n for n in r.notes) and not providers["gemini"].calls


def test_explore_failures_are_shown_and_the_copy_is_still_deleted(tmp_path, providers):
    def boom(*a, **k):
        raise ProviderError("explore broke")
    providers["claude"].explore = boom
    r = build(tmp_path, providers).ask("q", groups={"proj"}, consent=lambda req: ["claude"], code_mode="explore")
    assert r.answers[0].error == "explore broke" and not (explore.STAGE_DIR / "current").exists()


def test_no_project_or_image_request_falls_back(tmp_path, providers):
    eng = build(tmp_path, providers)
    r = eng.ask("q", mode="claude", groups=set(), consent=lambda req: ["claude"], code_mode="explore")
    assert any("needs a project ticked" in n for n in r.notes)
    r = eng.ask("make an infographic of the model", groups={"proj"}, consent=lambda req: None, code_mode="explore")
    assert any("explore was skipped" in n for n in r.notes)


def test_a_cleaned_environment_has_no_leftover_staging(tmp_path):
    assert not os.path.exists(explore.STAGE_DIR / "current")
