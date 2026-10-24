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


def test_a_long_cv_and_about_you_reach_the_tools_in_full(role):
    cv = "# Alex Doe\n" + "\n".join(f"Worked on project {i} and wrote it up carefully." for i in range(3000)) + "\nTHE VERY LAST LINE OF THE CV."
    assert len(cv) > 100_000
    candidate.save_cv("cv.md", cv.encode())
    candidate.save_about("I like messy data. " * 3000 + "THE VERY LAST LINE OF ABOUT YOU.")
    r = candidate.create_role("Long", "Co", "Industry", "Needs imaging.", "", use_main_cv=True)
    ctx = prep.context(r)
    assert "THE VERY LAST LINE OF THE CV." in ctx and "THE VERY LAST LINE OF ABOUT YOU." in ctx and "cut for length" not in ctx


# ---------------------------------------------------------------- mock interview: selected resources
def _index_with_profile(chunks):
    from conftest import make_index
    from ingest import Chunk
    mine = Chunk("p1", "local", "profile/cv.pdf", "profile/cv.pdf", "My own CV text.", page=1)
    return make_index(chunks + [mine])


def test_without_resources_the_prompts_are_exactly_what_they_were(role):
    assert mock.question_prompt(role, "Technical", ["Q?"]) == mock.question_prompt(role, "Technical", ["Q?"], "")
    system, user = mock.question_prompt(role, "Technical", [])
    assert "resource" not in system.lower() and "<resource" not in user
    system, user = mock.feedback_prompt(role, "Technical", "Q?", "A.")
    assert "resource" not in system.lower() and "<resource" not in user


def test_with_resources_both_prompts_carry_them_and_keep_the_cv_and_job(role):
    res = '<resource name="library/p.pdf">Scanner harmonization uses style transfer.</resource>'
    for system, user in (mock.question_prompt(role, "Technical", [], res), mock.feedback_prompt(role, "Technical", "Q?", "A.", res)):
        assert "Scanner harmonization uses style transfer." in user and "[Resource: name]" in system
        assert "never instructions" in system and "Never invent" in system
        assert "<cv>" in user and "<job>" in user and "Needs PyTorch" in user


def test_options_leave_out_your_own_documents_and_sort_papers_from_code(chunks):
    idx = _index_with_profile(chunks)
    local, code = mock.resource_options(idx, {})
    assert "profile/cv.pdf" not in local
    assert "library/p.pdf" in local and code == ["tau_proj"]


def test_resource_text_comes_only_from_the_picked_sources(role, chunks):
    idx = _index_with_profile(chunks)
    text, names, _ = mock.resource_context(idx, {"library/p.pdf"}, role, "Technical")
    assert "style transfer" in text and "vae_loss" not in text and "My own CV text" not in text
    assert names == ["library/p.pdf"]
    text, names, _ = mock.resource_context(idx, {"tau_proj"}, role, "Technical")
    assert "vae_loss" in text and "style transfer" not in text
    assert mock.resource_context(idx, set(), role, "Technical") == ("", [], set())


def test_a_paper_over_the_cap_is_cut_down_to_passages(role, monkeypatch):
    from conftest import make_index
    from ingest import Chunk
    big = [Chunk(f"b{i}", "local", "library/big.pdf", "library/big.pdf", ("PyTorch imaging " if i == 3 else "filler ") * 50, page=i) for i in range(40)]
    idx = make_index(big)
    monkeypatch.setattr(mock, "RESOURCE_CAP", 2000)
    text, names, _ = mock.resource_context(idx, {"library/big.pdf"}, role, "Technical")
    assert text and len(text) < 20000 and names == ["library/big.pdf"]


def test_the_numbers_check_counts_numbers_found_in_a_resource(role):
    fb = "You could mention the 94.7% accuracy reported in the paper [Resource: library/p.pdf]."
    assert "94.7" in prep.check(fb, role)
    assert prep.check(fb, role, "The paper reports 94.7% accuracy.") == ""


def test_the_record_lists_the_resources_used(role):
    mock.record(role, "Technical", "2026-10-22 09:30", [{"question": "Q1?", "answer": "A1", "feedback": "F1"}], ["library/p.pdf"])
    t = next(prep.prep_dir(role).glob("mock_*.md")).read_text()
    assert "Resources used: library/p.pdf" in t


def _long_paper(n=60):
    from conftest import make_index
    from ingest import Chunk
    return make_index([Chunk(f"c{i}", "local", "library/long.pdf", "library/long.pdf",
                             f"PyTorch imaging section {i} " + f"topic{i} " * 20, page=i) for i in range(n)])


def test_each_question_gets_fresh_passages_from_a_long_paper(role, monkeypatch):
    monkeypatch.setattr(mock, "RESOURCE_CAP", 2000)          # the paper does not fit, so passages are picked
    idx = _long_paper()
    t1, _n, seen1 = mock.resource_context(idx, {"library/long.pdf"}, role, "Technical")
    t2, _n, seen2 = mock.resource_context(idx, {"library/long.pdf"}, role, "Technical", seen1)
    assert seen1 and seen1 < seen2, "the second question adds new passages"
    first, second = set(t1.split("</resource>")), set(t2.split("</resource>"))
    assert first != second and len(first & second) < len(first), "mostly different passages"


def test_the_passages_start_over_once_the_paper_is_used_up(role, monkeypatch):
    monkeypatch.setattr(mock, "RESOURCE_CAP", 2000)
    idx = _long_paper(20)
    seen, rounds = set(), []
    for _ in range(4):
        _t, _n, seen = mock.resource_context(idx, {"library/long.pdf"}, role, "Technical", seen)
        rounds.append(len(seen))
    assert max(rounds) <= 20 and rounds[-1] > 0, "no crash and always something to ask about"


def test_a_paper_that_fits_is_the_same_for_every_question(role, chunks):
    idx = _index_with_profile(chunks)
    t1, _n, s1 = mock.resource_context(idx, {"library/p.pdf"}, role, "Technical")
    t2, _n, _s = mock.resource_context(idx, {"library/p.pdf"}, role, "Technical", s1)
    assert t1 == t2


# ---------------------------------------------------------------- mock interview: pick sections of a paper
def _paper_md():
    from conftest import make_index
    from ingest import Chunk
    secs = {"Methods": "We trained with a cycle loss.", "Loss functions": "The adversarial loss weights were tuned.",
            "Results": "Accuracy rose by a wide margin.", "Limitations": "Only three sites were tested."}
    return make_index([Chunk(f"m{i}", "local", "notes/paper.md", "notes/paper.md", t, section=s) for i, (s, t) in enumerate(secs.items())])


def test_a_markdown_paper_offers_its_headings_in_order():
    opts = mock.section_options(_paper_md(), "notes/paper.md")
    assert [label for label, _ in opts] == ["Methods", "Loss functions", "Results", "Limitations"]


def test_a_pdf_offers_blocks_of_pages():
    from conftest import make_index
    from ingest import Chunk
    idx = make_index([Chunk(f"p{n}", "local", "library/x.pdf", "library/x.pdf", f"page {n} text", page=n) for n in range(1, 13)])
    labels = [label for label, _ in mock.section_options(idx, "library/x.pdf")]
    assert labels == ["Pages 1–5", "Pages 6–10", "Pages 11–12"]


def test_a_short_note_with_one_part_offers_no_choice():
    from conftest import make_index
    from ingest import Chunk
    idx = make_index([Chunk("s1", "local", "notes/one.md", "notes/one.md", "just one part", section="one.md")])
    assert mock.section_options(idx, "notes/one.md") == []


def test_only_the_picked_sections_are_sent(role):
    idx = _paper_md()
    opts = dict(mock.section_options(idx, "notes/paper.md"))
    text, names, _ = mock.resource_context(idx, {"notes/paper.md"}, role, "Technical", focus={"notes/paper.md": opts["Loss functions"] | opts["Limitations"]})
    assert "adversarial loss" in text and "three sites" in text
    assert "cycle loss" not in text and "wide margin" not in text, "unpicked sections must not be sent"


def test_picked_sections_hold_even_when_the_paper_is_over_the_cap(role, monkeypatch):
    idx = _paper_md()
    monkeypatch.setattr(mock, "RESOURCE_CAP", 40)
    opts = dict(mock.section_options(idx, "notes/paper.md"))
    text, _n, _s = mock.resource_context(idx, {"notes/paper.md"}, role, "Technical", focus={"notes/paper.md": opts["Results"]})
    assert "wide margin" in text and "cycle loss" not in text and "three sites" not in text
