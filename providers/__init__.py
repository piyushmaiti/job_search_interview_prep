from .base import Provider
from .claude import ClaudeProvider
from .claude_code import ClaudeCodeProvider
from .claude_hybrid import HybridClaudeProvider
from .gemini import GeminiProvider
from .ollama import OllamaProvider
from .openai_provider import OpenAIProvider


def build_providers(cfg: dict, overrides: dict | None = None) -> dict[str, Provider]:
    """All providers, whether or not they have credentials. Use .available() to check."""
    m, o = cfg["models"], overrides or {}
    access = o.get("claude_access") or m.get("claude_access", "auto")
    claude_cls = {"api": ClaudeProvider, "login": ClaudeCodeProvider}.get(access, HybridClaudeProvider)
    role = cfg.get("assistant_role", "")
    return {
        "claude": claude_cls(o.get("claude") or m["claude"], effort=m.get("claude_effort", "medium"), fallbacks=m.get("claude_fallbacks", True), role=role),
        "gemini": GeminiProvider(o.get("gemini") or m["gemini"], role=role),
        "ollama": OllamaProvider(o.get("ollama") or m.get("ollama"), role=role),
        "openai": OpenAIProvider(o.get("openai") or m.get("openai", "gpt-6.1-sol"), role=role),
    }
