"""Claude, using your login for ordinary questions and the API key only when web search is on.

The login route (`claude -p`) has no structured web citations, so a question that searches the web goes through the
API, which does. If no API key is set, web questions fall back to the login route and the answer says so.
"""
from __future__ import annotations

from .base import Provider, ProviderResult
from .claude import CLAUDE_MODELS, ClaudeProvider
from .claude_code import ClaudeCodeProvider


class HybridClaudeProvider(Provider):
    name = "claude"
    supports_web = True

    def __init__(self, model: str, **opts):
        super().__init__(model, **opts)
        self.api = ClaudeProvider(model, **opts)
        self.login = ClaudeCodeProvider(model, **opts)

    def label(self):
        return f"claude, login for questions and API key for web search ({self.model}, cloud)"

    def models(self):
        return CLAUDE_MODELS

    def available(self):
        ok_l, why_l = self.login.available()
        ok_a, _ = self.api.available()
        if ok_l or ok_a:
            return True, ""
        return False, why_l + " Or set ANTHROPIC_API_KEY in .env."

    def route(self, web: bool) -> tuple[Provider, str]:
        ok_a, _ = self.api.available()
        ok_l, _ = self.login.available()
        if web and ok_a:
            return self.api, "API key, for web search citations"
        if ok_l:
            return self.login, "login" + (", web links unchecked: no API key set" if web else "")
        return self.api, "API key, since the claude command is missing"

    def generate(self, question, chunks, history, web) -> ProviderResult:
        prov, why = self.route(web)
        res = prov.generate(question, chunks, history, web)
        res.model = f"{res.model} · {why}"
        return res
