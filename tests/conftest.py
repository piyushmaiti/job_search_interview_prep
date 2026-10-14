import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import settings  # noqa: E402
from index import Index, TfidfEmbedder, _doc_text  # noqa: E402
from ingest import Chunk  # noqa: E402
from providers.fake import FakeProvider  # noqa: E402


@pytest.fixture
def cfg():
    return settings.load_config()


def make_index(chunks):
    emb = TfidfEmbedder()
    matrix = emb.encode_docs([_doc_text(c) for c in chunks])
    return Index(chunks, matrix, emb, {"backend": "tfidf", "model": "tfidf", "built_at": "now", "n_chunks": len(chunks), "n_local": 0, "n_code": 0})


@pytest.fixture
def chunks():
    return [
        Chunk("a1", "local", "library/p.pdf", "library/p.pdf", "Scanner harmonization across sites uses style transfer.", page=3),
        Chunk("a2", "local", "notes/job.md", "notes/job.md", "Required: Python and PyTorch with reproducible pipelines.", section="Required"),
        Chunk("c1", "code", "tau_proj", "tau_proj/model.py", "def vae_loss(recon, x, mu, logvar): return recon + kl  # scanner harmonization loss", line_start=10, line_end=14),
    ]


@pytest.fixture
def providers():
    return {
        "claude": FakeProvider(label="claude", locality="cloud"),
        "gemini": FakeProvider(label="gemini", locality="cloud"),
        "ollama": FakeProvider(label="ollama", locality="local"),
    }


@pytest.fixture
def engine(cfg, chunks, providers):
    from engine import Engine
    return Engine(cfg, make_index(chunks), providers)


@pytest.fixture(autouse=True)
def _isolated_sessions_and_outputs(monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_CONFIG", settings.TOOL_DIR / ".test_scratch" / "no_local_config.yaml")
    """Tests write to a scratch folder inside this folder, never to the real sessions/ or outputs/."""
    import shutil
    scratch = settings.TOOL_DIR / ".test_scratch"
    monkeypatch.setattr(settings, "KNOWLEDGE_DIR", scratch / "knowledge")
    monkeypatch.setattr(settings, "SESSIONS_DIR", scratch / "sessions")
    monkeypatch.setattr(settings, "OUTPUTS_DIR", scratch / "outputs")
    yield
    shutil.rmtree(scratch, ignore_errors=True)
