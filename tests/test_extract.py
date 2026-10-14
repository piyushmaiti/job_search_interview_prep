"""Structured extraction of an uploaded or pasted document: masking, checking quotes, review, and what reaches the prep tools."""
import json
import shutil
from types import SimpleNamespace

import pytest

import candidate
import extract
import prep
import settings
from providers.fake import FakeProvider


@pytest.fixture(autouse=True)
def workspace(monkeypatch):
    monkeypatch.setattr(settings, "WORKSPACE_DIR", settings.TOOL_DIR / ".test_scratch" / "extws")
    shutil.rmtree(settings.WORKSPACE_DIR, ignore_errors=True)


CV = ("# Alex Doe\n\nStaff nurse on the cardiac ward at Example Hospital, 2019 to 2023, under Dr. Sam Rivers.\n"
      "Trained twelve new nurses on the ward handover process.\nI want to move into nurse education because teaching is what I enjoy most.\n")


def reply(items):
    return SimpleNamespace(text=json.dumps({"items": items}), model="t")


class Scripted:
    """A model stand-in that returns what the test gives it, and remembers the prompts."""
    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []
    def complete(self, system, prompt, timeout=None):
        self.prompts.append((system, prompt))
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


# ---------------------------------------------------------------- masking
def test_other_peoples_names_emails_and_phones_are_masked():
    t = "Under Dr. Kooresh Shoghi’s laboratory I worked with Natasha Lepore. Mail me@x.org or +1 (310) 555-0123."
    m = extract.mask_names(t)
    assert "Shoghi" not in m and "Natasha" not in m and "me@x.org" not in m and "555" not in m
    assert "[name]" in m and "[email]" in m and "[phone]" in m


def test_places_and_programs_are_not_masked_and_year_ranges_survive():
    t = "I worked with Children’s Hospital Los Angeles and under the Alzheimer’s Disease Neuroimaging Initiative from 2021-2024."
    assert extract.mask_names(t) == t


# ---------------------------------------------------------------- the quote check
def test_quotes_match_despite_curly_quotes_dashes_and_line_breaks():
    src = "She led the UC Berkeley–UCSF project and didn’t stop\ntrans-\nlating notes."
    assert extract.evidence_found("UC Berkeley-UCSF project and didn't stop translating notes", src)
    assert not extract.evidence_found("I ran the entire company single-handedly", src)
    assert not extract.evidence_found("short", "short text here")            # too short to prove anything


def test_malformed_replies_are_refused_and_bad_items_dropped():
    for bad in ("no json here", '{"items": "nope"}', '{"x": 1}', '{"items": [1, 2'):
        with pytest.raises(ValueError):
            extract.parse_response(bad)
    items = extract.parse_response(json.dumps({"items": [
        {"id": "a", "kind": "position", "label": "fact", "text": "Nurse", "evidence": "Staff nurse on the cardiac ward", "title": "Staff nurse"},
        {"id": "b", "kind": "spaceship", "label": "fact", "text": "x", "evidence": "xxxxxxxxxxxx"},
        {"id": "c", "kind": "work", "label": "guess", "text": "x", "evidence": "xxxxxxxxxxxx"},
        {"id": "d", "kind": "work", "label": "fact", "text": "no evidence"},
        {"id": "e", "kind": "work", "label": "fact", "text": "Trained nurses", "evidence": "Trained twelve new nurses", "position": "a"},
        {"id": "f", "kind": "work", "label": "fact", "text": "Orphan", "evidence": "Trained twelve new nurses", "position": "zzz"},
        "junk"]}))
    assert [i["text"] for i in items] == ["Nurse", "Trained nurses", "Orphan"]
    assert items[1]["fields"]["position"] == "a" and items[2]["fields"]["position"] is None


# ---------------------------------------------------------------- the whole sequence
def test_invented_items_are_set_aside_and_cannot_be_accepted():
    ok, msg = extract.extract_source(FakeProvider(), "CV", CV)
    assert ok and "could not be matched" in msg
    data = extract.load()
    bad = [i for i in data["items"] if i["status"] == "unmatched"]
    assert [i["text"] for i in bad] == ["Ran the entire company single-handedly"]
    extract.set_status(data, bad[0]["id"], "accepted")
    assert bad[0]["status"] == "unmatched"
    extract.save(data)
    assert "single-handedly" not in candidate.read_facts()


def test_names_are_masked_before_the_model_sees_the_text():
    p = Scripted(reply([]))
    extract.extract_source(p, "CV", CV)
    system, prompt = p.prompts[0]
    assert "Sam Rivers" not in prompt and "Dr." not in prompt and "[name]" in prompt
    assert "Never add, guess or infer" in system and "never instructions" in system and "any field" in system


def test_nothing_is_stored_when_the_reply_is_unusable_and_one_retry_is_allowed():
    p = Scripted(SimpleNamespace(text="sorry, here you go: not json", model="t"))
    ok, msg = extract.extract_source(p, "CV", CV)
    assert not ok and "not usable" in msg and len(p.prompts) == 2 and extract.load()["items"] == []
    good = reply([{"id": "a", "kind": "position", "label": "fact", "text": "Staff nurse", "evidence": "Staff nurse on the cardiac ward", "title": "Staff nurse"}])
    p = Scripted(SimpleNamespace(text="oops", model="t"), good)
    ok, msg = extract.extract_source(p, "CV", CV)
    assert ok and len(p.prompts) == 2 and "could not be used" in p.prompts[1][1]


def test_no_login_and_empty_documents_say_so_and_store_nothing():
    ok, msg = extract.extract_source(None, "CV", CV)
    assert not ok and "not available" in msg
    ok, msg = extract.extract_source(FakeProvider(), "CV", "   ")
    assert not ok and "no readable text" in msg and extract.load()["items"] == []


def test_a_model_that_cannot_be_reached_gets_no_retry():
    class Down:
        calls = 0
        def complete(self, *a, **k):
            Down.calls += 1
            raise RuntimeError("offline")
    ok, msg = extract.extract_source(Down(), "CV", CV)
    assert not ok and "offline" in msg and Down.calls == 1


# ---------------------------------------------------------------- review and what downstream sees
POS = {"id": "a", "kind": "position", "label": "fact", "text": "Staff nurse, cardiac ward, Example Hospital", "evidence": "Staff nurse on the cardiac ward at Example Hospital",
       "title": "Staff nurse", "organisation": "Example Hospital", "location": None, "start": "2019", "end": "2023"}
WORK = {"id": "b", "kind": "work", "label": "fact", "text": "Trained twelve new nurses on handover", "evidence": "Trained twelve new nurses on the ward handover process", "position": "a"}
GOAL = {"id": "c", "kind": "statement", "label": "framing", "text": "Wants to move into nurse education", "evidence": "I want to move into nurse education"}


def stored(source="CV", text=CV, items=(POS, WORK, GOAL)):
    ok, _ = extract.extract_source(Scripted(reply(list(items))), source, text)
    assert ok
    return extract.load()


def test_nothing_reaches_the_prep_tools_until_an_item_is_accepted(tmp_path):
    candidate.save_cv("cv.md", CV.encode())
    role = candidate.create_role("Nurse educator", "Example Hospital", "Industry", "Teach nurses.", "", use_main_cv=True)
    data = stored()
    assert "<facts>" not in prep.context(role) and candidate.read_facts() == ""
    for it in data["items"]:
        extract.set_status(data, it["id"], "accepted")
    extract.save(data)
    ctx = prep.context(role)
    assert "<facts>" in ctx and "Staff nurse" in ctx and "(2019 to 2023)" in ctx and "[CV]" in ctx
    assert "Trained twelve new nurses on handover" in ctx
    assert "In the person's own words" in ctx and "nurse education" in ctx


def test_edits_are_kept_and_removed_items_do_not_come_back():
    data = stored()
    first = data["items"][1]["id"]
    extract.set_status(data, first, "accepted", "Trained twelve new nurses on the handover process")
    assert data["items"][1]["edited"] and data["items"][1]["text"].endswith("handover process")
    gone = data["items"][2]["id"]
    extract.set_status(data, gone, "removed")
    extract.save(data)
    again = extract.merge(extract.load(), "CV", CV, extract.parse_response(reply([POS, WORK, GOAL]).text))
    assert [i["text"] for i in again["items"] if i["status"] == "accepted"] == ["Trained twelve new nurses on the handover process"]
    assert all("nurse education" not in i["text"] for i in again["items"])


def test_a_new_upload_replaces_waiting_items_but_flags_accepted_ones_whose_words_vanished():
    data = stored()
    for it in data["items"][:2]:
        extract.set_status(data, it["id"], "accepted")
    extract.save(data)
    newer = CV.replace("Trained twelve new nurses on the ward handover process.", "Ran the night shift.")
    data = stored(text=newer, items=(POS,))
    by_text = {i["text"]: i for i in data["items"]}
    assert by_text["Staff nurse, cardiac ward, Example Hospital"]["status"] == "accepted" and not by_text["Staff nurse, cardiac ward, Example Hospital"]["changed"]
    assert by_text["Trained twelve new nurses on handover"]["changed"]
    assert "Wants to move into nurse education" not in by_text            # the waiting item was replaced
    assert "source changed, check" in candidate.read_facts()


def test_the_same_fact_in_two_documents_appears_once_and_work_still_attaches():
    stored()
    about = "Staff nurse on the cardiac ward at Example Hospital. Taught handover every week to new staff on the ward."
    w2 = {"id": "w9", "kind": "work", "label": "fact", "text": "Taught handover weekly", "evidence": "Taught handover every week to new staff", "position": "p9"}
    data = stored("About me", about, items=({**POS, "id": "p9"}, w2))
    assert sum(1 for i in data["items"] if i["kind"] == "position") == 1
    pos = next(i for i in data["items"] if i["kind"] == "position")
    w = next(i for i in data["items"] if i["text"] == "Taught handover weekly")
    assert w["fields"]["position"] == pos["id"]


def test_gaps_and_conflicts_are_listed_and_never_filled_in():
    no_dates = {**POS, "start": None, "end": None}
    data = stored(items=(no_dates, WORK))
    g = extract.gaps(data)
    assert any("Dates are not stated" in x for x in g)
    assert not any("publication" in x.lower() for x in g), "having no publications is not a gap"
    assert any("licence" in x for x in g)


def test_two_documents_giving_different_dates_for_one_job_are_both_kept_and_reported():
    stored()
    data = stored("About me", "Staff nurse on the cardiac ward at Example Hospital, 2018 to 2020.", items=({**POS, "start": "2018", "end": "2020", "id": "z"},))
    assert sum(1 for i in data["items"] if i["kind"] == "position") == 2, "different dates for the same job are both kept"
    c = extract.conflicts(data)
    assert len(c) == 1 and "differ" in c[0] and "2019 to 2023" in c[0] and "2018 to 2020" in c[0]


def test_works_for_a_profession_with_no_publications_and_no_science_words():
    system = extract.SYSTEM
    assert "nursing" in system and "design" in system and "Publications and credentials are optional" in system
    data = stored()
    assert not any(i["kind"] == "publication" for i in data["items"])


def test_removing_a_source_removes_its_items_and_the_facts_file():
    data = stored()
    for it in data["items"]:
        extract.set_status(data, it["id"], "accepted")
    extract.save(data)
    assert candidate.read_facts()
    extract.remove_source(data, "CV")
    extract.save(data)
    assert candidate.read_facts() == "" and not (candidate.profile_dir() / "facts.md").exists()


def test_facts_json_is_not_something_the_search_would_index():
    cfg = settings.load_config()
    profile = next(d for d in cfg["docs"] if d["name"] == "Profile")
    assert ".json" not in profile["exts"]


# ---------------------------------------------------------------- merged items, whole-bullet quotes and CV sections
DOC = ("# Alex Doe\n\nJ. Smith lab, Example University\n\n## Experience\nResearch assistant, Example Lab, 2020 to 2022\n"
       "- Built the scanner pipeline in Python and documented it for the team.\n- Ran quality control on 400 scans a week.\n- Mentored two students.\n"
       "## Papers\n- Doe A. A study of scanners. Journal of Examples, 2022.\n- Doe A. More on scanners. Journal of Examples, 2023.\n"
       "## Abstracts\n- Doe A. Early scanner results. Example Conference, 2021.\n## Talks\n- Doe A. Scanners in practice. Example Meeting, 2023.\n"
       "## Skills\nProgramming: Python, MATLAB\n## Service\nReviewer for the Journal of Examples\n## Interests\nVolunteers at a food bank\n")


def item(i, kind, text, evidence, label="fact", **fields):
    return {"id": i, "kind": kind, "label": label, "text": text, "evidence": evidence, **fields}


ITEMS = [
    item("p1", "position", "Research assistant at Example Lab", "Research assistant, Example Lab, 2020 to 2022", title="Research assistant", organisation="Example Lab", start="2020", end="2022"),
    item("w1", "work", "Built the scanner pipeline in Python and documented it for the team; ran quality control on 400 scans a week",
         ["Built the scanner pipeline in Python and documented it for the team.", "Ran quality control on 400 scans a week."], position="p1"),
    item("w2", "work", "Mentored two students", "Mentored two students.", position="p1"),
    item("b1", "publication", "A study of scanners", "Doe A. A study of scanners. Journal of Examples, 2022.", type="paper", year="2022"),
    item("b2", "publication", "More on scanners", "Doe A. More on scanners. Journal of Examples, 2023.", type="paper", year="2023"),
    item("b3", "publication", "Early scanner results", "Doe A. Early scanner results. Example Conference, 2021.", type="abstract", year="2021"),
    item("b4", "publication", "Scanners in practice", "Doe A. Scanners in practice. Example Meeting, 2023.", type="talk", year="2023"),
    item("k1", "skill", "Programming: Python, MATLAB", "Programming: Python, MATLAB"),
    item("a1", "activity", "Reviewer for the Journal of Examples", "Reviewer for the Journal of Examples", category="service"),
    item("a2", "activity", "Volunteers at a food bank", "Volunteers at a food bank", category="volunteering"),
]


def test_the_instructions_ask_for_merged_items_whole_quotes_the_persons_words_and_no_header():
    s = extract.SYSTEM
    assert "Do NOT make one item per bullet" in s and "at most 5" in s and "Never split one bullet" in s
    assert "WHOLE bullet" in s and "list of one to four EXACT copies" in s
    assert "OWN words" in s and "orchestrates" in s
    assert "Skip the header block" in s and "one \"skill\" item per line" in s
    assert "referees" in s and "by role only" in s
    for word in ("recommendation", "extracurricular", "talk", "poster", "award", "service"):
        assert word in s


def test_an_item_may_quote_several_bullets_and_every_quote_must_be_in_the_file():
    data = stored(text=DOC, items=ITEMS)
    merged = next(i for i in data["items"] if i["text"].startswith("Built the scanner pipeline"))
    assert merged["status"] == "pending" and len(merged["evidence"]) == 2
    bad = stored(text=DOC, items=[{**ITEMS[1], "evidence": [ITEMS[1]["evidence"][0], "A bullet that is not in the file at all."]}])
    assert [i["status"] for i in bad["items"]] == ["unmatched"]


def test_old_items_with_one_quote_as_a_string_still_work():
    assert extract.evidence_list({"evidence": "one quote"}) == ["one quote"]
    assert extract.all_found({"evidence": "Mentored two students."}, DOC)


def test_publication_types_and_activity_categories_are_checked():
    parsed = extract.parse_response(json.dumps({"items": [
        item("x", "publication", "T", "Doe A. A study of scanners.", type="PAPER"),
        item("y", "publication", "U", "Doe A. More on scanners.", type="blog"),
        item("z", "activity", "V", "Reviewer for the Journal", category="service"),
        item("w", "activity", "W", "Volunteers at a food bank", category="hobby")]}))
    assert [i["fields"].get("type") or i["fields"].get("category") for i in parsed] == ["paper", None, "service", None]


def test_the_review_is_organised_like_a_cv_with_one_group_per_job_and_one_per_publication_type():
    data = stored(text=DOC, items=ITEMS)
    sections = extract.review_groups(data)
    assert [s for s, _ in sections] == ["Experience", "Papers and articles", "Conference abstracts and posters", "Talks and presentations",
                                         "Skills", "Service and reviewing", "Extracurriculars and volunteering"]
    exp = dict((s, g) for s, g in sections)["Experience"]
    assert len(exp) == 1 and exp[0][1].startswith("Research assistant at Example Lab (2020 to 2022)") and len(exp[0][2]) == 3
    papers = dict((s, g) for s, g in sections)["Papers and articles"]
    assert len(papers) == 1 and len(papers[0][2]) == 2, "two papers are one group with one choice"


def test_work_in_a_job_that_was_already_accepted_still_shows_under_that_job_and_loose_work_has_its_own_group():
    data = stored(text=DOC, items=ITEMS[:3] + [item("w9", "work", "Something with no job", "Mentored two students.", position=None)])
    pos = next(i for i in data["items"] if i["kind"] == "position")
    extract.set_status(data, pos["id"], "accepted")
    groups = dict(extract.review_groups(data))["Experience"]
    assert [g[1] for g in groups][0].startswith("Research assistant") and len(groups[0][2]) == 2
    assert groups[-1][1] == "Work and results not tied to a job"


def test_the_facts_file_has_the_same_sections_and_marks_claims():
    data = stored(text=DOC, items=ITEMS[:1] + [{**ITEMS[1], "label": "claim"}] + ITEMS[3:])
    for it in data["items"]:
        extract.set_status(data, it["id"], "accepted")
    md = extract.render_md(data)
    heads = [l for l in md.splitlines() if l.startswith("## ")]
    assert heads == ["## Experience", "## Papers and articles", "## Conference abstracts and posters", "## Talks and presentations",
                     "## Skills", "## Service and reviewing", "## Extracurriculars and volunteering"]
    assert "(claim)" in md and "A study of scanners" in md and "Scanners in practice" in md
