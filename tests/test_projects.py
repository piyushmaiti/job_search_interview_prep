"""Adding and removing code projects from the app."""
import os
from pathlib import Path

import pytest
import yaml

import projects
import settings


@pytest.fixture(autouse=True)
def local_config(monkeypatch):
    cfg = settings.TOOL_DIR / ".test_scratch" / "cfg" / "config.local.yaml"      # the tool only writes inside its own folder
    cfg.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "LOCAL_CONFIG", cfg)
    monkeypatch.delenv("INTERVIEW_PREP_LAN", raising=False)


@pytest.fixture
def code(tmp_path):
    d = tmp_path / "work" / "my_project"
    d.mkdir(parents=True)
    (d / "model.py").write_text("def f():\n    return 1\n")
    (d / "README.md").write_text("# notes\n")
    (d / "logo.png").write_bytes(b"\x89PNG")
    return d


def test_add_saves_the_project_and_keeps_other_settings(code):
    settings.LOCAL_CONFIG.write_text("assistant_role: helping me\nprojects:\n  - {name: old, path: /tmp/old}\n")
    name = projects.add(str(code))
    data = yaml.safe_load(settings.LOCAL_CONFIG.read_text())
    assert name == "my_project" and data["assistant_role"] == "helping me"
    assert data["projects"] == [{"name": "old", "path": "/tmp/old"}, {"name": "my_project", "path": str(code.resolve())}]
    assert "my_project" in [p["name"] for p in settings.load_config()["projects"]]


def test_a_custom_name_is_used_and_must_be_new_and_tidy(code, tmp_path):
    assert projects.add(str(code), "tau_reads") == "tau_reads"
    other = tmp_path / "work" / "other"
    other.mkdir()
    with pytest.raises(ValueError, match="already exists"):
        projects.add(str(other), "tau_reads")
    with pytest.raises(ValueError, match="short name"):
        projects.add(str(other), "bad name!")
    with pytest.raises(ValueError, match="already added"):
        projects.add(str(code), "second_name")


@pytest.mark.parametrize("raw,msg", [("", "Give the folder"), ("relative/dir", "full path"), ("/nonexistent/zzz", "does not exist"),
                                      ("/", "not your whole computer"), ("~", "not your whole computer")])
def test_bad_paths_are_refused_in_plain_words(raw, msg):
    with pytest.raises(ValueError, match=msg):
        projects.check_path(raw)


def test_a_file_credential_folders_and_patient_folders_are_refused(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x")
    with pytest.raises(ValueError, match="does not exist, or it is a file"):
        projects.check_path(str(f))
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    with pytest.raises(ValueError, match="keys or credentials"):
        projects.check_path(str(ssh))
    for bad in ("patient-data", "Patient Data v2"):
        d = tmp_path / bad / "code"
        d.mkdir(parents=True)
        with pytest.raises(ValueError, match="patient data"):
            projects.check_path(str(d))


def test_this_tools_own_folder_and_anything_containing_it_is_refused():
    for p in (settings.TOOL_DIR, settings.TOOL_DIR.parent, settings.TOOL_DIR / "providers"):
        with pytest.raises(ValueError, match="overlaps this tool"):
            projects.check_path(str(p))


def test_a_symlink_into_a_private_area_is_resolved_and_refused(tmp_path):
    link = tmp_path / "innocent"
    link.symlink_to(settings.TOOL_DIR / "providers")
    with pytest.raises(ValueError, match="overlaps this tool"):
        projects.check_path(str(link))


def test_preview_counts_readable_and_skipped_files(code):
    pv = projects.preview(code.resolve())
    assert pv.readable == 2 and pv.skipped >= 1 and "model.py" in pv.examples and pv.size_bytes > 0


def test_remove_only_changes_the_list_and_never_the_code(code):
    projects.add(str(code))
    projects.remove("my_project")
    assert yaml.safe_load(settings.LOCAL_CONFIG.read_text())["projects"] == []
    assert (code / "model.py").exists()
    with pytest.raises(ValueError, match="was not added"):
        projects.remove("my_project")


def test_nothing_can_be_added_or_removed_while_the_app_is_open_to_other_devices(code, monkeypatch):
    projects.add(str(code))
    monkeypatch.setenv("INTERVIEW_PREP_LAN", "1")
    assert projects.lan_mode()
    with pytest.raises(ValueError, match="this computer alone"):
        projects.add(str(code), "second")
    with pytest.raises(ValueError, match="this computer alone"):
        projects.remove("my_project")


def test_default_names_are_tidy():
    assert projects.default_name(Path("/a/My Project (v2)")) == "My_Project_v2"
    assert projects.default_name(Path("/a/...")) == "project"
