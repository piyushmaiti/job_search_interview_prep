"""Embeddings and a small brute-force vector index.

The corpus is a few thousand chunks, so numpy cosine search is enough. If the sentence-transformers
model cannot be loaded (for example, offline on first run), TF-IDF is used instead.
"""
from __future__ import annotations

import fnmatch
import json
import pickle
import re
import time

import numpy as np

import settings
from codemap import CodeMap, MAP_SECTIONS
from ingest import Chunk, ingest_all

settings.setup_env()


class StEmbedder:
    kind = "st"

    def __init__(self, name: str):
        from sentence_transformers import SentenceTransformer

        self.name = name
        self.model = SentenceTransformer(name, device="cpu")

    def encode_docs(self, texts: list[str]) -> np.ndarray:
        return self.model.encode(texts, batch_size=32, normalize_embeddings=True, show_progress_bar=False)

    def encode_query(self, q: str) -> np.ndarray:
        return self.model.encode([q], normalize_embeddings=True, show_progress_bar=False)[0]


class TfidfEmbedder:
    kind = "tfidf"
    name = "tfidf"

    def __init__(self, vectorizer=None):
        from sklearn.feature_extraction.text import TfidfVectorizer

        self.vec = vectorizer or TfidfVectorizer(sublinear_tf=True, ngram_range=(1, 2), min_df=1, max_features=60000)

    def encode_docs(self, texts):
        return self.vec.fit_transform(texts)

    def encode_query(self, q):
        return self.vec.transform([q])


_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_WORDS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


FILE_QUESTION = re.compile(
    r"\b(which|what|list|show|name)\b.*\b(files?|scripts?|modules?|notebooks?|classes|functions?)\b|\bwhere (is|are|does|do)\b.*\b(defined|implemented|located|stored|live|lives)\b", re.I)


def is_file_question(q: str) -> bool:
    """Questions whose answer is one or more files ("which scripts train the model?"): these want many different files."""
    return bool(FILE_QUESTION.search(q))


def matches_any(question: str, patterns: list[str]) -> bool:
    """True if the question matches any of the user's regular expressions (case-insensitive). A bad pattern is ignored."""
    for p in patterns or []:
        try:
            if re.search(p, question, re.I):
                return True
        except re.error:
            continue
    return False


def _stem(w: str) -> str:
    """Light word-ending removal so train, trains, trained and training match each other."""
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)]
    return w


def code_tokens(text: str) -> list[str]:
    """Words for keyword search. An identifier is kept whole and also split: vae_loss -> vae_loss, vae, loss.
    Every word is also added without its ending (training -> train)."""
    out: list[str] = []
    for m in _IDENT.finditer(text):
        w = m.group(0)
        words = [w.lower()]
        parts = [p.lower() for p in _WORDS.findall(w.replace("_", " "))]
        if len(parts) > 1:
            words += parts
        for x in words:
            out.append(x)
            if _stem(x) != x:
                out.append(_stem(x))
    return out


def _doc_text(c: Chunk) -> str:
    return f"{c.source}\n{c.section or ''}\n{c.text}"


class Index:
    def __init__(self, chunks: list[Chunk], matrix, embedder, meta: dict, codemap: CodeMap | None = None,
                 low_priority: list[str] | None = None):
        self.chunks, self.matrix, self.embedder, self.meta = chunks, matrix, embedder, meta
        self.codemap = codemap or CodeMap()
        self.low_priority = [g.lower() for g in (low_priority or [])]   # file name patterns that rank lower (logs, diagrams)
        self._kw = None            # keyword index, built on first search
        self._maps = None          # lookups used to add context around code hits

    # ----- build / persist
    @classmethod
    def build(cls, cfg: dict, log=print) -> "Index":
        extra: dict = {}
        chunks = ingest_all(cfg, log, extra)
        if not chunks:
            raise RuntimeError("No sources found to index.")
        texts = [_doc_text(c) for c in chunks]
        try:
            emb = StEmbedder(cfg["retrieval"]["embed_model"])
            log(f"embedding {len(chunks)} chunks with {emb.name} (CPU)...")
        except Exception as e:
            log(f"warning: embedding model unavailable ({type(e).__name__}); using TF-IDF instead")
            emb = TfidfEmbedder()
        matrix = cls._encode_reusing(emb, texts)
        meta = {
            "backend": emb.kind, "model": emb.name, "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "n_chunks": len(chunks),
            "n_local": sum(c.kind == "local" for c in chunks),
            "n_code": sum(c.kind == "code" for c in chunks),
        }
        idx = cls(chunks, matrix, emb, meta, CodeMap.merge(extra.get("codemap", [])), cfg["retrieval"].get("low_priority"))
        idx.save()
        return idx

    @staticmethod
    def _encode_reusing(emb, texts: list[str]) -> np.ndarray:
        """Embed only the texts the last index did not already hold (same embedding model); the rest are copied over."""
        if emb.kind != "st":
            return emb.encode_docs(texts)
        d = settings.INDEX_DIR
        old: dict[str, np.ndarray] = {}
        try:
            meta = json.loads((d / "meta.json").read_text())
            if meta.get("backend") == "st" and meta.get("model") == emb.name:
                old_chunks = [Chunk(**x) for x in json.loads((d / "chunks.json").read_text())]
                vecs = np.load(d / "vectors.npy")
                if len(old_chunks) == len(vecs):
                    old = {_doc_text(c): vecs[i] for i, c in enumerate(old_chunks)}
        except (OSError, ValueError, KeyError, TypeError):
            old = {}
        todo = [i for i, t in enumerate(texts) if t not in old]
        new = emb.encode_docs([texts[i] for i in todo]) if todo else None
        dim = len(next(iter(old.values()))) if old else (new.shape[1] if new is not None else 0)
        out = np.zeros((len(texts), dim), dtype=np.float32)
        for i, t in enumerate(texts):
            if t in old:
                out[i] = old[t]
        for j, i in enumerate(todo):
            out[i] = new[j]
        return out

    def save(self):
        d = settings.assert_writable(settings.INDEX_DIR)
        d.mkdir(parents=True, exist_ok=True)
        (d / "chunks.json").write_text(json.dumps([c.to_dict() for c in self.chunks]), encoding="utf-8")
        (d / "meta.json").write_text(json.dumps(self.meta, indent=2), encoding="utf-8")
        (d / "codemap.json").write_text(self.codemap.to_json(), encoding="utf-8")
        if self.embedder.kind == "st":
            np.save(d / "vectors.npy", self.matrix)
        else:
            with open(d / "tfidf.pkl", "wb") as f:
                pickle.dump({"vec": self.embedder.vec, "matrix": self.matrix}, f)

    @classmethod
    def load(cls, cfg: dict) -> "Index | None":
        d = settings.INDEX_DIR
        if not (d / "meta.json").exists():
            return None
        meta = json.loads((d / "meta.json").read_text())
        chunks = [Chunk(**x) for x in json.loads((d / "chunks.json").read_text())]
        if meta["backend"] == "st":
            emb = StEmbedder(meta["model"])
            matrix = np.load(d / "vectors.npy")
        else:
            with open(d / "tfidf.pkl", "rb") as f:  # written only by this tool, inside this folder/.index
                obj = pickle.load(f)
            emb, matrix = TfidfEmbedder(obj["vec"]), obj["matrix"]
        cm = d / "codemap.json"
        return cls(chunks, matrix, emb, meta, CodeMap(json.loads(cm.read_text())) if cm.exists() else None,
                   cfg["retrieval"].get("low_priority"))

    @classmethod
    def ensure(cls, cfg: dict, rebuild: bool = False, log=print) -> "Index":
        if not rebuild:
            idx = cls.load(cfg)
            if idx:
                return idx
        return cls.build(cfg, log)

    # ----- query
    def scores(self, query: str) -> np.ndarray:
        q = self.embedder.encode_query(query)
        if self.embedder.kind == "st":
            return self.matrix @ q
        return np.asarray((self.matrix @ q.T).todense()).ravel()

    def _keyword_scores(self, query: str) -> np.ndarray:
        if self._kw is None:
            from sklearn.feature_extraction.text import TfidfVectorizer
            vec = TfidfVectorizer(analyzer=code_tokens, sublinear_tf=True)
            self._kw = (vec, vec.fit_transform([_doc_text(c) for c in self.chunks]))
        vec, mat = self._kw
        return np.asarray((mat @ vec.transform([query]).T).todense()).ravel()

    def _weight(self, c: Chunk, diverse: bool) -> float:
        """Logs and diagrams count half, so they do not crowd out code. In a which-file question, file cards count more."""
        name = c.source.rsplit("/", 1)[-1].lower()
        if any(fnmatch.fnmatch(name, g) for g in self.low_priority):
            return 0.5
        return 1.15 if diverse and c.section == "file card" else 1.0

    def search(self, query: str, k: int, groups: set[str] | None = None, diverse: bool = False) -> list[Chunk]:
        """Closest passages. With the embedding model, its ranking is fused with a keyword ranking (reciprocal
        rank fusion), so exact names such as vae_loss are found even when the meaning-based match misses them.
        With diverse=True (a which-file question) at most one piece per file is returned."""
        ok = np.array([groups is None or c.group in groups for c in self.chunks])
        dense = np.where(ok, self.scores(query), -1.0)
        n = max(60, 4 * k)
        ranked = [[int(i) for i in np.argsort(-dense)[:n] if dense[i] > 0]]
        if self.embedder.kind == "st":
            kw = np.where(ok, self._keyword_scores(query), -1.0)
            ranked.append([int(i) for i in np.argsort(-kw)[:n] if kw[i] > 0])
        fused: dict[int, float] = {}
        for order in ranked:
            for r, i in enumerate(order):
                fused[i] = fused.get(i, 0.0) + 1.0 / (60 + r)
        score = {i: s * self._weight(self.chunks[i], diverse) for i, s in fused.items()}
        top = [self.chunks[i] for i in sorted(score, key=score.get, reverse=True)]
        out = self._named_symbols(query, groups)
        out += [c for c in top if c not in out]
        if diverse:
            seen: set[str] = set()
            out = [c for c in out if not (c.source in seen or seen.add(c.source))]
        return out[:k]

    def pinned_docs(self, patterns: list[str], groups: set[str] | None, max_added: int = 60000) -> list[Chunk]:
        """Every piece of the documents whose file name matches a pattern, in reading order, from selected sources only."""
        out, used = [], 0
        for c in self.chunks:
            name = c.source.rsplit("/", 1)[-1].lower()
            if (c.kind == "local" and (groups is None or c.group in groups) and any(fnmatch.fnmatch(name, p.lower()) for p in patterns)
                    and used + len(c.text) <= max_added):
                out.append(c)
                used += len(c.text)
        return out

    def expand_small_docs(self, hits: list[Chunk], groups: set[str] | None, max_doc_chars: int = 24000,
                          max_added: int = 60000) -> list[Chunk]:
        """A short paper or note (a CV, a job description) is sent whole when any of its pieces matches, because a few
        pieces of a short document leave the model guessing at the rest. Long documents are left to retrieval."""
        size: dict[str, int] = {}
        pieces: dict[str, list[Chunk]] = {}
        for c in self.chunks:
            if c.kind == "local":
                size[c.source] = size.get(c.source, 0) + len(c.text)
                pieces.setdefault(c.source, []).append(c)
        have = {c.id for c in hits}
        out, added = list(hits), 0
        for src in dict.fromkeys(c.source for c in hits if c.kind == "local"):
            if size.get(src, 0) > max_doc_chars:
                continue
            for c in pieces[src]:                                  # in reading order
                if c.id in have or (groups is not None and c.group not in groups) or added + len(c.text) > max_added:
                    continue
                have.add(c.id)
                out.append(c)
                added += len(c.text)
        return out

    # ----- code context
    def _lookups(self):
        if self._maps is None:
            by_source: dict[str, list[Chunk]] = {}
            overview: dict[str, Chunk] = {}
            cards: dict[str, Chunk] = {}
            datafiles: dict[str, Chunk] = {}
            for c in self.chunks:
                if c.kind != "code":
                    continue
                if c.section == "project map":
                    overview[c.group] = c
                elif c.section == "file card":
                    cards[c.source] = c
                elif c.section == "data files":
                    datafiles[c.group] = c
                else:
                    by_source.setdefault(c.source, []).append(c)
            for lst in by_source.values():
                lst.sort(key=lambda c: c.line_start or 0)
            self._maps = (by_source, overview, cards, datafiles)
        return self._maps

    def chunk_at(self, source: str, line: int) -> Chunk | None:
        for c in self._lookups()[0].get(source, []):
            if c.line_start is not None and c.line_start <= line <= (c.line_end or c.line_start):
                return c
        return None

    def _named_symbols(self, query: str, groups: set[str] | None) -> list[Chunk]:
        """Definitions of function or class names written in the question (snake_case, CamelCase or long names)."""
        out: list[Chunk] = []
        for w in dict.fromkeys(_IDENT.findall(query)):
            if w not in self.codemap.symbols or not ("_" in w or any(ch.isupper() for ch in w[1:]) or len(w) >= 8):
                continue
            for src, line in self.codemap.defined(w)[:2]:
                c = self.chunk_at(src, line)
                if c and (groups is None or c.group in groups) and c not in out:
                    out.append(c)
            if len(out) >= 4:
                break
        return out[:4]

    def expand_code(self, hits: list[Chunk], groups: set[str] | None, max_chars: int = 24000, max_extra: int = 10) -> list[Chunk]:
        """Add context around retrieved code: the project overview, each file's card, the definitions the hit
        functions call, and the next piece of the file. Capped by size, and only from the selected groups."""
        by_source, overview, cards, datafiles = self._lookups()
        code_hits = [c for c in hits if c.kind == "code" and c.section not in MAP_SECTIONS + ("data files",)]
        if not code_hits:
            return hits
        have = {c.id for c in hits}
        used = sum(len(c.text) for c in hits)
        extra: list[Chunk] = []

        def add(c: Chunk | None):
            nonlocal used
            if (c is None or c.id in have or len(extra) >= max_extra or used + len(c.text) > max_chars
                    or (groups is not None and c.group not in groups)):
                return
            have.add(c.id)
            extra.append(c)
            used += len(c.text)

        for g in dict.fromkeys(c.group for c in code_hits):
            add(overview.get(g))
        for c in code_hits:
            add(cards.get(c.source))
        for c in code_hits:
            for src, line in self.codemap.callees(c.source, c.line_start, c.line_end)[:2]:
                add(self.chunk_at(src, line))
        for c in code_hits:
            nxt = [x for x in by_source.get(c.source, []) if (x.line_start or 0) > (c.line_end or 0)]
            add(nxt[0] if nxt else None)
        return hits + extra

    def whole_documents(self, groups: set[str] | None, char_cap: int) -> tuple[list[Chunk], bool]:
        """All local-document chunks in reading order, up to a character cap. Returns (chunks, truncated)."""
        out, total = [], 0
        for c in self.chunks:
            if c.kind != "local" or (groups is not None and c.group not in groups):
                continue
            if total + len(c.text) > char_cap:
                return out, True
            out.append(c); total += len(c.text)
        return out, False

    def groups(self) -> dict[str, dict]:
        """group -> {kind, source, n}"""
        g: dict[str, dict] = {}
        for c in self.chunks:
            e = g.setdefault(c.group, {"kind": c.kind, "n": 0})
            e["n"] += 1
        return g
