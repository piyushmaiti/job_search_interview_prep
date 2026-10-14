"""Project maps, keyword-plus-meaning search, and the context added around retrieved code."""
import numpy as np

import settings
from codemap import CodeMap, build_project_map, parse_python
from engine import Engine
from index import Index, TfidfEmbedder, _doc_text, code_tokens, is_file_question
from ingest import Chunk, ingest_all

MODEL = '''"""model.py - the VAE and its loss."""


class VAE:
    """A tiny variational autoencoder."""

    def encode(self, x):
        return helper_scale(x)


def helper_scale(x):
    return x * 2


def vae_loss(recon, x, mu, logvar):
    """Reconstruction plus KL."""
    return recon + kl_term(mu, logvar)


def kl_term(mu, logvar):
    return mu + logvar
'''
TRAIN = '''"""Training script."""
from .model import vae_loss, VAE


def train_step(batch):
    return vae_loss(batch, batch, 0, 0)


if __name__ == "__main__":
    train_step(1)
'''


def make_project(tmp_path):
    proj = tmp_path / "proj"
    (proj / "pkg").mkdir(parents=True)
    (proj / "pkg" / "model.py").write_text(MODEL)
    (proj / "pkg" / "train.py").write_text(TRAIN)
    (proj / "README.md").write_text("# Demo\n\nA demo project.\n")
    return proj


def build_index(tmp_path, **project_extra):
    proj = make_project(tmp_path)
    cfg = settings.load_config()
    cfg["docs"] = []
    cfg["projects"] = [{"name": "proj", "path": str(proj), **project_extra}]
    extra = {}
    chunks = ingest_all(cfg, log=lambda *_: None, extra=extra)
    emb = TfidfEmbedder()
    matrix = emb.encode_docs([_doc_text(c) for c in chunks])
    meta = {"backend": "tfidf", "model": "tfidf", "built_at": "now", "n_chunks": len(chunks), "n_local": 0, "n_code": len(chunks)}
    return cfg, Index(chunks, matrix, emb, meta, CodeMap.merge(extra["codemap"]))


# ---------------------------------------------------------------- the map
def test_map_has_overview_and_a_card_per_python_file():
    chunks, data = build_project_map("proj", {"pkg/model.py": MODEL, "pkg/train.py": TRAIN}, ["README.md"])
    overview = next(c for c in chunks if c.section == "project map")
    assert overview.kind == "code" and overview.source == "proj/(project map)" and overview.label() == "proj/(project map)"
    assert "model.py" in overview.text and "train.py" in overview.text and "README.md" in overview.text
    assert "Entry points" in overview.text and "pkg/train.py" in overview.text and "approximate" in overview.text
    cards = {c.source: c for c in chunks if c.section == "file card"}
    assert set(cards) == {"proj/pkg/model.py", "proj/pkg/train.py"}
    assert cards["proj/pkg/model.py"].label() == "proj/pkg/model.py (file card)"
    assert "vae_loss" in data["symbols"]


def test_cards_link_imports_and_calls_by_name():
    chunks, _ = build_project_map("proj", {"pkg/model.py": MODEL, "pkg/train.py": TRAIN}, [])
    cards = {c.source: c.text for c in chunks if c.section == "file card"}
    assert "Imported by: pkg/train.py" in cards["proj/pkg/model.py"]
    assert "Imports from this project: pkg/model.py" in cards["proj/pkg/train.py"]
    assert "train_step calls vae_loss" in cards["proj/pkg/train.py"]
    assert "vae_loss is called by train_step" in cards["proj/pkg/model.py"]
    assert "class VAE" in cards["proj/pkg/model.py"] and "def encode(self, x)" in cards["proj/pkg/model.py"]


def test_purpose_skips_author_lines_and_bare_file_names():
    text = '"""three_d_skeleton.py\n\nAuthor: A. Person\nModified on Monday\n\nExtract the skeleton of a mask.\n"""\n'
    assert parse_python("get_skeleton.py", text).doc == "Extract the skeleton of a mask."


def test_generic_names_are_not_linked():
    files = {f"m{i}.py": "def run_thing():\n    return 1\n" for i in range(5)}
    files["caller.py"] = "def go():\n    return run_thing()\n"
    chunks, _ = build_project_map("p", files, [])
    card = next(c.text for c in chunks if c.source == "p/caller.py")
    assert "go calls" not in card, "a name defined in many files is too generic to link"


def test_map_text_is_redacted_and_bad_python_is_skipped():
    chunks, _ = build_project_map("p", {"a.py": '"""api_key = "abcdefghijklmnop" lives here"""\n\ndef f():\n    pass\n',
                                        "broken.py": "def (:\n"}, [])
    assert "abcdefghijklmnop" not in " ".join(c.text for c in chunks)
    assert parse_python("broken.py", "def (:\n") is None
    assert {c.source for c in chunks if c.section == "file card"} == {"p/a.py"}


def test_a_huge_project_still_gives_a_short_overview():
    files = {f"d{i % 7}/mod{i}.py": f"def f{i}():\n    pass\n" for i in range(400)}
    chunks, _ = build_project_map("big", files, [f"docs/n{i}.md" for i in range(300)])
    assert len(next(c for c in chunks if c.section == "project map").text) <= 6000


def test_imaging_projects_get_a_data_files_chunk_only_when_asked(tmp_path):
    import gzip
    from tests.test_wholeproject import nifti1
    proj = make_project(tmp_path)
    (proj / "scan.nii.gz").write_bytes(gzip.compress(nifti1() + b"\0" * 2000))
    cfg = settings.load_config()
    cfg["docs"] = []
    cfg["projects"] = [{"name": "proj", "path": str(proj)}]
    assert not [c for c in ingest_all(cfg, log=lambda *_: None) if c.section == "data files"]
    cfg["projects"][0]["nifti_headers"] = True
    data = [c for c in ingest_all(cfg, log=lambda *_: None) if c.section == "data files"]
    assert len(data) == 1 and "shape 82x64x130" in data[0].text and data[0].label() == "proj/(data files)"


# ---------------------------------------------------------------- search
def test_identifier_tokens_keep_the_whole_name_and_its_parts():
    assert {"vae_loss", "vae", "loss", "vaeloss"} <= set(code_tokens("VAELoss vae_loss compute2x"))


class StubEmbedder:
    """Pretends to be the meaning-based model."""
    kind = "st"
    name = "stub"

    def encode_query(self, q):
        return np.array([1.0, 0.0])


def test_exact_names_are_found_even_when_the_meaning_match_misses():
    texts = [f"Paper passage number {i} about unrelated imaging topics." for i in range(30)]
    texts.append("def compute_zeta_score(volume):\n    return volume.mean()")
    chunks = [Chunk(f"c{i}", "code" if i == 30 else "local", "g", "f", t) for i, t in enumerate(texts)]
    matrix = np.array([[1.0, 0.0]] * 30 + [[-1.0, 0.0]])          # the target is the worst meaning match
    idx = Index(chunks, matrix, StubEmbedder(), {"backend": "st", "model": "stub", "built_at": "now", "n_chunks": 31,
                                                  "n_local": 30, "n_code": 1})
    assert "c30" in [c.id for c in idx.search("where is compute_zeta_score defined?", 3)]   # the meaning match alone never returns it


def test_a_function_named_in_the_question_is_pulled_in(tmp_path):
    cfg, idx = build_index(tmp_path)
    hits = idx.search("explain vae_loss please", 3, {"proj"})
    assert any(c.line_start and "def vae_loss" in c.text for c in hits[:2])


# ---------------------------------------------------------------- context around code
def hit_for(idx, text):
    return next(c for c in idx.chunks if text in c.text and c.section is None)


def test_expand_adds_overview_card_callee_and_stays_in_selected_groups(tmp_path):
    cfg, idx = build_index(tmp_path)
    hit = hit_for(idx, "def vae_loss")
    out = idx.expand_code([hit], {"proj"})
    sections = [c.section for c in out[1:]]
    assert "project map" in sections and "file card" in sections
    assert any("def kl_term" in c.text for c in out), "the function called by the hit is included"
    assert idx.expand_code([hit], {"other"}) == [hit], "nothing from groups that were not selected"
    assert len(idx.expand_code([hit], {"proj"}, max_extra=1)) == 2


def test_expand_respects_the_size_budget(tmp_path):
    cfg, idx = build_index(tmp_path)
    hit = hit_for(idx, "def vae_loss")
    assert idx.expand_code([hit], {"proj"}, max_chars=len(hit.text) + 10) == [hit]


def test_papers_only_hits_are_left_alone(tmp_path, chunks):
    cfg, idx = build_index(tmp_path)
    local = [c for c in chunks if c.kind == "local"]
    assert idx.expand_code(local, None) == local


def test_consent_lists_the_map_and_nothing_is_sent_before_it(tmp_path, providers):
    cfg, idx = build_index(tmp_path)
    asked = []

    def deny(req):
        asked.append(req)
        assert all(not p.calls for p in providers.values()), "provider called before consent"
        return None
    res = Engine(cfg, idx, providers).ask("what does vae_loss do", mode="claude", groups={"proj"}, consent=deny)
    assert asked and "proj/(project map)" in asked[0].files and "proj/pkg/model.py" in asked[0].files
    assert res.cancelled and all(not p.calls for p in providers.values())


def test_codemap_survives_save_and_load(tmp_path, monkeypatch):
    cfg, idx = build_index(tmp_path)
    monkeypatch.setattr(settings, "INDEX_DIR", settings.TOOL_DIR / ".test_scratch" / "idx")
    idx.save()
    again = Index.load(cfg)
    assert again.codemap.defined("vae_loss") and again.codemap.callees("proj/pkg/train.py", 1, 20)


# ---------------------------------------------------------------- ranking fixes
def tfidf_index(texts_by_source, low_priority=None):
    chunks = [Chunk(f"c{i}", "code", "g", src, t) for i, (src, t) in enumerate(texts_by_source)]
    emb = TfidfEmbedder()
    matrix = emb.encode_docs([_doc_text(c) for c in chunks])
    meta = {"backend": "tfidf", "model": "tfidf", "built_at": "now", "n_chunks": len(chunks), "n_local": 0, "n_code": len(chunks)}
    return Index(chunks, matrix, emb, meta, low_priority=low_priority)


def test_word_endings_match_each_other():
    for word in ("trains", "training", "trained", "train"):
        assert "train" in code_tokens(word)
    assert "script" in code_tokens("scripts") and "vae_loss" in code_tokens("vae_loss")


def test_logs_rank_lower_but_stay_searchable():
    docs = [("g/train_log_v1.txt", "train autoencoder loss"),
            ("g/train_ae.py", "train autoencoder loss with many other unrelated words diluting the match here and there")]
    plain = tfidf_index(docs).search("train autoencoder loss", 2)
    weighted = tfidf_index(docs, ["*train_log*"]).search("train autoencoder loss", 2)
    assert plain[0].source == "g/train_log_v1.txt"
    assert [c.source for c in weighted] == ["g/train_ae.py", "g/train_log_v1.txt"]


def test_which_file_questions_return_one_piece_per_file():
    docs = [("g/a.py", "train model loop one"), ("g/a.py", "train model loop two"), ("g/a.py", "train model loop three"),
            ("g/b.py", "train model script"), ("g/c.py", "model training entry")]
    idx = tfidf_index(docs)
    assert len({c.source for c in idx.search("train model", 4)}) < 3 or len(idx.search("train model", 4)) == 4
    assert [c.source for c in idx.search("train model", 4, diverse=True)].count("g/a.py") == 1


def test_which_file_questions_are_recognised():
    assert is_file_question("Which scripts train the autoencoder?")
    assert is_file_question("where is vae_loss defined")
    assert is_file_question("what files generate the nifti volumes?")
    assert not is_file_question("how does the loss work?")


# ---------------------------------------------------------------- short documents are sent whole
def doc_index(sizes):
    chunks = []
    for src, n_pieces, size in sizes:
        chunks += [Chunk(f"{src}-{i}", "local", src, src, f"{src} piece {i} " + "word " * size, page=i + 1) for i in range(n_pieces)]
    emb = TfidfEmbedder()
    meta = {"backend": "tfidf", "model": "tfidf", "built_at": "now", "n_chunks": len(chunks), "n_local": len(chunks), "n_code": 0}
    return Index(chunks, emb.encode_docs([_doc_text(c) for c in chunks]), emb, meta)


def test_a_short_document_is_sent_whole_when_any_piece_matches():
    idx = doc_index([("cv.pdf", 6, 100), ("other.pdf", 3, 100)])
    hits = [idx.chunks[1]]                                          # one piece of the CV matched
    out = idx.expand_small_docs(hits, None, max_doc_chars=10000)
    assert [c.id for c in out] == ["cv.pdf-1", "cv.pdf-0", "cv.pdf-2", "cv.pdf-3", "cv.pdf-4", "cv.pdf-5"]   # the rest, in reading order
    assert not any(c.source == "other.pdf" for c in out)


def test_long_documents_and_unselected_sources_are_not_expanded():
    idx = doc_index([("long.pdf", 6, 100)])
    assert idx.expand_small_docs([idx.chunks[0]], None, max_doc_chars=100) == [idx.chunks[0]]
    assert idx.expand_small_docs([idx.chunks[0]], {"elsewhere"}, max_doc_chars=10000) == [idx.chunks[0]]


def test_expansion_has_a_size_budget_and_can_be_turned_off():
    idx = doc_index([("cv.pdf", 6, 100)])
    out = idx.expand_small_docs([idx.chunks[0]], None, max_doc_chars=10000, max_added=1200)
    assert 1 < len(out) < 6
    assert idx.expand_small_docs([idx.chunks[0]], None, max_doc_chars=0) == [idx.chunks[0]]


def test_the_engine_sends_the_whole_short_document(providers):
    import settings as s
    idx = doc_index([("cv.pdf", 5, 50)])
    eng = Engine(s.load_config(), idx, providers)
    eng.ask("piece word", mode="claude", groups={"cv.pdf"}, consent=lambda r: None, k=1)
    assert len(providers["claude"].calls[0]["ids"]) == 5


# ---------------------------------------------------------------- questions about the user pull in their CV
def test_pinned_rules_are_user_defined_and_bad_patterns_are_ignored():
    from index import matches_any
    rules = [r"\b(my|mine)\b[^.?!]{0,40}\bexperience\b", r"\bour guidelines\b", "(unclosed"]
    assert matches_any("How does my imaging experience fit?", rules) and matches_any("What do OUR GUIDELINES say?", rules)
    assert not matches_any("How does the loss work?", rules)
    assert not matches_any("anything", []) and not matches_any("anything", None), "no rules means nothing is pinned"


def test_pinned_documents_come_whole_and_only_from_selected_sources():
    idx = doc_index([("Alex_Doe_CV.pdf", 4, 40), ("jobs.md", 3, 40)])
    pinned = idx.pinned_docs(["*cv*", "*resume*"], None)
    assert [c.id for c in pinned] == [f"Alex_Doe_CV.pdf-{i}" for i in range(4)]
    assert idx.pinned_docs(["*cv*"], {"jobs.md"}) == [], "an unticked CV is never added"
    assert len(idx.pinned_docs(["*cv*"], None, max_added=500)) < 4


def pinned_engine(providers, **retrieval):
    import settings as s
    cfg = s.load_config()
    cfg["retrieval"] = {**cfg["retrieval"], "pinned_docs": [], "pinned_when": [], **retrieval}
    return Engine(cfg, doc_index([("Alex_Doe_CV.pdf", 4, 40), ("jobs.md", 3, 40)]), providers)


def test_nothing_is_pinned_by_default(providers):
    eng = pinned_engine(providers)
    r = eng.ask("jobs piece: how does my experience fit this role?", mode="claude", groups={"Alex_Doe_CV.pdf", "jobs.md"},
                consent=lambda q: None, k=2)
    assert not any("pinned" in n for n in r.notes)
    assert sum("Alex_Doe_CV.pdf" in x for x in providers["claude"].calls[0]["refs"]) < 4


def test_a_pinned_rule_sends_the_document_whole_when_its_phrase_matches(providers):
    eng = pinned_engine(providers, pinned_docs=["*cv*"], pinned_when=[r"\bmy\b.*\bexperience\b"])
    groups = {"Alex_Doe_CV.pdf", "jobs.md"}
    r = eng.ask("jobs piece: how does my experience fit this role?", mode="claude", groups=groups, consent=lambda q: None, k=2)
    assert sum("Alex_Doe_CV.pdf" in x for x in providers["claude"].calls[0]["refs"]) == 4
    assert any("pinned-document rule matched" in n and "Alex_Doe_CV.pdf" in n for n in r.notes)
    eng.ask("jobs piece: what does the role require?", mode="claude", groups=groups, consent=lambda q: None, k=2)
    assert not any("Alex_Doe_CV.pdf" in x for x in providers["claude"].calls[1]["refs"][:2])


def test_an_unticked_pinned_document_is_never_added(providers):
    eng = pinned_engine(providers, pinned_docs=["*cv*"], pinned_when=["experience"])
    eng.ask("jobs piece: my experience", mode="claude", groups={"jobs.md"}, consent=lambda q: None, k=2)
    assert not any("Alex_Doe_CV.pdf" in x for x in providers["claude"].calls[0]["refs"])


# ---------------------------------------------------------------- the instructions are neutral
def test_the_model_instructions_are_neutral_and_take_an_optional_role():
    from providers.base import SYSTEM_PROMPT, system_prompt
    assert "job" not in SYSTEM_PROMPT.lower() and SYSTEM_PROMPT.startswith("You are a research assistant.")
    assert system_prompt("") == SYSTEM_PROMPT and system_prompt("   ") == SYSTEM_PROMPT
    assert system_prompt("reviewing the amyloid PET literature").endswith("How the user uses this tool: reviewing the amyloid PET literature")


def test_the_role_reaches_every_provider():
    from providers import build_providers
    cfg = settings.load_config()
    cfg["assistant_role"] = "reviewing the literature"
    provs = build_providers(cfg, {"claude_access": "login"})
    cmd = provs["claude"].command(False)
    assert "How the user uses this tool: reviewing the literature" in cmd[cmd.index("--system-prompt") + 1]
    assert all(p.opts.get("role") == "reviewing the literature" for p in provs.values())
    cfg["assistant_role"] = ""
    plain = build_providers(cfg, {"claude_access": "login"})["claude"].command(False)
    assert "How the user uses" not in plain[plain.index("--system-prompt") + 1]
