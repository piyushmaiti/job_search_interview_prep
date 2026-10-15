"""Pick the model for a query. A manual choice always wins over the router."""
from __future__ import annotations

import re
from dataclasses import dataclass

IMAGE_RE = re.compile(
    r"\b(infographic|poster|illustration|make (me )?an? (image|picture|graphic|diagram)|"
    r"generate (an? )?(image|picture|graphic|diagram)|draw (me )?an? (picture|diagram|infographic)|diagram of|visual summary)\b", re.I)
WHOLE_RE = re.compile(
    r"\b(entire|whole|full)\s+(paper|document|pdf|text|study)s?\b|"
    r"\b(summari[sz]e|compare|summary of|overview of)\b.*\b(papers|both|documents|pdfs|each paper)\b", re.I)



@dataclass
class Route:
    providers: list[str]
    reason: str
    image: bool = False
    whole_docs: bool = False


def is_image_request(q: str) -> bool:
    return bool(IMAGE_RE.search(q))


def is_whole_doc_request(q: str) -> bool:
    return bool(WHOLE_RE.search(q))


COMPARE_ORDER = ("claude", "gemini", "openai")


def compare_pair(available: set[str]) -> list[str]:
    """The two models Compare runs: Claude and Gemini when both are ready, otherwise the first two ready ones (OpenAI can stand in)."""
    ps = [p for p in COMPARE_ORDER if p in available][:2]
    if len(ps) < 2:
        raise ValueError("Compare needs two cloud models (Claude, Gemini, OpenAI); only "
                         + (", ".join(p for p in COMPARE_ORDER if p in available) or "none") + " is ready")
    return ps


def decide_whole(mode: str, available: set[str], tokens: int, caps: dict[str, int]) -> Route:
    """Route a whole-project question. Auto prefers Claude (your login, no API bill) while the project fits its limit,
    and otherwise Gemini's larger context. Auto never picks OpenAI: choose it by name. A manual choice always wins.
    Ollama's context is too small."""
    cloud = available - {"ollama"}
    if mode == "ollama":
        raise ValueError("Ollama's context is too small to hold a whole project. Choose Claude, Gemini or OpenAI.")
    if mode in ("claude", "gemini", "openai"):
        if mode not in available:
            raise ValueError(f"{mode} is not available")
        return Route([mode], f"you chose {mode}: whole project (about {tokens:,} tokens)")
    if mode == "compare":
        ps = compare_pair(available)
        return Route(ps, f"compare mode: both models read the whole project (about {tokens:,} tokens)")
    if "claude" in cloud and tokens <= caps.get("claude", 0):
        return Route(["claude"], f"whole project (about {tokens:,} tokens) fits Claude's limit")
    if "gemini" in cloud:
        why = "is over Claude's limit" if "claude" in cloud else "and Claude is not available"
        return Route(["gemini"], f"whole project (about {tokens:,} tokens) {why}, so Gemini's larger context")
    if "claude" in cloud:
        return Route(["claude"], "only Claude is available, so the project is trimmed to fit")
    raise ValueError("No cloud model is ready for a whole project. Set up Claude or Gemini.")


def decide(question: str, mode: str, available: set[str], locality: dict[str, str] | None = None) -> Route:
    loc = locality or {}
    image = is_image_request(question)
    whole = is_whole_doc_request(question) and not image

    if mode == "compare":
        return Route(compare_pair(available), "compare mode: both models answer the same context", image=False, whole_docs=whole)

    if mode in ("claude", "gemini", "openai", "ollama"):
        if mode not in available:
            raise ValueError(f"{mode} is not available")
        note = ""
        if image and mode != "gemini":
            note = " (images need Gemini, so the visual step will be skipped)"
        where = " (cloud-hosted)" if mode == "ollama" and loc.get("ollama") == "cloud" else ""
        return Route([mode], f"you chose {mode}{where}: router skipped{note}", image=image and mode == "gemini", whole_docs=whole)

    # auto
    if image:
        if "gemini" in available:
            return Route(["gemini"], "the request asks for a visual, and only Gemini generates images", image=True)
        raise ValueError("A visual was requested but Gemini is not available (set GEMINI_API_KEY).")
    if whole and "gemini" in available:
        return Route(["gemini"], "whole-document task: Gemini's large context", whole_docs=True)
    for p, why in (("claude", "chunked Q&A across sources and the web"), ("gemini", "Claude is not available")):
        if p in available:
            return Route([p], why, whole_docs=whole)
    if "ollama" in available and loc.get("ollama", "local") == "local":
        return Route(["ollama"], "only a local model is available")
    hint = " Ollama's installed model runs in the cloud, so it is not used automatically; choose it by name, or `ollama pull` a local one." if "ollama" in available else ""
    if "openai" in available:
        hint += " OpenAI is ready, but Auto never picks it; choose OpenAI by name."
    raise ValueError("No model is ready. Set ANTHROPIC_API_KEY or GEMINI_API_KEY." + hint)
