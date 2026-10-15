"""OpenAI through the official `openai` package, using the Responses API (web search is a tool of that API).

The API key is read from OPENAI_API_KEY and billed per use by OpenAI. Requests are sent with store=False, so OpenAI does
not keep the response as application state. OpenAI may still keep logs under its own policy; see its data usage page.
"""
from __future__ import annotations

import os
import re

from .base import Provider, ProviderError, ProviderResult, WebCite, system_prompt, user_prompt

# Confirm against OpenAI's model page: names change often. The first is the default for new installs (lower cost than the flagship).
OPENAI_MODELS = ["gpt-6.1-sol", "gpt-6-astra", "gpt-6-luna"]
DEFAULT_TIMEOUT = 300


def _key() -> str | None:
    return os.environ.get("OPENAI_API_KEY")


def _safe(msg: str) -> str:
    """An error message with anything that looks like an API key hidden (OpenAI echoes part of a wrong key)."""
    k = _key()
    if k:
        msg = msg.replace(k, "[key]")
    return re.sub(r"sk-[A-Za-z0-9_\-*.]{6,}", "[key]", msg)


class OpenAIProvider(Provider):
    name = "openai"
    supports_web = True

    def available(self):
        if _key():
            return True, ""
        return False, "Set OPENAI_API_KEY in .env to use OpenAI."

    def models(self):
        return OPENAI_MODELS

    def _client(self, timeout: float | None = None):
        from openai import OpenAI

        return OpenAI(api_key=_key(), timeout=float(timeout or DEFAULT_TIMEOUT), max_retries=2)

    def _create(self, timeout, **kw):
        try:
            resp = self._client(timeout).responses.create(model=self.model, store=False, **kw)
        except Exception as e:
            raise ProviderError(f"OpenAI API error: {_safe(str(e))}") from e
        if getattr(resp, "status", "completed") == "failed" or getattr(resp, "error", None):
            raise ProviderError(f"OpenAI API error: {_safe(str(getattr(resp, 'error', None) or 'the request failed'))}")
        return resp

    def generate(self, question, chunks, history, web):
        msgs = []
        for q, a in history:
            msgs += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
        msgs.append({"role": "user", "content": user_prompt(question, chunks)})
        kw = {"instructions": system_prompt(self.opts.get("role", "")), "input": msgs}
        if web:
            kw["tools"] = [{"type": "web_search"}]
        resp = self._create(self.opts.get("timeout"), **kw)
        text, cites = text_and_cites(resp)
        return ProviderResult(text=text, web=cites, model=self.model, usage=_usage(resp))

    def complete(self, system: str, prompt: str, timeout: int | None = None) -> ProviderResult:
        """One request with its own instructions and no tools (used by the prep tools, the mock interview and project knowledge)."""
        resp = self._create(timeout or 600, instructions=system, input=prompt)
        text, _cites = text_and_cites(resp)
        return ProviderResult(text=text, model=self.model, usage=_usage(resp))


def _usage(resp) -> dict:
    u = getattr(resp, "usage", None)
    return {"input": getattr(u, "input_tokens", 0) or 0, "output": getattr(u, "output_tokens", 0) or 0} if u else {}


def text_and_cites(resp) -> tuple[str, list[WebCite]]:
    """The answer text and its web citations. OpenAI marks each citation with a character range of the text. When that range is
    link markup, such as ([title](url)), the markup is removed and the citation is attached where it stood, so the app shows its own
    clickable mark and no unchecked raw link."""
    out, cites = "", []
    for item in getattr(resp, "output", None) or []:
        if getattr(item, "type", "") != "message":
            continue
        for part in getattr(item, "content", None) or []:
            if getattr(part, "type", "") != "output_text":
                continue
            text = getattr(part, "text", "") or ""
            anns = sorted((a for a in (getattr(part, "annotations", None) or []) if getattr(a, "type", "") == "url_citation"),
                          key=lambda a: a.start_index)
            pos = 0
            for a in anns:
                s, e = int(a.start_index), int(a.end_index)
                if s < pos or e > len(text) or e < s:
                    continue                                   # overlapping or outside the text: leave it alone
                out += text[pos:s]
                span = text[s:e]
                if not ("](" in span or span.lstrip().startswith("([")):
                    out += span                                # the range is real text that the citation supports
                cites.append(WebCite(end=len(out), url=a.url, title=getattr(a, "title", "") or ""))
                pos = e
            out += text[pos:]
    return out, cites
