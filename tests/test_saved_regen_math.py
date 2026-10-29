"""Formulas drawn as LaTeX, and the saved questions and answers (the screens are tested in test_app.py)."""
import pytest

import candidate
import safetext
import saved
import settings


# ---------------------------------------------------------------- formulas
def test_formulas_written_with_backslash_brackets_become_dollar_formulas():
    out = safetext.math(r"Loss \(L = \sum_i x_i\) and \[ a^2 + b^2 = c^2 \] done")
    assert "$L = \\sum_i x_i$" in out and "$$a^2 + b^2 = c^2$$" in out
    assert "\\(" not in out and "\\[" not in out


def test_money_is_not_read_as_a_formula_but_real_formulas_are_left_alone():
    out = safetext.math("The pay is $120k, or $5 million, and $5 and $10. Also $x + 5$ and $2x$.")
    assert "\\$120k" in out and "\\$5 million" in out and "\\$5 and \\$10" in out
    assert "$x + 5$" in out and "$2x$" in out


def test_a_formula_that_starts_with_a_decimal_is_not_read_as_money():
    for f in (r"$0.036 \pm 0.014$", r"$0.5 x + 1$", r"$3.14 \times r^2$"):
        assert safetext.math(f"was relatively high ({f}) and") == f"was relatively high ({f}) and", f
    o, c = safetext.MARK_OPEN, safetext.MARK_CLOSE
    assert safetext.math_with_marks(rf"high ($0.036 \pm 0.014$){o}0{c}.") == rf"high ($0.036 \pm 0.014$){o}0{c}."
    out = safetext.math("It costs $0.50. Then $1.5 million, and $9.")        # real money with decimals is still escaped
    assert "\\$0.50." in out and "\\$1.5 million" in out and "\\$9." in out


def test_code_is_never_changed():
    text = "Use `re.compile(r'\\(\\d+\\)')` or:\n```\nprice = '$5'\nm = r'\\(a\\)'\n```"
    assert safetext.math(text) == text


def test_a_citation_mark_inside_a_formula_moves_to_just_after_it():
    o, c = safetext.MARK_OPEN, safetext.MARK_CLOSE
    out = safetext.math_with_marks(f"We have $x = a{o}0{c} + b$ here{o}1{c}.")
    assert out == f"We have $x = a + b${o}0{c} here{o}1{c}."


# ---------------------------------------------------------------- saved items
@pytest.fixture
def role(monkeypatch):
    ws = settings.TOOL_DIR / ".test_scratch" / "workspace_saved"
    monkeypatch.setattr(settings, "WORKSPACE_DIR", ws)
    candidate.save_cv("cv.md", b"# Alex Doe\nImaging scientist.")
    r = candidate.create_role("Saved role", "Acme", "Industry", "We need a scientist.", "Show my work.", use_main_cv=True)
    yield r
    candidate.delete_role(r.slug)


def test_a_saved_item_can_be_listed_and_deleted_and_goes_with_its_role(role):
    a = saved.save(role, "research", {"question": "What is VAE?", "answers": [{"provider": "claude", "segments": []}]})
    b = saved.save(role, "mock", {"question": "Tell me about a time...", "answer": "I did X.", "feedback": "## What worked"})
    assert [i["id"] for i in saved.items(role, "research")] == [a]
    assert saved.items(role, "mock")[0]["answer"] == "I did X."
    assert saved.items(role, "research")[0]["saved_at"]
    saved.delete(role, a)
    assert saved.items(role, "research") == [] and len(saved.items(role, "mock")) == 1
    candidate.delete_role(role.slug)
    assert not (saved._dir(role)).exists(), "deleting the role deletes what was kept for it"


def test_two_saves_in_the_same_second_get_different_ids(role):
    ids = {saved.save(role, "research", {"question": "q", "answers": []}) for _ in range(3)}
    assert len(ids) == 3


def test_only_ids_made_by_the_tool_can_be_deleted(role):
    for bad in ("", "../x", "research-../../x", "research-20260101-000000/../../x", "mock", "notes-20260101-000000"):
        with pytest.raises(ValueError):
            saved.delete(role, bad)
    with pytest.raises(ValueError):
        saved.save(role, "other", {})


def test_nothing_is_saved_outside_the_tool_folder(role, monkeypatch):
    monkeypatch.setattr(settings, "WORKSPACE_DIR", settings.TOOL_DIR.parent / "outside_the_tool")
    with pytest.raises(Exception):
        saved.save(role, "research", {"question": "q", "answers": []})
