"""Whole-project context: what is read, what is never read, routing, trimming and consent."""
import gzip
import struct

import pytest

import settings
from engine import Engine
from index import Index, TfidfEmbedder, _doc_text
from ingest import Chunk, iter_files
from projectctx import (DATA_FILES, ProjectFile, WholeProject, collect, data_files_summary, estimate_tokens,
                        read_nifti_header)
from router import decide_whole
from sessions import Session

SENTINEL = "SENTINEL_PATIENT_RECORD_9f3a"
CAPS = {"claude": 1000, "gemini": 5000}


def nifti1(shape=(82, 64, 130), voxel=(0.2, 0.2, 0.2), dtype=4, big=False):
    head = bytearray(348)
    e = ">" if big else "<"
    head[0:4] = struct.pack(e + "i", 348)
    dim = [len(shape), *shape] + [1] * (7 - len(shape))
    head[40:56] = struct.pack(e + "8h", *dim)
    head[70:72] = struct.pack(e + "h", dtype)
    head[76:108] = struct.pack(e + "8f", 1.0, *voxel, *([1.0] * (7 - len(voxel))))
    head[123] = 2                                                   # millimetres
    return bytes(head)


def make_project(tmp_path):
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "patient-data").mkdir()
    (proj / "resources").mkdir()
    (proj / "README.md").write_text("# Demo\n\nA demo project.\n")
    (proj / "src" / "model.py").write_text("def vae_loss():\n    password = 'hunter2hunter2'\n    return 1\n")
    (proj / "src" / "notes.txt").write_text("Plain notes.\n")
    (proj / "src" / "diagram.tex").write_text("\\documentclass{article}\n" * 20)
    (proj / "src" / "train_log_v1.txt").write_text("epoch 1 loss 0.5\n" * 50)
    (proj / "requirements.txt").write_text("numpy==1.0\n" * 50)
    (proj / "src" / ".env").write_text("API_KEY=abcdefghijklmnop\n")
    (proj / "src" / "config_secrets.py").write_text("KEY='zzzzzzzzzzzz'\n")
    (proj / "src" / "big.py").write_text("x=1\n" * 200000)
    (proj / "patient-data" / "p.py").write_text(f"x = '{SENTINEL}'\n")
    (proj / "patient-data" / "p.md").write_text(f"{SENTINEL}\n")
    (proj / "resources" / "scan.nii").write_bytes(nifti1() + SENTINEL.encode() * 10)
    (proj / "resources" / "scan2.nii.gz").write_bytes(gzip.compress(nifti1(big=True) + b"\0" * 5000))
    (proj / "resources" / "photo.jpg").write_bytes(b"\xff\xd8" + SENTINEL.encode())
    (proj / "resources" / "pic.png").write_bytes(b"\x89PNG" + SENTINEL.encode())
    (proj / "resources" / "arr.npy").write_bytes(SENTINEL.encode())
    return proj


def cfg_for(proj, **project_extra):
    c = settings.load_config()
    c["docs"] = []
    c["projects"] = [{"name": "proj", "path": str(proj), **project_extra}]
    return c


# ---------------------------------------------------------------- what is read
def test_images_imaging_files_and_arrays_are_never_read_even_if_config_lists_them(tmp_path):
    proj = make_project(tmp_path)
    cfg = cfg_for(proj)
    cfg["project_exts"] = [".py", ".md", ".txt", ".nii", ".gz", ".jpg", ".jpeg", ".png", ".npy", ".svg"]
    names = {p.name for p in iter_files(proj, {e for e in cfg["project_exts"]}, cfg)}
    assert names == {"model.py", "README.md", "notes.txt", "train_log_v1.txt", "requirements.txt"}


def test_whole_project_reads_only_allowed_files_and_redacts(tmp_path):
    cfg = cfg_for(make_project(tmp_path))
    wp = collect(cfg, ["proj"])
    text = "\n".join(f.text for f in wp.files)
    assert {f.rel for f in wp.files} == {"README.md", "src/model.py", "src/notes.txt"}
    assert SENTINEL not in text and "abcdefghijklmnop" not in text and "hunter2hunter2" not in text
    assert "zzzzzzzzzzzz" not in text and "x=1" not in text
    assert sorted(wp.skipped) == ["proj/requirements.txt", "proj/src/diagram.tex", "proj/src/train_log_v1.txt"]


def test_readme_comes_first_and_data_files_summary_last(tmp_path):
    wp = collect(cfg_for(make_project(tmp_path), nifti_headers=True), ["proj"])
    assert wp.files[0].rel == "README.md" and wp.files[-1].rel == DATA_FILES


def test_unticked_projects_are_not_read(tmp_path):
    assert collect(cfg_for(make_project(tmp_path)), ["other"]).files == []


# ---------------------------------------------------------------- NIfTI headers
def test_nifti_header_fields_little_big_endian_and_gzip(tmp_path):
    (tmp_path / "a.nii").write_bytes(nifti1((10, 20, 30), (1.5, 1.5, 3.0), dtype=16))
    h = read_nifti_header(tmp_path / "a.nii")
    assert h["shape"] == (10, 20, 30) and h["voxel"] == (1.5, 1.5, 3.0) and h["dtype"] == "float32" and h["unit"] == "mm"
    (tmp_path / "b.nii.gz").write_bytes(gzip.compress(nifti1((4, 5), (2.0, 2.0), big=True)))
    assert read_nifti_header(tmp_path / "b.nii.gz")["shape"] == (4, 5)
    (tmp_path / "bad.nii").write_bytes(b"not a nifti file" * 40)
    assert read_nifti_header(tmp_path / "bad.nii") is None
    (tmp_path / "short.nii").write_bytes(b"\0" * 10)
    assert read_nifti_header(tmp_path / "short.nii") is None


def test_data_files_summary_lists_names_sizes_headers_and_never_content(tmp_path):
    proj = make_project(tmp_path)
    s = data_files_summary(proj, cfg_for(proj))
    assert "resources/scan.nii" in s and "shape 82x64x130" in s and "int16" in s and "scan2.nii.gz" in s
    assert "photo.jpg" not in s and ".jpg files" in s and ".png files" in s       # images are only counted
    assert SENTINEL not in s and "patient" not in s.lower().replace("patient_record", "")


def test_summary_is_empty_without_imaging_files(tmp_path):
    (tmp_path / "x.py").write_text("x=1")
    assert data_files_summary(tmp_path, settings.load_config()) == ""


# ---------------------------------------------------------------- trimming
def test_trim_drops_the_largest_files_first_and_keeps_the_data_summary():
    wp = WholeProject([ProjectFile("p", "README.md", "a" * 400), ProjectFile("p", "big.py", "b" * 4000),
                       ProjectFile("p", "mid.py", "c" * 1200), ProjectFile("p", DATA_FILES, "d" * 100)])
    dropped = wp.trim_to(estimate_tokens(1800))
    assert dropped == ["p/big.py"] and {f.rel for f in wp.files} == {"README.md", "mid.py", DATA_FILES}
    wp.trim_to(0)
    assert [f.rel for f in wp.files] == [DATA_FILES]


# ---------------------------------------------------------------- routing
def test_whole_project_routing():
    both = {"claude", "gemini", "ollama"}
    assert decide_whole("auto", both, 500, CAPS).providers == ["claude"]
    assert decide_whole("auto", both, 2000, CAPS).providers == ["gemini"]
    assert decide_whole("auto", {"claude"}, 2000, CAPS).providers == ["claude"]        # trimmed later
    assert decide_whole("auto", {"gemini", "ollama"}, 10, CAPS).providers == ["gemini"]
    assert decide_whole("claude", both, 2000, CAPS).providers == ["claude"]            # manual wins
    assert decide_whole("compare", both, 10, CAPS).providers == ["claude", "gemini"]
    with pytest.raises(ValueError, match="too small"):
        decide_whole("ollama", both, 10, CAPS)
    with pytest.raises(ValueError):
        decide_whole("auto", {"ollama"}, 10, CAPS)


# ---------------------------------------------------------------- engine, consent
def build(tmp_path, providers, **project_extra):
    cfg = cfg_for(make_project(tmp_path), **project_extra)
    cfg["whole_project"] = {"max_tokens": {"claude": 100_000, "gemini": 700_000}}
    emb = TfidfEmbedder()
    chunks = [Chunk("d1", "local", "docs/a.md", "docs/a.md", "Notes about imaging scanners and harmonization.")]
    matrix = emb.encode_docs([_doc_text(c) for c in chunks])
    idx = Index(chunks, matrix, emb, {"backend": "tfidf", "model": "tfidf", "built_at": "now", "n_chunks": 1, "n_local": 1, "n_code": 0})
    return Engine(cfg, idx, providers)


def test_nothing_is_sent_before_consent_and_cancel_sends_nothing(tmp_path, providers):
    eng = build(tmp_path, providers)
    seen = []

    def deny(req):
        seen.append(req)
        assert all(not p.calls for p in providers.values()), "provider called before consent"
        return None
    r = eng.ask("how does this work?", mode="auto", groups={"proj"}, consent=deny, code_mode="whole")
    assert seen and seen[0].scope == "whole" and "Whole project" in seen[0].detail and "tokens" in seen[0].detail
    assert "proj/src/model.py" in seen[0].files and not any("patient" in f for f in seen[0].files)
    assert r.cancelled and all(not p.calls for p in providers.values())


def test_ollama_is_not_offered_for_a_whole_project(tmp_path, providers):
    eng = build(tmp_path, providers)
    got = {}

    def look(req):
        got["opts"] = {o.key: o.available for o in req.options}
        return None
    eng.ask("q", groups={"proj"}, consent=look, code_mode="whole")
    assert got["opts"]["ollama"] is False and got["opts"]["claude"] is True


def test_the_approved_provider_receives_every_allowed_file_and_nothing_private(tmp_path, providers):
    eng = build(tmp_path, providers)
    r = eng.ask("how does this work?", mode="auto", groups={"proj"}, consent=lambda req: ["claude"], code_mode="whole")
    call = providers["claude"].calls[0]
    assert not providers["gemini"].calls and r.route.providers == ["claude"]
    assert "def vae_loss" in call["text"] and "A demo project" in call["text"] and "Plain notes" in call["text"]
    for secret in (SENTINEL, "abcdefghijklmnop", "hunter2hunter2", "zzzzzzzzzzzz", "x=1\nx=1"):
        assert secret not in call["text"]
    assert call["refs"][0].startswith("proj/README.md") and any("model.py" in x for x in call["refs"])
    assert any("low-value" in n for n in r.notes)


def test_a_follow_up_after_a_whole_project_turn_asks_again(tmp_path, providers):
    eng = build(tmp_path, providers)
    sess, asked = Session(id="whole-test"), []

    def approve(req):
        asked.append(req.carried)
        return ["claude"]
    eng.ask("how does this work?", groups={"proj"}, session=sess, consent=approve, code_mode="whole")
    assert sess.turns[-1].used_code
    eng.ask("and the loss?", groups=set(), session=sess, consent=approve, memory=True)
    assert asked == [False, True]


def test_over_the_limit_the_largest_files_are_dropped_and_the_user_is_told(tmp_path, providers):
    eng = build(tmp_path, providers)
    eng.cfg["whole_project"]["max_tokens"]["claude"] = 20
    r = eng.ask("q", mode="claude", groups={"proj"}, consent=lambda req: ["claude"], code_mode="whole")
    assert any("largest files were left out" in n for n in r.notes)
    assert estimate_tokens(len(providers["claude"].calls[0]["text"])) <= 25


def test_choosing_a_different_provider_than_routed_refits_the_project(tmp_path, providers):
    eng = build(tmp_path, providers)
    eng.cfg["whole_project"]["max_tokens"] = {"claude": 20, "gemini": 700_000}
    r = eng.ask("q", mode="auto", groups={"proj"}, consent=lambda req: ["claude"], code_mode="whole")
    assert r.route.providers == ["gemini"]                                       # routed to the larger context
    assert any("largest files" in n for n in r.notes) and estimate_tokens(len(providers["claude"].calls[0]["text"])) <= 25


def test_no_project_ticked_falls_back_to_retrieval_with_a_note(tmp_path, providers):
    eng = build(tmp_path, providers)
    r = eng.ask("q", mode="claude", groups=set(), consent=lambda req: ["claude"], code_mode="whole")
    assert any("needs a project ticked" in n for n in r.notes)


def test_image_requests_skip_whole_project(tmp_path, providers):
    eng = build(tmp_path, providers)
    r = eng.ask("make an infographic of the model", mode="auto", groups={"proj"}, consent=lambda req: None, code_mode="whole")
    assert any("whole project was skipped" in n for n in r.notes)
