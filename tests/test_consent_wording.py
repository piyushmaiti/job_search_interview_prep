"""The consent dialog says what is being sent, and names the model chosen in Settings."""
from consent import ConsentRequest, dialog_title, included_line, settings_line

CV = ["profile/cv.pdf", "roles/ai-imaging-scientist/cv.pdf", "roles/ai-imaging-scientist/job_description.md"]
CODE = ["my_project/app.py:1-40", "my_project/README.md"]


def req(files, carried=False):
    return ConsentRequest(files, [], carried=carried)


def test_title_for_cv_and_job_text_does_not_say_code():
    assert dialog_title(req(CV)) == "This question will send your CV, profile and job text to a model"


def test_title_for_project_code_is_unchanged():
    assert dialog_title(req(CODE)) == "This question will send project code to a model"


def test_title_for_both_names_both():
    t = dialog_title(req(CODE + CV))
    assert "project code" in t and "CV" in t


def test_title_for_code_carried_from_an_earlier_turn():
    r = req(["(project code from an earlier turn in this chat)"], carried=True)
    assert dialog_title(r) == "This question will send project code to a model"


def test_included_line_counts_each_kind():
    assert included_line(req(CV)) == "Included: your CV, About you, publications and job text (3 files)."
    assert included_line(req(CODE)) == "Included: project code (2 files)."
    both = included_line(req(CODE + CV))
    assert "project code (2 files)" in both and "(3 files)" in both


def test_included_line_for_earlier_turn_code_only():
    r = req(["(project code from an earlier turn in this chat)"], carried=True)
    assert included_line(r) == "Included: project code from an earlier turn in this chat."


def test_settings_line_names_the_choice_and_says_it_is_not_permission():
    s = settings_line("openai", "openai (gpt-6.1-sol, cloud)")
    assert "openai (gpt-6.1-sol, cloud)" in s and "does not allow anything to be sent" in s
    assert "Auto" in settings_line("auto") and "Compare" in settings_line("compare")
