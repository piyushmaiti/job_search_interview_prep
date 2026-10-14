"""Prep tools and the mock interview: what the model is given, what is kept, and the checks on its numbers."""
import pytest

import candidate
import mock
import prep
import settings
from providers.fake import FakeProvider


@pytest.fixture(autouse=True)
def workspace(monkeypatch):
    monkeypatch.setattr(settings, "WORKSPACE_DIR", settings.TOOL_DIR / ".test_scratch" / "prepws")
    import shutil
    shutil.rmtree(settings.WORKSPACE_DIR, ignore_errors=True)


@pytest.fixture
def role():
    candidate.save_cv("cv.md", b"# Alex Doe\nBuilt a PET pipeline in 2021. Cut scan time by 30%.")
    candidate.save_about("I want to work on imaging.")
    return candidate.create_role("Scientist", "Acme", "Industry", "Needs PyTorch and imaging.", "Show my pipeline work.", use_main_cv=True)


def test_context_has_tagged_documents_and_skips_missing_ones(role):
    c = prep.context(role)
    assert "<cv>" in c and "<about>" in c and "<job>" in c and "<intent>" in c
    assert "<profile>" not in c, "no profile has been built, so none is claimed"
    assert "Scientist at Acme (Industry position)" in c


def test_every_tool_prompt_carries_the_never_invent_rule(role):
    for t in prep.tools_for("Academic"):
        system, user = prep.prompt(t, role)
        assert "Never invent" in system and "GAP:" in system and "[model knowledge]" in system
        assert "Needs PyTorch" in user and t.task in user


def test_publication_tool_is_for_academic_roles_only():
    assert "publications" not in [t.key for t in prep.tools_for("Industry")]
    assert "publications" in [t.key for t in prep.tools_for("Academic")]


def test_run_saves_the_result(role):
    p = FakeProvider()
    ok, msg = prep.run(prep.TOOLS["gap"], role, p)
    assert ok and "Strong matches" in prep.load(role, "gap")
    assert p.calls[0]["system"].startswith("You are an interview coach")


def test_run_without_a_job_description_does_not_call_the_model(role):
    candidate._save_texts(role, "", "")
    p = FakeProvider()
    ok, msg = prep.run(prep.TOOLS["gap"], role, p)
    assert not ok and "job description" in msg and not p.calls and prep.load(role, "gap") == ""


def test_run_without_a_login_says_so(role):
    ok, msg = prep.run(prep.TOOLS["gap"], role, None)
    assert not ok and "Claude Code login" in msg


def test_a_failed_run_keeps_the_old_result(role):
    prep.save(role, "gap", "old")

    class Boom:
        def complete(self, *a, **k):
            raise RuntimeError("down")
    ok, msg = prep.run(prep.TOOLS["gap"], role, Boom())
    assert not ok and prep.load(role, "gap") == "old"


def test_invented_numbers_are_flagged_but_documented_ones_are_not(role):
    ctx = prep.context(role)
    odd = prep.unsupported_numbers("I cut scan time by 30% in 2021 and reached 99% accuracy with 250 patients. 1. First point", ctx)
    assert odd == ["250", "99%"]
    assert "99%" in prep.check("It hit 99% accuracy.", role)
    assert prep.check("It cut scan time by 30%.", role) == ""


def test_deleting_a_role_deletes_its_prep_files(role):
    prep.save(role, "gap", "x")
    assert prep.result_path(role, "gap").exists()
    candidate.delete_role(role.slug)
    assert not prep.prep_dir(role).exists()


def test_prep_results_are_not_part_of_the_searched_folders():
    cfg = settings.load_config()
    searched = [settings.resolve(d["path"]) for d in cfg["docs"]]
    assert all(settings.WORKSPACE_DIR / "prep" not in p.parents and p != settings.WORKSPACE_DIR / "prep" for p in searched)


# ---------------------------------------------------------------- mock interview
def test_question_prompt_avoids_repeats_and_fits_the_role_kind(role):
    _s, u = mock.question_prompt(role, "Technical", ["Tell me about PET."])
    assert "Tell me about PET." in u and "job description" in u.lower()
    acad = candidate.create_role("Postdoc", "Uni", "Academic", "Research on PET.", "", use_main_cv=True)
    _s, u = mock.question_prompt(acad, "Technical", [])
    assert "publications" in u


def test_feedback_has_no_scores_and_forbids_invented_detail(role):
    _s, u = mock.feedback_prompt(role, "Behavioral", "Q?", "My answer.")
    assert "[add:" in u and "My answer." in u
    assert "score" not in u.lower().replace("no scores", "")


def test_empty_answer_is_refused_before_the_model_is_called(role):
    p = FakeProvider()
    with pytest.raises(ValueError):
        mock.feedback(p, role, "Behavioral", "Q?", "   ")
    assert not p.calls


def test_interview_is_saved_for_reading_later(role):
    items = [{"question": "Q1?", "answer": "A1", "feedback": "F1"}, {"question": "Q2?"}]
    mock.record(role, "Behavioral", "2026-10-14 09:30", items)
    f = next(prep.prep_dir(role).glob("mock_*.md"))
    t = f.read_text()
    assert "Q1?" in t and "A1" in t and "F1" in t and "Q2?" in t
