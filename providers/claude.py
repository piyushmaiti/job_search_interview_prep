"""Claude via the Anthropic SDK, with native web search. The API key is read from ANTHROPIC_API_KEY."""
from __future__ import annotations

import os

from .base import Provider, ProviderError, ProviderResult, WebCite, system_prompt, user_prompt

CLAUDE_MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"]
# Models that accept the newer web search tool (dynamic filtering). Others get the basic version.
DYNAMIC_WEB_PREFIXES = ("claude-opus-5", "claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8",
                        "claude-sonnet-5", "claude-sonnet-4-6")
# Models that support Anthropic's server-side refusal fallback.
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}


def web_tool(model: str) -> dict:
    new = model.startswith(DYNAMIC_WEB_PREFIXES)
    return {"type": "web_search_20260209" if new else "web_search_20250305", "name": "web_search", "max_uses": 5}


class ClaudeProvider(Provider):
    name = "claude"
    supports_web = True

    def available(self):
        if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            return True, ""
        return False, "Set ANTHROPIC_API_KEY (or run `ant auth login`) to use Claude."

    def models(self):
        return CLAUDE_MODELS

    def generate(self, question, chunks, history, web):
        import anthropic

        client = anthropic.Anthropic()
        messages = []
        for q, a in history:
            messages += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
        messages.append({"role": "user", "content": user_prompt(question, chunks)})

        kwargs = dict(
            model=self.model,
            max_tokens=16000,
            system=system_prompt(self.opts.get("role", "")),
            messages=messages,
            thinking={"type": "adaptive"},
            output_config={"effort": self.opts.get("effort", "medium")},
        )
        if web:
            kwargs["tools"] = [web_tool(self.model)]

        # If a safety classifier declines, Anthropic can retry on another Claude model on its side. Request and
        # data stay with Anthropic. Turn off with `claude_fallbacks: false` in config.yaml.
        use_fallback = bool(self.opts.get("fallbacks", True)) and self.model in FALLBACK_MODELS
        extra = dict(betas=["server-side-fallback-2026-07-01"], fallbacks="default") if use_fallback else {}

        def call(kw):
            nonlocal use_fallback
            streamer = client.beta.messages.stream if use_fallback else client.messages.stream
            try:
                with streamer(**kw, **(extra if use_fallback else {})) as stream:
                    return stream.get_final_message()
            except anthropic.BadRequestError as e:
                if use_fallback and "fallback" in str(e).lower():   # the option was rejected; retry without it
                    use_fallback = False
                    with client.messages.stream(**kw) as stream:
                        return stream.get_final_message()
                raise

        text, cites = "", []
        try:
            for _ in range(4):  # server-side web search can pause the turn; continue it
                resp = call(kwargs)
                if resp.stop_reason == "refusal":
                    cat = getattr(getattr(resp, "stop_details", None), "category", None)
                    raise ProviderError(f"{self.model} declined this request (category: {cat}).")
                for block in resp.content:
                    if block.type != "text":
                        continue
                    text += block.text
                    for c in getattr(block, "citations", None) or []:
                        url = getattr(c, "url", None)
                        if url:
                            cites.append(WebCite(end=len(text), url=url, title=getattr(c, "title", "") or ""))
                if resp.stop_reason != "pause_turn":
                    break
                kwargs["messages"] = messages + [{"role": "assistant", "content": resp.content}]
        except anthropic.APIError as e:
            raise ProviderError(f"Claude API error: {e}") from e

        usage = {"input": resp.usage.input_tokens, "output": resp.usage.output_tokens}
        return ProviderResult(text=text, web=cites, model=getattr(resp, "model", None) or self.model, usage=usage)
