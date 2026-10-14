"""Rules the tool must never break: what is indexed, what is written, and when code may leave the machine."""
import os

import pytest

import settings
from ingest import iter_files, ingest_all, read_python
from providers.ollama import OllamaProvider, is_cloud_model
from sessions import Session

SENTINEL = "SENTINEL_PATIENT_RECORD_9f3a"


def make_tree(tmp_path):
    proj = tmp_path / "proj"
    (proj / "patient-data" / "abc").mkdir(parents=True)
    (proj / "patient-data" / "abc" / "notes.md").write_text(f"# x\n{SENTINEL}\n")
    (proj / "patient-data" / "abc" / "loader.py").write_text(f"x = '{SENTINEL}'\n")
    (proj / "Patient_Data").mkdir()
    (proj / "Patient_Data" / "a.py").write_text(f"y = '{SENTINEL}'\n")
    (proj / "src").mkdir()
    (proj / "src" / "ok.py").write_text("def hello():\n    return 1\n")
    (proj / "src" / ".env").write_text("API_KEY=abcdefghijklmnop\n")
    (proj / "src" / "config_secrets.py").write_text("KEY='zzzzzzzzzzzz'\n")
    (proj / "src" / "big.py").write_text("x=1\n" * 200000)
    (proj / "node_modules").mkdir()
    (proj / "node_modules" / "dep.py").write_text("z=1\n")
    return proj


def cfg_for(proj, **extra):
    c = settings.load_config()
    c["docs"] = []
    c["projects"] = [{"name": "proj", "path": str(proj)}]
    c.update(extra)
    return c


def test_patient_data_never_indexed_even_if_config_tries(tmp_path):
    proj = make_tree(tmp_path)
    cfg = cfg_for(proj, extra_excludes=[], project_exts=[".py", ".md"])
    cfg["exclude_dirs"] = []            # attempts to override are ignored
    cfg["hard_exclude"] = []
    chunks = ingest_all(cfg, log=lambda *_: None)
    assert chunks, "normal files should still be indexed"
    assert not any(SENTINEL in c.text for c in chunks)
    assert not any("patient" in c.source.lower() for c in chunks)


def test_source_inside_patient_data_is_refused(tmp_path):
    proj = make_tree(tmp_path)
    with pytest.raises(ValueError):
        list(iter_files(proj / "patient-data", {".py"}, settings.load_config()))


def test_walk_does_not_enter_excluded_dirs(tmp_path, monkeypatch):
    proj = make_tree(tmp_path)
    visited = []
    real_walk = os.walk

    def spy(top, *a, **k):
        for dirpath, dirnames, filenames in real_walk(top, *a, **k):
            visited.append(dirpath)
            yield dirpath, dirnames, filenames

    monkeypatch.setattr(os, "walk", spy)
    list(iter_files(proj, {".py", ".md"}, settings.load_config()))
    assert not any("patient" in v.lower() for v in visited)
    assert not any("node_modules" in v for v in visited)


def test_secret_files_and_large_files_skipped(tmp_path):
    proj = make_tree(tmp_path)
    names = {p.name for p in iter_files(proj, {".py"}, settings.load_config())}
    assert names == {"ok.py"}


def test_secret_values_redacted():
    assert "abcdefghijkl" not in settings.redact('api_key = "abcdefghijklmnop"')
    assert "REDACTED" in settings.redact("password: 'hunter2hunter2'")


def test_python_is_parsed_not_executed(tmp_path):
    p = tmp_path / "danger.py"
    p.write_text("import os\nos.makedirs('/tmp/should_not_exist_xyz', exist_ok=True)\n\n"
                 "def a():\n    return 1\n\nclass B:\n    def m(self):\n        return 2\n")
    chunks = read_python(p, "proj/danger.py", "proj", settings.load_config())
    assert not os.path.exists("/tmp/should_not_exist_xyz")
    spans = {(c.line_start, c.line_end) for c in chunks}
    assert (4, 5) in spans and (7, 9) in spans          # function a, class B with correct lines


def test_writes_outside_job_search_refused(tmp_path):
    with pytest.raises(PermissionError):
        settings.assert_writable(tmp_path / "x.json")
    assert settings.assert_writable(settings.OUTPUTS_DIR)


def test_ollama_cloud_model_is_not_called_local():
    assert is_cloud_model("kimi-k3:cloud") and not is_cloud_model("llama3.1:8b")
    p = OllamaProvider.__new__(OllamaProvider)
    p.model, p.opts = "kimi-k3:cloud", {}
    assert p.locality == "cloud" and "NOT on this machine" in p.label()
    p.model = "llama3.1:8b"
    assert p.locality == "local" and "stays on this machine" in p.label()


def test_session_saves_inside_job_search(tmp_path, monkeypatch):
    s = Session(id="pytest-temp")
    path = s.save()
    try:
        assert settings.ROOT in path.parents
    finally:
        path.unlink(); path.with_suffix(".md").unlink()


@pytest.mark.parametrize("raw,leak", [
    ("password: abc123def456ghi", "abc123def456"),                       # unquoted YAML
    ("DB_PASSWORD=Sup3rS3cret99", "Sup3rS3cret99"),                      # .env style
    ('api_key = "abcdefghijklmnop"', "abcdefghijkl"),
    ("url: postgres://admin:pa55w0rd@db.example.com/x", "pa55w0rd"),
    ("key = " + "sk-" + "ant-abcdefghijklmnopqrstu", "abcdefghijklmnopqrstu"),
    ("t = eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijk", "eyJzdWIiOiIx"),
    ("k = " + "AI" + "zaSyA1234567890abcdefghijklmnopqrstuv", "A1234567890abcdef"),
    ("-----BEGIN RSA " + "PRIVATE KEY-----\nMIIabc\n-----END RSA " + "PRIVATE KEY-----", "MIIabc"),
])
def test_redaction_covers_common_secret_shapes(raw, leak):
    assert leak not in settings.redact(raw)


@pytest.mark.parametrize("code", ["token = get_token()", "def f(token: str, tokenizer):", "password_field = None"])
def test_redaction_leaves_ordinary_code_alone(code):
    assert settings.redact(code) == code


def test_no_secret_shaped_text_in_the_built_index():
    p = settings.INDEX_DIR / "chunks.json"
    if not p.exists():
        pytest.skip("index not built")
    text = p.read_text()
    for rx in settings.SECRET_PATTERNS[2:]:
        assert not [m for m in rx[0].finditer(text) if "REDACTED" not in m.group(0)], rx[0].pattern


def test_session_delete_removes_both_files_and_refuses_odd_ids():
    s = Session(id="pytest-del")
    s.save()
    d = settings.SESSIONS_DIR
    assert (d / "pytest-del.json").exists() and (d / "pytest-del.md").exists()
    Session.delete("pytest-del")
    assert not (d / "pytest-del.json").exists() and not (d / "pytest-del.md").exists()
    Session.delete("pytest-del")                      # deleting twice is harmless
    for bad in ("", "../escape", "a/b", "x.json"):
        with pytest.raises(ValueError):
            Session.delete(bad)


def test_group_path_finds_files_and_stays_inside_the_source(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("hello")
    (tmp_path / "outside.md").write_text("secret")
    (tmp_path / "docs" / "patient-data").mkdir()
    (tmp_path / "docs" / "patient-data" / "p.md").write_text("no")
    cfg = {"docs": [{"name": "Docs", "path": str(tmp_path / "docs"), "exts": [".md"]}]}
    assert settings.group_path(cfg, "docs/a.md") == tmp_path / "docs" / "a.md"
    assert settings.group_path(cfg, "docs/../outside.md") is None
    assert settings.group_path(cfg, "docs/patient-data/p.md") is None
    assert settings.group_path(cfg, "docs/missing.md") is None
