"""Profile and role handling: uploads, profile building, roles, and how they reach the search."""
import io
import zipfile
from types import SimpleNamespace

import pytest

import candidate
import settings
from providers.fake import FakeProvider


@pytest.fixture(autouse=True)
def workspace(monkeypatch):
    monkeypatch.setattr(settings, "WORKSPACE_DIR", settings.TOOL_DIR / ".test_scratch" / "workspace")


def make_docx(text: str) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in text.split("\n"))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f'<w:document xmlns:w="x"><w:body>{body}</w:body></w:document>')
    return buf.getvalue()


def test_docx_cv_is_saved_as_text_and_found():
    p = candidate.save_cv("My CV.docx", make_docx("Alex Doe\nImaging scientist & modeller"))
    assert p.name == "cv.md" and "Imaging scientist & modeller" in p.read_text()
    assert candidate.cv_path() == p and candidate.has_profile_cv()


def test_replacing_the_cv_removes_the_old_one():
    candidate.save_cv("a.md", b"# one")
    candidate.save_cv("b.txt", b"two")
    assert [p.name for p in candidate.profile_dir().glob("cv.*")] == ["cv.txt"]


@pytest.mark.parametrize("name,data", [("x.exe", b"MZ"), ("x.pdf", b"not a pdf"), ("x.docx", b"not a zip")])
def test_bad_uploads_are_refused(name, data):
    with pytest.raises(ValueError):
        candidate.save_cv(name, data)
    assert not candidate.has_profile_cv()


def test_publication_names_cannot_escape_the_folder():
    p = candidate.save_publication("../../evil.md", b"# paper")
    assert p.parent == candidate.profile_dir() / "publications" and p.name == "evil.md"
    candidate.delete_publication("../../evil.md")
    assert candidate.list_publications() == []


def test_about_text_round_trips_and_clears():
    candidate.save_about("I want to work on imaging.")
    assert candidate.read_about() == "I want to work on imaging."
    candidate.save_about("  ")
    assert candidate.read_about() == ""


def test_build_profile_uses_the_documents_and_keeps_contact_details_out_of_the_instructions():
    candidate.save_cv("cv.md", b"# Alex Doe\nPyTorch since 2019.")
    candidate.save_about("I like hard problems.")
    candidate.save_publication("paper.md", b"# A paper on harmonization")
    fake = FakeProvider()
    ok, msg = candidate.build_profile(fake.complete)
    assert ok, msg
    call = fake.calls[-1]
    assert "PyTorch since 2019" in call["prompt"] and "I like hard problems" in call["prompt"] and "harmonization" in call["prompt"]
    assert "never invent" in call["system"].lower() and "contact details" in call["system"].lower()
    assert candidate.read_profile().startswith("# Profile") and candidate.profile_status()["built"]
    assert not candidate.profile_status()["stale"]
    candidate.save_about("Changed.")
    assert candidate.profile_status()["stale"], "changed inputs make the profile stale"


def test_a_bad_model_answer_changes_nothing():
    candidate.save_cv("cv.md", b"# Alex")
    candidate.write_profile("# Profile: old\n\n## Experience\n" + "x" * 300)
    junk = lambda system, prompt, timeout=None: SimpleNamespace(text="Sure! Here you go.", model="m")
    ok, msg = candidate.build_profile(junk)
    assert not ok and candidate.read_profile().startswith("# Profile: old")
    assert candidate.build_profile(None)[0] is False


def test_build_profile_needs_a_cv():
    ok, msg = candidate.build_profile(FakeProvider().complete)
    assert not ok and "CV" in msg


def test_create_role_saves_the_job_description_as_markdown():
    candidate.save_cv("cv.md", b"# Main CV")
    r = candidate.create_role("Imaging Scientist", "Acme", "Industry", "We need PyTorch.", "Show transfer.", use_main_cv=True)
    assert r.slug == "imaging-scientist-acme"
    assert (r.folder / "job_description.md").read_text().endswith("We need PyTorch.\n")
    assert candidate.job_description_text(r) == "We need PyTorch." and candidate.intent_text(r) == "Show transfer."
    assert r.cv().read_text() == "# Main CV"
    assert candidate.get_role(r.slug).kind == "Industry"


def test_roles_get_their_own_cv_and_unique_names_and_can_be_deleted():
    a = candidate.create_role("Postdoc", "", "Academic", "jd", "", "cv.md", b"# CV for the postdoc")
    b = candidate.create_role("Postdoc", "", "Academic", "jd2", "", "cv.txt", b"other")
    assert a.slug != b.slug and a.cv().read_text() == "# CV for the postdoc"
    assert [r.slug for r in candidate.list_roles()] == [a.slug, b.slug]
    candidate.delete_role(a.slug)
    assert [r.slug for r in candidate.list_roles()] == [b.slug]


@pytest.mark.parametrize("kw,fragment", [
    (dict(name=" ", kind="Industry", jd="x", cv=True), "title"),
    (dict(name="R", kind="Other", jd="x", cv=True), "Industry or Academic"),
    (dict(name="R", kind="Industry", jd=" ", cv=True), "job description"),
    (dict(name="R", kind="Industry", jd="x", cv=False), "CV"),
])
def test_create_role_asks_for_what_is_missing(kw, fragment):
    with pytest.raises(ValueError, match=fragment):
        candidate.create_role(kw["name"], "", kw["kind"], kw["jd"], "", "cv.md" if kw["cv"] else "", b"# cv" if kw["cv"] else None)
    assert candidate.list_roles() == []


def test_main_cv_option_needs_a_main_cv():
    with pytest.raises(ValueError, match="no main CV"):
        candidate.create_role("R", "", "Industry", "jd", "", use_main_cv=True)
    assert candidate.list_roles() == []


def test_role_lookup_rejects_path_tricks():
    assert candidate.get_role("../x") is None and candidate.get_role("") is None


def test_other_roles_sources_are_hidden():
    assert candidate.visible("library/a.pdf", "r1") and candidate.visible("profile/cv.pdf", "r1")
    assert candidate.visible("roles/r1/cv.pdf", "r1")
    assert not candidate.visible("roles/r2/cv.pdf", "r1") and not candidate.visible("roles/r1/cv.pdf", "")
    assert candidate.is_workspace("profile/x.md") and not candidate.is_workspace("library/x.md")


def test_configured_pins_your_documents_and_tells_the_model_not_to_invent(cfg):
    candidate.save_cv("cv.md", b"# CV")
    r = candidate.create_role("R", "Co", "Academic", "jd", "", use_main_cv=True)
    c = candidate.configured(cfg, r)
    assert "profile.md" in c["retrieval"]["pinned_docs"] and "job_description.md" in c["retrieval"]["pinned_docs"]
    assert "." in c["retrieval"]["pinned_when"]
    assert "never invent" in c["assistant_role"] and "R, Co" in c["assistant_role"] and "academic" in c["assistant_role"]
    assert cfg["retrieval"]["pinned_docs"] == [], "the original config is not changed"


def test_your_documents_reach_every_answer_for_the_role(cfg, monkeypatch):
    import index as index_mod
    from engine import Engine
    from index import Index

    monkeypatch.setattr(index_mod, "StEmbedder", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("keyword index only")))
    monkeypatch.setattr(settings, "INDEX_DIR", settings.TOOL_DIR / ".test_scratch" / "index")
    candidate.save_cv("cv.md", b"# Alex Doe\nBuilt a harmonization model in PyTorch.")
    candidate.write_profile("# Profile: Alex\n\n## Experience\n### Scientist, Lab\n- Wrote the harmonization code.\n" + "x" * 200)
    r = candidate.create_role("Imaging Scientist", "Acme", "Industry", "Requirements: Kubernetes and Go experience.", "", use_main_cv=True)
    other = candidate.create_role("Other job", "Else", "Industry", "Requirements: Fortran.", "", use_main_cv=True)
    c = dict(cfg)
    ws = candidate.workspace()
    c["docs"] = [{"name": "Profile", "path": str(ws / "profile"), "exts": [".pdf", ".md", ".txt"]},
                 {"name": "Roles", "path": str(ws / "roles"), "exts": [".pdf", ".md", ".txt"]}]
    c["projects"] = []
    idx = Index.build(c)
    groups = {g for g in idx.groups() if candidate.visible(g, r.slug)}
    assert not any(g.startswith(f"roles/{other.slug}/") for g in groups) and any(g.startswith(f"roles/{r.slug}/") for g in groups)
    fake = FakeProvider(label="claude", locality="cloud")
    eng = Engine(candidate.configured(c, r), idx, {"claude": fake})
    eng.ask("What should I say about weekends?", mode="claude", web=False, groups=groups, memory=False)
    sent = fake.calls[-1]["text"]
    assert "harmonization" in sent, "the profile and CV go with a question that does not mention them"
    assert "Kubernetes" in sent and "Fortran" not in sent, "this role's job description goes, another role's does not"


def test_source_labels_are_readable():
    f = candidate.source_label
    assert f("profile/profile.md") == "My profile" and f("profile/cv.pdf") == "My main CV" and f("profile/about.md") == "About me"
    assert f("profile/publications/my-paper.pdf") == "Publication: my-paper"
    assert f("roles/r1/job_description.md") == "Job description" and f("roles/r1/cv.md") == "CV for this role" and f("roles/r1/intent.md") == "My intent"
    assert f("library/notes.md") == "notes.md"
