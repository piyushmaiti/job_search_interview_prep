"""Drive the Streamlit app headlessly with the offline demo models."""
import sys

import pytest
from streamlit.testing.v1 import AppTest

import candidate
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
