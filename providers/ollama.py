"""Ollama over its local HTTP API. Models whose name ends in ':cloud' run on remote servers, not here."""
from __future__ import annotations

import os

import requests

from .base import Provider, ProviderError, ProviderResult, system_prompt, user_prompt

HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
if not HOST.startswith("http"):
    HOST = "http://" + HOST


def is_cloud_model(name: str | None) -> bool:
    n = (name or "").lower()
    return n.endswith(":cloud") or n.endswith("-cloud") or ":cloud" in n


_MODELS_CACHE: dict = {"at": 0.0, "names": []}


def installed_models(max_age: float = 20.0) -> list[str]:
    """Installed Ollama models. The answer is kept for a few seconds, so a busy UI does not probe the server on every click."""
    import time
    now = time.monotonic()
    if now - _MODELS_CACHE["at"] < max_age and _MODELS_CACHE["at"]:
        return list(_MODELS_CACHE["names"])
    try:
        r = requests.get(f"{HOST}/api/tags", timeout=2)
        names = [m["name"] for m in r.json().get("models", [])]
    except Exception:
        names = []
    _MODELS_CACHE.update(at=now, names=names)
    return list(names)


class OllamaProvider(Provider):
    name = "ollama"

    def __init__(self, model: str | None = None, **opts):
        names = installed_models()
        if not model:
            local = [n for n in names if not is_cloud_model(n)]
            model = (local or names or [""])[0]
        super().__init__(model, **opts)

    @property
    def locality(self):  # type: ignore[override]
        return "cloud" if is_cloud_model(self.model) else "local"

    def available(self):
        if not self.model:
            return False, "Ollama is not running or has no models. Run `ollama pull <model>`."
        return True, ""

    def models(self):
        return installed_models() or [self.model]

    def label(self):
        if is_cloud_model(self.model):
            return f"ollama ({self.model}, runs on Ollama's cloud, NOT on this machine)"
        return f"ollama ({self.model}, local, stays on this machine)"

    def complete(self, system: str, prompt: str, timeout: int | None = None, num_ctx: int | None = None) -> ProviderResult:
        """One request with its own system prompt and nothing else (used by the deep read). num_ctx sets how much text the model can hold."""
        body = {"model": self.model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}], "stream": False,
                "options": {"temperature": 0.1, **({"num_ctx": int(num_ctx)} if num_ctx else {})}}
        try:
            r = requests.post(f"{HOST}/api/chat", json=body, timeout=int(timeout or 1200))
            r.raise_for_status()
            return ProviderResult(text=r.json()["message"]["content"], model=self.model)
        except Exception as e:
            raise ProviderError(f"Ollama error: {e}") from e

    def generate(self, question, chunks, history, web):
        msgs = [{"role": "system", "content": system_prompt(self.opts.get("role", ""))}]
        for q, a in history:
            msgs += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
        msgs.append({"role": "user", "content": user_prompt(question, chunks)})
        try:
            r = requests.post(f"{HOST}/api/chat", json={"model": self.model, "messages": msgs, "stream": False}, timeout=600)
            r.raise_for_status()
            return ProviderResult(text=r.json()["message"]["content"], model=self.model)
        except Exception as e:
            raise ProviderError(f"Ollama error: {e}") from e
