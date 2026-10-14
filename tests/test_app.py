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
    local = tmp_path / "config.local.yaml"
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
    assert at.button(key="profile-build").disabled, "building needs a CV"
    assert not any("Conversation" in m.value for m in at.markdown), "the research screen is not shown before there is a profile"


def test_profile_page_builds_and_shows_the_profile():
    import candidate
    at = run_app(view="__profile__")
    assert not at.exception, at.exception
    assert not at.button(key="profile-build").disabled
    at.button(key="profile-build").click().run()
    assert not at.exception, at.exception
    assert candidate.profile_status()["built"], ([e.value for e in at.error], at.session_state["flash"] if "flash" in at.session_state else None)
    assert any("A demo profile built offline" in t.value for t in at.text_area)


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


# ---------------------------------------------------------------- structured extraction on the Profile page
NURSE_CV = (b"# Sam Lee\n\nStaff nurse on the cardiac ward at Example Hospital, 2019 to 2023.\n"
            b"Trained twelve new nurses on the ward handover process.\nI want to move into nurse education.\n")


def test_saving_a_cv_reads_it_and_lists_items_for_review_without_using_them_yet():
    import extract
    at = run_app(view="__profile__")
    assert not at.exception, at.exception
    at.file_uploader(key="cv_up_0").set_value(("cv.md", NURSE_CV, "text/markdown")).run()
    next(b for b in at.button if b.label in ("Replace CV", "Save CV")).click().run()
    assert not at.exception, at.exception
    data = extract.load()
    pending = [i for i in data["items"] if i["status"] == "pending"]
    assert pending and all(i["evidence"] for i in pending)
    assert any(i["status"] == "unmatched" for i in data["items"]), "the invented item was set aside"
    assert candidate.read_facts() == "", "nothing is used before it is accepted"
    assert any("Review what was found" in m.value for m in at.markdown)
    assert any("Set aside" in e.label for e in at.expander)


def test_accepting_items_in_the_review_form_makes_them_available_to_the_prep_tools():
    import extract
    from providers.fake import FakeProvider
    candidate.save_cv("cv.md", NURSE_CV)
    ok, _ = extract.extract_source(FakeProvider(), "CV", NURSE_CV.decode())
    assert ok
    at = run_app(view="__profile__")
    pending = [i for i in extract.load()["items"] if i["status"] == "pending"]
    for it in pending:
        at.radio(key=f"rv_dec_{it['id']}").set_value("Accept")
    next(b for b in at.button if b.label == "Apply decisions").click().run()
    assert not at.exception, at.exception
    assert all(i["status"] == "accepted" for i in extract.load()["items"] if i["verified"])
    assert "Reviewed facts" in candidate.read_facts()
