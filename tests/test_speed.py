"""Caches that keep the app quick: nothing unchanged is re-read, re-embedded or re-probed."""
import numpy as np

import projectctx
from providers import ollama


def test_unchanged_file_is_read_once(tmp_path, monkeypatch):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    calls = []
    real = projectctx.settings.redact
    monkeypatch.setattr(projectctx.settings, "redact", lambda t: (calls.append(1), real(t))[1])
    projectctx._read_redacted(f); projectctx._read_redacted(f)
    assert len(calls) == 1
    f.write_text("x = 22222\n")
    assert projectctx._read_redacted(f) == "x = 22222\n"


def test_ollama_models_are_probed_once_in_a_row(monkeypatch):
    n = []

    class R:
        def json(self): return {"models": [{"name": "m1"}]}

    monkeypatch.setattr(ollama.requests, "get", lambda *a, **k: (n.append(1), R())[1])
    ollama._MODELS_CACHE.update(at=0.0, names=[])
    assert ollama.installed_models() == ["m1"] and ollama.installed_models() == ["m1"]
    assert len(n) == 1
