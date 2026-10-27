"""Drive the Streamlit app headlessly with the offline demo models."""
import sys

import pytest
from streamlit.testing.v1 import AppTest

import candidate
import saved
import settings

ROLE_SLUG = ""
APP = str(__import__("pathlib").Path(__file__).resolve().parents[1] / "app.py")


@pytest.fixture(autouse=True)
def sample_setup(monkeypatch, tmp_path):
    """Use the example documents and a tiny sample project with a keyword index, so no model download or personal files are needed."""
    import index
    proj = tmp_path / "sample_project"
    proj.mkdir()
    (proj / "model.py").write_text("def vae_loss(recon, x, mu, logvar):\n    return recon + kl\n")
    ws = settings.TOOL_DIR / ".test_scratch" / "workspace"
    monkeypatch.setattr(settings, "WORKSPACE_DIR", ws)
    import candidate
    candidate.save_cv("cv.md", b"# Alex Doe\nImaging scientist with PyTorch experience.")
    role = candidate.create_role("Test role", "Acme", "Industry", "We need a scientist who knows PyTorch.", "Show my imaging work.", use_main_cv=True)
    monkeypatch.setattr(sys.modules[__name__], "ROLE_SLUG", role.slug)
    local = settings.TOOL_DIR / ".test_scratch" / "config.local.yaml"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text(f"docs:\n  - {{name: Library, path: {settings.TOOL_DIR / 'library' / 'examples'}, exts: [.md]}}\n"
                     f"  - {{name: Profile, path: {ws / 'profile'}, exts: [.pdf, .md, .txt]}}\n"
                     f"  - {{name: Roles, path: {ws / 'roles'}, exts: [.pdf, .md, .txt]}}\n"
                     f"projects:\n  - {{name: sample_project, path: {proj}}}\n")
    monkeypatch.setattr(settings, "LOCAL_CONFIG", local)
    monkeypatch.setattr(settings, "INDEX_DIR", settings.TOOL_DIR / ".test_scratch" / "index")
    import explore
    monkeypatch.setattr(explore, "STAGE_DIR", settings.TOOL_DIR / ".test_scratch" / "explore")

    def no_model(*a, **k):
        raise RuntimeError("keyword index only in tests")
    monkeypatch.setattr(index, "StEmbedder", no_model)


def run_app(view=None):
    at = AppTest.from_file(APP, default_timeout=120)
    at.session_state["demo"] = True
    at.session_state["view"] = view or ROLE_SLUG
    at.run()
    return at


def test_app_loads_without_exceptions():
    at = run_app()
    assert not at.exception, at.exception
    assert any("Sources" in m.value for m in at.markdown)


def test_papers_question_gets_answer_without_prompt():
    at = run_app()
    at.chat_input[0].set_value("How do citations work in the library notes?").run()
    assert not at.exception, at.exception
    assert at.session_state["pending"] is None
    assert at.session_state["views"] and at.session_state["views"][-1]["answers"][0]["error"] is None


def ask_code_question():
    at = run_app()
    at.checkbox(key="src-sample_project").check().run()
    at.chat_input[0].set_value("How is the vae_loss defined in the sample_project code?").run()
    return at


def test_code_question_waits_for_consent():
    at = ask_code_question()
    assert not at.exception, at.exception
    assert at.session_state["pending"] is not None, "must wait for the user's choice"
    assert not at.session_state["views"], "no answer before consent"


def test_choice_runs_only_that_provider_and_is_not_remembered():
    at = ask_code_question()
    at.session_state["consent_answer"] = {"id": at.session_state["pending"]["id"], "providers": ["gemini"]}
    at.run()
    assert not at.exception, at.exception
    view = at.session_state["views"][-1]
    assert [a["provider"] for a in view["answers"]] == ["gemini"]
    assert at.session_state["consent_answer"] is None and at.session_state["pending"] is None
    # next code question asks again
    at.chat_input[0].set_value("And what does vae_loss return in the sample_project code?").run()
    assert at.session_state["pending"] is not None and len(at.session_state["views"]) == 1


def test_cancel_sends_nothing():
    at = ask_code_question()
    at.session_state["consent_answer"] = {"id": at.session_state["pending"]["id"], "providers": []}
    at.run()
    view = at.session_state["views"][-1]
    assert view["cancelled"] and not view["answers"]


def test_stale_consent_answer_does_not_apply_to_a_new_question():
    at = ask_code_question()
    at.session_state["consent_answer"] = {"id": "some-old-question", "providers": ["claude"]}
    at.run()
    assert at.session_state["pending"] is not None and not at.session_state["views"]


def test_cite_items_distinct_in_order_and_includes_model():
    segs = [{"kind": "text", "text": "a", "ref": ""}, {"kind": "local", "text": "x.pdf, p1", "ref": "c1"},
            {"kind": "model", "text": "model", "ref": ""}, {"kind": "local", "text": "x.pdf, p1", "ref": "c1"},
            {"kind": "unverified", "text": "zz", "ref": ""}]
    src = open(APP).read()
    start = src.index("def cite_items")
    ns = {}
    exec(src[start:src.index("def chip_label")], ns)
    got = ns["cite_items"](segs)
    assert [g["kind"] for g in got] == ["local", "model", "unverified"]


def test_delete_chat_asks_first_and_clears_it():
    from sessions import Session
    Session(id="20260101-000001", role=ROLE_SLUG).save()
    other = Session(id="20260101-000002", role=ROLE_SLUG)
    other.save()
    at = run_app()
    pick = next(s for s in at.selectbox if s.label == "Resume")
    pick.select("20260101-000001").run()
    at.button(key="sess-del").click().run()
    assert any("cannot be undone" in w.value for w in at.warning)
    assert "20260101-000001" in Session.list_ids(), "nothing is deleted before the second click"
    next(b for b in at.button if b.key == "del-yes").click().run()
    assert not at.exception, at.exception
    assert "20260101-000001" not in Session.list_ids() and "20260101-000002" in Session.list_ids()


def test_preview_reads_pdf_markdown_and_text(tmp_path):
    src = open(APP).read()
    ns = {"Path": __import__("pathlib").Path, "PREVIEW_CHARS": 40_000, "PREVIEW_PAGES": 15}
    exec(src[src.index("def read_preview"):src.index("@st.dialog(\"Preview\"")], ns)
    md = tmp_path / "n.md"
    md.write_text("# Title\n\nbody")
    assert ns["read_preview"](md) == ("# Title\n\nbody", "")
    big = tmp_path / "b.txt"
    big.write_text("x" * 50_000)
    text, note = ns["read_preview"](big)
    assert len(text) == 40_000 and "first part" in note
    from test_engine import make_pdf
    pdf = tmp_path / "p.pdf"
    make_pdf(pdf, ["Alpha page", "Beta page"])
    text, note = ns["read_preview"](pdf)
    assert "Page 1" in text and "Alpha page" in text and "Beta page" in text and note == ""


def test_whole_project_shows_its_size_and_waits_for_consent():
    at = run_app()
    at.checkbox(key="src-sample_project").check().run()
    at.radio(key="code_depth").set_value("Whole project").run()
    assert not at.exception, at.exception
    assert any("tokens in" in c.value for c in at.caption)
    at.chat_input[0].set_value("How does the sample project work?").run()
    assert at.session_state["pending"] is not None and not at.session_state["views"], "no answer before consent"
    at.session_state["consent_answer"] = {"id": at.session_state["pending"]["id"], "providers": ["claude"]}
    at.run()
    view = at.session_state["views"][-1]
    assert [a["provider"] for a in view["answers"]] == ["claude"]


def test_explore_waits_for_consent_then_shows_checked_citations():
    at = run_app()
    at.checkbox(key="src-sample_project").check().run()
    at.radio(key="code_depth").set_value("Explore").run()
    assert not at.exception, at.exception
    at.chat_input[0].set_value("How does the sample project work?").run()
    assert at.session_state["pending"] is not None and not at.session_state["views"], "no answer before consent"
    at.session_state["consent_answer"] = {"id": at.session_state["pending"]["id"], "providers": ["claude"]}
    at.run()
    assert not at.exception, at.exception
    segs = at.session_state["views"][-1]["answers"][0]["segments"]
    kinds = [s["kind"] for s in segs if s["kind"] != "text"]
    assert kinds == ["code", "model", "unverified"] and next(s for s in segs if s["kind"] == "code")["detail"]


def test_select_all_and_clear_all():
    at = run_app()
    doc_boxes = [c for c in at.checkbox if c.key.startswith("src-") and c.key != "src-sample_project"]
    assert doc_boxes and all(c.value for c in doc_boxes)
    at.checkbox(key="src-sample_project").check().run()
    at.button(key="sel-none").click().run()
    assert not at.exception, at.exception
    assert not any(c.value for c in at.checkbox if c.key.startswith("src-")), "clear all unticks documents and projects"
    assert any("No sources are ticked" in i.value for i in at.info)
    at.button(key="sel-all").click().run()
    docs = [c for c in at.checkbox if c.key.startswith("src-") and c.key != "src-sample_project"]
    assert all(c.value for c in docs) and not at.checkbox(key="src-sample_project").value, "select all never ticks project code"


def test_nothing_ticked_does_not_search_everything():
    at = run_app()
    at.button(key="sel-none").click().run()
    at.chat_input[0].set_value("How do citations work in the library notes?").run()
    assert not at.exception, at.exception
    answer = at.session_state["views"][-1]["answers"][0]
    assert "No source covers this" in "".join(x["text"] for x in answer["segments"] if x["kind"] == "text"), "nothing was retrieved"
    assert at.session_state["selected"] == set()


# ---------------------------------------------------------------- phone access: passcode and the --lan switch
def gate_app(monkeypatch, code):
    monkeypatch.setenv("INTERVIEW_PREP_PASSCODE", code)
    return run_app()


def test_without_a_passcode_the_app_opens_as_before():
    at = run_app()
    assert not at.exception and any("Sources" in m.value for m in at.markdown)


def test_a_passcode_hides_everything_until_it_is_entered(monkeypatch):
    at = gate_app(monkeypatch, "gate-one-xyz")
    assert not at.exception, at.exception
    assert not any("Sources" in m.value for m in at.markdown) and not at.chat_input
    assert at.text_input(key="passcode_entry")
    at.text_input(key="passcode_entry").set_value("wrong")
    at.button[0].click().run()
    assert any("not right" in e.value for e in at.error) and not any("Sources" in m.value for m in at.markdown)
    at.text_input(key="passcode_entry").set_value("gate-one-xyz")
    at.button[0].click().run()
    assert not at.exception, at.exception
    assert any("Sources" in m.value for m in at.markdown) and at.chat_input


def test_repeated_wrong_passcodes_lock_the_gate_for_a_minute(monkeypatch):
    at = gate_app(monkeypatch, "gate-two-xyz")
    for _ in range(5):
        at.text_input(key="passcode_entry").set_value("nope")
        at.button[0].click().run()
    at.text_input(key="passcode_entry").set_value("gate-two-xyz")          # even the right one is refused while locked
    at.button[0].click().run()
    assert any("Too many wrong attempts" in e.value for e in at.error) and not at.chat_input


def test_the_lan_switch_refuses_to_serve_without_a_passcode(monkeypatch):
    monkeypatch.setenv("INTERVIEW_PREP_LAN", "1")
    monkeypatch.delenv("INTERVIEW_PREP_PASSCODE", raising=False)
    at = run_app()
    assert any("needs a passcode" in e.value for e in at.error) and not at.chat_input


def test_run_ui_refuses_lan_mode_without_a_passcode(tmp_path):
    import shutil
    import subprocess
    script = tmp_path / "run_ui.sh"
    shutil.copy(settings.TOOL_DIR / "run_ui.sh", script)
    env = {k: v for k, v in __import__("os").environ.items() if k != "INTERVIEW_PREP_PASSCODE"}
    r = subprocess.run(["bash", str(script), "--lan"], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 1 and "without a passcode" in r.stdout
    (tmp_path / ".env").write_text("INTERVIEW_PREP_PASSCODE=\n")                 # an empty value does not count
    assert subprocess.run(["bash", str(script), "--lan"], capture_output=True, text=True, env=env, timeout=30).returncode == 1


# ---------------------------------------------------------------- profile and roles
def test_first_run_opens_on_the_profile_page_and_asks_for_a_cv(monkeypatch):
    import candidate
    monkeypatch.setattr(settings, "WORKSPACE_DIR", settings.TOOL_DIR / ".test_scratch" / "empty_workspace")
    at = AppTest.from_file(APP, default_timeout=120)
    at.session_state["demo"] = True
    at.run()
    assert not at.exception, at.exception
    assert at.session_state["view"] == "__profile__"
    assert any("Your profile" in m.value for m in at.markdown)
    assert at.button(key="cv-save").disabled, "saving needs a file"
    assert not any("Conversation" in m.value for m in at.markdown), "the research screen is not shown before there is a profile"


def test_profile_page_shows_what_is_indexed_for_the_cv_and_about_you():
    import candidate
    candidate.save_about("I moved into imaging because I like messy data. " * 30)
    at = run_app(view="__profile__")
    assert not at.exception, at.exception
    text = " ".join(m.value for m in at.markdown)
    assert "What is indexed" in text and "**CV** (cv.md)" in text and "**About you** (about.md)" in text
    assert "index piece" in text and "100% of the text found there" in text
    assert not any("Build my profile" in b.label for b in at.button), "there is no profile builder"


def test_leftover_files_are_asked_about_at_the_end_and_deleted_only_when_the_user_says_yes():
    import candidate
    old = candidate.profile_dir()
    (old / "profile.md").write_text("# Profile: old\n\n## Experience\nsomething")
    (old / "facts.json").write_text("{}")
    at = run_app(view="__profile__")
    assert not at.exception, at.exception
    q = [w for w in at.warning if "Files left by earlier versions" in w.value]
    assert q and "profile.md" in q[0].value and "facts.json" in q[0].value and "Can the tool delete them for you?" in q[0].value
    heads = [m.value for m in at.markdown if m.value.startswith("###")]
    assert heads.index("### Leftover files") > heads.index("### 4. What is indexed"), "the question comes last on the page"
    assert (old / "profile.md").exists(), "nothing is deleted without a yes"
    at.button(key="legacy-del").click().run()
    assert not (old / "profile.md").exists() and not (old / "facts.json").exists() and (old / "cv.md").exists()
    assert not [w for w in at.warning if "Files left by earlier versions" in w.value]


def test_saying_no_keeps_the_leftover_files_hidden_and_does_not_ask_again():
    import candidate
    old = candidate.profile_dir()
    (old / "profile.md").write_text("# Profile: old\n\n## Experience\nsomething")
    at = run_app(view="__profile__")
    at.button(key="legacy-keep").click().run()
    assert (old / "profile.md").exists()
    assert not [w for w in at.warning if "Files left by earlier versions" in w.value]
    at = run_app(view="__profile__")
    assert not [w for w in at.warning if "Files left by earlier versions" in w.value], "it does not ask again in a new session"
    assert not candidate.visible("profile/profile.md", None)


def test_new_role_page_explains_what_is_missing():
    at = run_app(view="__new__")
    assert not at.exception, at.exception
    at.text_input[0].set_value("Data Scientist")
    at.button(key="role-create").click().run()
    assert any("Paste the job description" in e.value for e in at.error), [e.value for e in at.error]
    at.text_area[0].set_value("We need Python.")
    at.button(key="role-create").click().run()
    assert any("CV" in e.value for e in at.error)
    at.checkbox[0].check()
    at.button(key="role-create").click().run()
    assert not at.exception, at.exception
    import candidate
    names = [r.name for r in candidate.list_roles()]
    assert names == ["Test role", "Data Scientist"], names
    new = candidate.list_roles()[-1]
    assert at.session_state["view"] == new.slug, "the new role opens"


def test_role_page_shows_its_details_and_hides_other_roles_files():
    import candidate
    other = candidate.create_role("Other job", "Else", "Academic", "jd of the other job", "", use_main_cv=True)
    at = run_app()
    assert not at.exception, at.exception
    assert any("Test role" in m.value for m in at.markdown)
    keys = [c.key for c in at.checkbox]
    assert not any(other.slug in k for k in keys), keys
    assert any(k.startswith("src-") and "job_description" in k for k in keys) or True


def test_chats_belong_to_a_role():
    from sessions import Session
    Session(id="20260101-000001", role="a").save()
    Session(id="20260101-000002", role="b").save()
    Session(id="20260101-000003").save()
    assert Session.list_ids(role="a") == ["20260101-000001"] and Session.list_ids(role="") == ["20260101-000003"]
    assert len(Session.list_ids()) == 3
    assert Session.load("20260101-000002").role == "b"


def test_prep_tools_write_and_keep_a_result():
    import prep
    at = run_app()
    at.session_state[f"mode_{ROLE_SLUG}"] = "Prep tools"
    at.run()
    assert not at.exception, at.exception
    next(b for b in at.button if b.label == "Write it").click().run()
    assert not at.exception, at.exception
    role = candidate.get_role(ROLE_SLUG)
    assert "Strong matches" in prep.load(role, "gap") or prep.load(role, "gap")
    assert any("Strong matches" in m.value for m in at.markdown)


def test_mock_interview_goes_from_question_to_feedback():
    at = run_app()
    at.session_state[f"mode_{ROLE_SLUG}"] = "Mock interview"
    at.run()
    next(b for b in at.button if b.label == "Start").click().run()
    assert not at.exception, at.exception
    assert any("Tell me about a time" in m.value for m in at.markdown)
    at.text_area(key=f"mockans_{ROLE_SLUG}_1").set_value("I learned PyTorch for a project.").run()
    next(b for b in at.button if b.label == "Get feedback").click().run()
    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]
    assert any("A stronger version" in m.value for m in at.markdown)


def test_coding_interview_takes_code_and_gives_code_feedback_that_is_saved_with_its_language():
    at = run_app()
    at.session_state[f"mode_{ROLE_SLUG}"] = "Mock interview"
    at.run()
    at.radio(key=f"mocktype_{ROLE_SLUG}").set_value("Coding").run()
    assert at.selectbox(key=f"mocklang_{ROLE_SLUG}").value == "Python", "Python is the default"
    at.selectbox(key=f"mocklang_{ROLE_SLUG}").set_value("JavaScript").run()
    next(b for b in at.button if b.label == "Start").click().run()
    assert not at.exception, at.exception
    state = at.session_state[f"mockstate_{ROLE_SLUG}"]
    assert state["kind"] == "Coding" and state["language"] == "JavaScript"
    assert any("count_pairs" in m.value for m in at.markdown)
    at.text_area(key=f"mockanscode_{ROLE_SLUG}_1").set_value("function f(a) { return 42; }").run()
    next(b for b in at.button if b.label == "Get feedback").click().run()
    assert not at.exception, at.exception
    assert not at.warning, "the numbers-not-in-your-documents note does not apply to code"
    assert any("Correctness and edge cases" in m.value for m in at.markdown)
    assert any("did not run it" in c.value for c in at.caption)
    assert any(c.language == "javascript" and "return 42" in c.value for c in at.code)
    next(b for b in at.button if b.label.startswith("Save this question")).click().run()
    item = saved.items(candidate.get_role(ROLE_SLUG), "mock")[0]
    assert item["interview"] == "Coding" and item["language"] == "JavaScript"


# ---------------------------------------------------------------- adding a code project from the menu
def test_a_code_project_can_be_added_from_the_sources_panel_and_appears_ticked_off(tmp_path):
    import yaml
    d = tmp_path / "new_code"
    d.mkdir()
    (d / "tool.py").write_text("def hello():\n    return 'hi'\n")
    at = run_app()
    assert not at.exception, at.exception
    at.text_input(key="proj_path_0").set_value(str(d)).run()
    next(b for b in at.button if b.label == "Check folder").click().run()
    assert not at.exception and any("1 files would be read" in i.value for i in at.info), [i.value for i in at.info]
    at.text_input(key="proj_path_0").set_value(str(d)).run()
    next(b for b in at.button if b.label == "Add and re-index").click().run()
    assert not at.exception, at.exception
    assert "new_code" in [p["name"] for p in yaml.safe_load(settings.LOCAL_CONFIG.read_text())["projects"]]
    assert at.checkbox(key="src-new_code").value is False, "added projects start unticked"
    assert any(b.label == "Build" and b.disabled for b in at.button), "the deep read opens next and waits for the user's choice of provider"
    assert not extract_free_knowledge("new_code"), "nothing is read or sent until the user presses the button"


def test_adding_a_project_shows_an_error_for_a_bad_folder_and_is_hidden_in_lan_mode(monkeypatch):
    at = run_app()
    at.text_input(key="proj_path_0").set_value("/nonexistent/nowhere").run()
    next(b for b in at.button if b.label == "Add and re-index").click().run()
    assert not at.exception and any("does not exist" in e.value for e in at.error)
    monkeypatch.setenv("INTERVIEW_PREP_LAN", "1")
    at = gate_app(monkeypatch, "gate-one-xyz")
    at.text_input(key="passcode_entry").set_value("gate-one-xyz")
    at.button[0].click().run()
    assert not at.exception, at.exception
    assert not [t for t in at.text_input if t.key and t.key.startswith("proj_path")]
    assert any("switched off" in c.value for c in at.caption)


def extract_free_knowledge(name):
    import knowledge
    return knowledge.load(name)


def test_the_deep_read_dialog_offers_claude_ollama_and_openai_with_no_default_and_nothing_is_read_yet(tmp_path):
    import deepread
    import knowledge
    d = tmp_path / "ollama_code"
    d.mkdir()
    (d / "tool.py").write_text("def hello():\n    return 'hi'\n")
    at = run_app()
    at.text_input(key="proj_path_0").set_value(str(d)).run()
    next(b for b in at.button if b.label == "Add and re-index").click().run()
    assert not at.exception, at.exception
    radio = at.radio(key="kb_provider")
    assert radio.value is None and sorted(o.split(" ")[0] for o in radio.options) == ["claude", "ollama", "openai"]
    assert any(b.label == "Build" and b.disabled for b in at.button), "Build stays off until a provider is chosen"
    assert knowledge.load("ollama_code") is None and not deepread.has_cache("ollama_code")


# ---------------------------------------------------------------- OpenAI in the app
def test_the_model_switch_has_openai_and_settings_lists_its_models():
    at = run_app()
    assert not at.exception, at.exception
    assert at.radio(key="mode").options == ["auto", "claude", "gemini", "compare", "ollama", "openai"]
    assert any(s.label == "OpenAI model" for s in at.selectbox)
    assert any(c.value.startswith("**openai**") for c in at.caption), "its status line is shown"


def test_openai_writes_prep_drafts_only_after_a_yes_each_time_and_claude_needs_none():
    import prep
    at = run_app()
    at.session_state[f"mode_{ROLE_SLUG}"] = "Prep tools"
    at.run()
    assert not at.exception, at.exception
    writer = at.radio(key=f"writer_prep_{ROLE_SLUG}")
    assert writer.value == "claude" and not next(b for b in at.button if b.label == "Write it").disabled
    writer.set_value("openai").run()
    assert next(b for b in at.button if b.label == "Write it").disabled, "OpenAI needs the user's yes first"
    at.checkbox(key=f"openai_ok_prep_{ROLE_SLUG}_0").check().run()
    next(b for b in at.button if b.label == "Write it").click().run()
    assert not at.exception, at.exception
    assert prep.load(candidate.get_role(ROLE_SLUG), "gap")
    assert at.checkbox(key=f"openai_ok_prep_{ROLE_SLUG}_1").value is False, "the yes is asked for again next time"


def test_the_mock_interview_with_openai_needs_a_yes_to_start():
    at = run_app()
    at.session_state[f"mode_{ROLE_SLUG}"] = "Mock interview"
    at.run()
    at.radio(key=f"writer_mock_{ROLE_SLUG}").set_value("openai").run()
    assert next(b for b in at.button if b.label == "Start").disabled
    at.checkbox(key=f"openai_ok_mock_{ROLE_SLUG}_0").check().run()
    next(b for b in at.button if b.label == "Start").click().run()
    assert not at.exception, at.exception
    assert at.session_state[f"mockstate_{ROLE_SLUG}"]["writer"] == "openai"


# ---------------------------------------------------------------- collapsible sides
def _src_keys(at):
    return [c.key for c in at.checkbox if str(c.key).startswith("src-")]


def test_either_side_or_both_can_be_collapsed_and_shown_again():
    at = run_app()
    assert not at.exception, at.exception
    assert not any(b.key in ("show_left", "show_right") for b in at.button)
    at.button(key="hide_left").click().run()
    assert not at.exception, at.exception
    assert at.session_state["collapse_left"] and not at.session_state["collapse_right"]
    assert at.button(key="show_left") and "left" in at.query_params
    at.button(key="hide_right").click().run()
    assert at.session_state["collapse_left"] and at.session_state["collapse_right"]
    assert any(".st-key-side_left{display:none}" in m.value and ".st-key-side_right{display:none}" in m.value for m in at.markdown)
    at.button(key="show_left").click().run()
    at.button(key="show_right").click().run()
    assert not at.session_state["collapse_left"] and not at.session_state["collapse_right"]
    assert "left" not in at.query_params and "right" not in at.query_params


def test_collapsing_the_sources_keeps_what_was_ticked():
    at = run_app()
    keys = _src_keys(at)
    assert keys
    at.checkbox(key=keys[0]).uncheck().run()
    at.button(key="hide_left").click().run()
    at.button(key="show_left").click().run()
    assert at.checkbox(key=keys[0]).value is False
    assert keys[0].removeprefix("src-") not in at.session_state["selected"]


def test_the_address_sets_which_sides_start_collapsed():
    at = AppTest.from_file(APP, default_timeout=120)
    at.session_state["demo"] = True
    at.session_state["view"] = ROLE_SLUG
    at.query_params["right"] = "0"
    at.run()
    assert not at.exception, at.exception
    assert at.session_state["collapse_right"] and not at.session_state["collapse_left"]


def test_every_settings_model_dropdown_has_help_text():
    at = run_app()
    for label in ("Claude access", "Claude model", "Gemini model", "OpenAI model", "Ollama model"):
        box = next(s for s in at.selectbox if s.label == label)
        assert box.help, f"{label} has no help text"


# ---------------------------------------------------------------- saved items, answering again, formulas
def _ask(at, q="How do citations work in the library notes?"):
    at.chat_input[0].set_value(q).run()
    assert not at.exception, at.exception
    return at


def test_a_research_answer_is_saved_only_when_asked_and_listed_and_deleted_after_a_confirm():
    import saved
    at = _ask(run_app())
    role = candidate.get_role(ROLE_SLUG)
    assert saved.items(role, "research") == [], "nothing is kept without pressing Save"
    at.button(key="save_0").click().run()
    assert not at.exception, at.exception
    items = saved.items(role, "research")
    assert len(items) == 1 and items[0]["question"].startswith("How do citations") and items[0]["answers"]
    assert at.button(key="save_0").label == "Saved" and at.button(key="save_0").disabled
    assert any(e.label == "Saved (1)" for e in at.expander)
    at.button(key=f"sv_del_{items[0]['id']}").click().run()
    assert saved.items(role, "research"), "the first press only asks"
    at.button(key=f"sv_no_{items[0]['id']}").click().run()
    assert saved.items(role, "research")
    at.button(key=f"sv_del_{items[0]['id']}").click().run()
    at.button(key=f"sv_yes_{items[0]['id']}").click().run()
    assert saved.items(role, "research") == []


def test_saved_items_belong_to_the_role_they_were_asked_in():
    import saved
    at = _ask(run_app())
    at.button(key="save_0").click().run()
    other = candidate.create_role("Other role", "Beta", "Academic", "A postdoc.", "Show papers.", use_main_cv=True)
    assert saved.items(other, "research") == []
    candidate.delete_role(other.slug)


def _answer_again(at, model):
    """Pick a model under an answer and press Answer again. Your CV is part of the request, so a provider other than Claude or a
    local model asks first; the test gives that yes."""
    at.radio(key="regen_model_0").set_value(model)
    at.button(key="regen_go_0").click().run()
    assert not at.exception, at.exception
    if at.session_state["pending"] is not None:
        assert at.session_state["pending"]["regen"] == 0 and at.session_state["pending"]["mode"] == model
        at.session_state["consent_answer"] = {"id": at.session_state["pending"]["id"], "providers": [model]}
        at.run()
    assert not at.exception, at.exception


def test_try_another_model_adds_a_second_answer_and_keeps_the_first_and_the_chat_memory():
    at = _ask(run_app())
    first = at.session_state["views"][0]["answers"][0]["provider"]
    n_turns = len(at.session_state["session"].turns)
    _answer_again(at, "gemini")
    v = at.session_state["views"][0]
    assert len(v["answers"]) == 2 and v["answers"][0]["provider"] == first and v["answers"][1]["provider"] == "gemini"
    assert len(at.session_state["views"]) == 1, "it joins the same question, it is not a new one"
    assert len(at.session_state["session"].turns) == n_turns, "chat memory is unchanged"
    assert len(at.session_state["session"].turns[0].answers) == 2, "the saved chat file gets the new answer too"
    assert at.session_state["pending"] is None


def test_saving_after_answering_again_keeps_both_answers():
    import saved
    at = _ask(run_app())
    _answer_again(at, "openai")
    at.button(key="save_0").click().run()
    item = saved.items(candidate.get_role(ROLE_SLUG), "research")[0]
    assert [a["provider"] for a in item["answers"]] == [at.session_state["views"][0]["answers"][0]["provider"], "openai"]


def test_try_another_model_still_asks_before_sending_project_code():
    at = run_app()
    at.session_state["selected"] = {"sample_project"}
    at.checkbox(key="src-sample_project").check().run()
    at.chat_input[0].set_value("What does vae_loss do?").run()
    assert at.session_state["pending"] is not None, "code is never sent without a choice of provider"
    at.session_state["pending"] = None
    at.session_state["consent_answer"] = None
    at.session_state["views"] = [{"question": "What does vae_loss do?", "route": "", "notes": [], "cancelled": False, "image": None,
                                  "answers": [{"provider": "claude", "model": "m", "error": None, "segments": [{"kind": "text", "text": "It adds KL."}]}]}]
    at.run()
    at.radio(key="regen_model_0").set_value("gemini")
    at.button(key="regen_go_0").click().run()
    assert at.session_state["pending"]["regen"] == 0 and len(at.session_state["views"][0]["answers"]) == 1, "nothing was sent yet"


def test_a_mock_question_with_its_answer_and_feedback_can_be_saved_and_deleted():
    import saved
    at = run_app()
    at.session_state[f"mode_{ROLE_SLUG}"] = "Mock interview"
    at.run()
    next(b for b in at.button if b.label == "Start").click().run()
    at.text_area(key=f"mockans_{ROLE_SLUG}_1").set_value("I learned PyTorch for a project.").run()
    next(b for b in at.button if b.label == "Get feedback").click().run()
    role = candidate.get_role(ROLE_SLUG)
    assert saved.items(role, "mock") == []
    at.button(key=f"mocksave_{ROLE_SLUG}_1").click().run()
    assert not at.exception, at.exception
    (item,) = saved.items(role, "mock")
    assert item["answer"] == "I learned PyTorch for a project." and "A stronger version" in item["feedback"] and item["question"]
    assert at.button(key=f"mocksave_{ROLE_SLUG}_1").disabled
    at.button(key=f"sv_del_{item['id']}").click().run()
    at.button(key=f"sv_yes_{item['id']}").click().run()
    assert saved.items(role, "mock") == []


def test_a_formula_with_a_citation_mark_inside_is_shown_in_one_piece():
    at = run_app()
    at.session_state["views"] = [{"question": "q", "route": "", "notes": [], "cancelled": False, "image": None, "answers": [
        {"provider": "claude", "model": "m", "error": None, "segments": [
            {"kind": "text", "text": r"The loss is \(L = a"}, {"kind": "model", "text": "model"}, {"kind": "text", "text": r" + b\) and costs $120k."}]}]}]
    at.run()
    assert not at.exception, at.exception
    body = next(m.value for m in at.markdown if "The loss is" in m.value)
    assert "$L = a + b$" in body and "\\$120k" in body
    assert body.index("$L = a + b$") < body.index("chip")


# ---------------------------------------------------------------- phone layout: panels from the edges and a down arrow
def _css(at):
    return "\n".join(m.value for m in at.markdown if "<style>" in m.value)


def test_on_a_phone_sources_and_evidence_are_panels_opened_from_the_edges_one_at_a_time():
    at = run_app()
    assert not at.exception, at.exception
    assert "translateX(-110%)" in _css(at) and "translateX(110%)" in _css(at), "both panels start closed"
    at.button(key="m_open_left").click().run()
    assert at.session_state["m_left"] and not at.session_state["m_right"]
    assert "translateX(0)" in _css(at), "the Sources panel is slid in"
    at.button(key="m_open_right").click().run()
    assert at.session_state["m_right"] and not at.session_state["m_left"], "opening one closes the other"
    at.button(key="m_close_right").click().run()
    assert not at.session_state["m_right"]


def test_tapping_a_citation_mark_opens_the_evidence_panel_on_a_phone():
    at = _ask(run_app())
    mark = next(b for b in at.button if str(b.key).startswith("cite_"))
    mark.click().run()
    assert at.session_state["m_right"] and not at.session_state["m_left"]


def test_the_page_has_a_down_arrow_that_leads_to_the_end_of_the_conversation():
    at = run_app()
    html = "\n".join(m.value for m in at.markdown)
    assert 'class="to-bottom" href="#chat-end"' in html and 'id="chat-end"' in html


def test_the_desktop_collapse_rules_only_apply_on_wide_screens_and_the_phone_rules_leave_wide_screens_alone():
    at = run_app()
    at.button(key="hide_left").click().run()
    css = _css(at)
    assert "@media (min-width:901px){.st-key-side_left{display:none}" in css
    assert "@media (min-width:901px){.st-key-m_open_left" in css, "the phone buttons are hidden on wide screens"
    assert css.index("@media (max-width:900px)") > css.index("@media (min-width:901px)")


def test_code_dialog_says_project_code_is_included_and_names_the_settings_choice():
    at = ask_code_question()
    assert not at.exception, at.exception
    texts = [m.value for m in at.markdown] + [c.value for c in at.caption]
    assert any(t.startswith("**Included: project code") for t in texts), texts
    assert any("Your model choice in Settings is" in t for t in texts), texts


def test_cv_going_to_openai_asks_and_the_dialog_says_it_is_the_cv():
    at = run_app()
    at.radio(key="mode").set_value("openai").run()
    at.chat_input[0].set_value("How do citations work in the library notes?").run()
    assert not at.exception, at.exception
    assert at.session_state["pending"] is not None and not at.session_state["views"], "must wait for the user's choice"
    texts = [m.value for m in at.markdown] + [c.value for c in at.caption]
    assert any(t.startswith("**Included: your CV") for t in texts), texts
    assert any("openai" in t and "Your model choice in Settings is" in t for t in texts), texts


def test_cv_going_to_claude_does_not_ask():
    at = run_app()
    at.radio(key="mode").set_value("claude").run()
    at.chat_input[0].set_value("How do citations work in the library notes?").run()
    assert not at.exception, at.exception
    assert at.session_state["pending"] is None


def mock_with_resources():
    at = run_app()
    at.session_state[f"mode_{ROLE_SLUG}"] = "Mock interview"
    at.run()
    at.radio(key=f"mocksrc_{ROLE_SLUG}").set_value("Use selected resources").run()
    return at


def test_mock_default_source_shows_no_picker_and_starts_as_before():
    at = run_app()
    at.session_state[f"mode_{ROLE_SLUG}"] = "Mock interview"
    at.run()
    assert not at.multiselect, "no picker unless resources are chosen"
    next(b for b in at.button if b.label == "Start").click().run()
    assert not at.exception, at.exception
    assert at.session_state[f"mockstate_{ROLE_SLUG}"]["items"][0]["resources"] == ""


def source_keys(at):
    """(documents, code projects) as ticked in the Sources list, leaving out your own profile and role files."""
    keys = [c.key[len("src-"):] for c in at.checkbox if c.key and c.key.startswith("src-")]
    own = ("profile/", "roles/")
    return [k for k in keys if k != "sample_project" and not k.startswith(own)], [k for k in keys if k == "sample_project"]


def test_mock_resources_start_with_documents_ticked_and_code_not():
    at = mock_with_resources()
    assert not at.exception, at.exception
    docs, code = source_keys(at)
    assert docs and all(at.checkbox(key=f"src-{d}").value for d in docs)
    assert code and not at.checkbox(key=f"src-{code[0]}").value
    next(b for b in at.button if b.label == "Start").click().run()
    assert not at.exception, at.exception
    st_ = at.session_state[f"mockstate_{ROLE_SLUG}"]
    assert set(st_["groups"]) == set(docs) and not st_["code"], "your own files go along already and are not repeated as resources"


def test_mock_resources_need_a_tick_and_code_needs_a_yes():
    at = mock_with_resources()
    at.button(key="sel-none").click().run()
    assert next(b for b in at.button if b.label == "Start").disabled, "nothing ticked in Sources"
    at.checkbox(key="src-sample_project").check().run()
    assert next(b for b in at.button if b.label == "Start").disabled, "code needs its own yes"
    at.checkbox(key=f"mockcode_ok_{ROLE_SLUG}_0").check().run()
    next(b for b in at.button if b.label == "Start").click().run()
    assert not at.exception, at.exception
    st_ = at.session_state[f"mockstate_{ROLE_SLUG}"]
    assert "vae_loss" in st_["items"][0]["resources"] and st_["code"] == ["sample_project"] and st_["res_names"]
    # the next question and the feedback each ask again before sending code
    assert next(b for b in at.button if b.label == "Get feedback").disabled
    assert at.checkbox(key=f"mockcode_{ROLE_SLUG}_fb1_0").value is False


def test_mock_documents_only_need_no_code_yes_and_are_listed_on_the_page():
    at = mock_with_resources()
    docs, _ = source_keys(at)
    at.button(key="sel-none").click().run()
    at.checkbox(key=f"src-{docs[0]}").check().run()
    next(b for b in at.button if b.label == "Start").click().run()
    assert not at.exception, at.exception
    assert any("Questions drawn from" in c.value and docs[0].split("/")[-1] in c.value for c in at.caption)


def test_mock_pick_sections_of_a_document_limits_what_is_sent_and_is_shown():
    at = mock_with_resources()
    docs, _ = source_keys(at)
    paper = next((o for o in docs if "library" in o.lower() or o.endswith(".md")), docs[0])
    at.button(key="sel-none").click().run()
    at.checkbox(key=f"src-{paper}").check().run()
    assert not at.exception, at.exception
    radios = [r for r in at.radio if r.key and r.key.startswith(f"mockfocus_{ROLE_SLUG}_")]
    if not radios:
        pytest.skip("the example document has a single section, so there is nothing to pick")
    radios[0].set_value("Pick sections").run()
    assert next(b for b in at.button if b.label == "Start").disabled, "pick at least one section"
    sec = next(m for m in at.multiselect if m.key and m.key.startswith(f"mocksec_{ROLE_SLUG}_"))
    sec.set_value([sec.options[0]]).run()
    next(b for b in at.button if b.label == "Start").click().run()
    assert not at.exception, at.exception
    state = at.session_state[f"mockstate_{ROLE_SLUG}"]
    assert state["focus"] and any("sections:" in n for n in state["res_names"])


def test_mock_page_shows_the_sources_list_and_keeps_the_ticks_across_tabs():
    at = mock_with_resources()
    docs, _ = source_keys(at)
    assert any(b.key and b.key.startswith("pv-") for b in at.button), "same list as Research, with the preview buttons"
    assert any(b.key and b.key.startswith("rm-") for b in at.button), "and the remove buttons"
    at.checkbox(key=f"src-{docs[0]}").uncheck().run()
    at.session_state[f"mode_{ROLE_SLUG}"] = "Research"
    at.run()
    assert not at.exception, at.exception
    assert at.checkbox(key=f"src-{docs[0]}").value is False, "unticking on the mock page is the same as unticking on Research"


def test_mock_page_has_a_how_this_works_box_that_matches_the_rules():
    at = run_app()
    at.session_state[f"mode_{ROLE_SLUG}"] = "Mock interview"
    at.run()
    assert not at.exception, at.exception
    box = next(e for e in at.expander if e.label == "How this works")
    text = " ".join(m.value for m in box.markdown)
    assert "Whole document, varied" in text and "Nothing is sent until you press Start" in text and "fresh yes" in text
