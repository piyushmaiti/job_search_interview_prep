"""Claude through the Claude Code CLI (`claude -p`), so questions use your Claude login instead of an API key.

The CLI is started with every tool switched off except web search and fetch (and those only when web is on),
in an empty folder, so it cannot read or change your files. API-key variables are removed from its environment,
otherwise the CLI would bill the API instead of your subscription.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

import settings

from .base import Provider, ProviderError, ProviderResult, SYSTEM_PROMPT, WebCite, system_prompt, user_prompt
from .claude import CLAUDE_MODELS

WEB_LINE = "Claims that come from web search are cited automatically by the system. Do not write URLs for them."
CLI_SYSTEM = SYSTEM_PROMPT.replace(
    WEB_LINE, "For a claim that comes from web search, put the page URL in parentheses right after the claim, "
              "for example (https://example.com/page). Never put a URL after a claim from a provided source.")
assert CLI_SYSTEM != SYSTEM_PROMPT

DROP_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "INTERVIEW_PREP_PASSCODE")
URL_RE = re.compile(r"[ \t]*\((https?://[^\s()]+)\)")


def split_web_cites(text: str) -> tuple[str, list[WebCite]]:
    """Turn '(https://...)' markers into web citations positioned where the marker stood."""
    out, cites, last = [], [], 0
    for m in URL_RE.finditer(text):
        out.append(text[last:m.start()])
        so_far = "".join(out)
        cites.append(WebCite(end=len(so_far), url=m.group(1).rstrip(".,;")))
        last = m.end()
    out.append(text[last:])
    return "".join(out), cites


def history_block(history: list[tuple[str, str]]) -> str:
    if not history:
        return ""
    turns = "\n\n".join(f"USER: {q}\nASSISTANT: {a}" for q, a in history)
    return f"EARLIER CONVERSATION (for context only):\n{turns}\n\n"


class ClaudeCodeProvider(Provider):
    name = "claude"
    supports_web = True

    def label(self):
        return f"claude via Claude Code login ({self.model}, cloud)"

    def available(self):
        if not shutil.which("claude"):
            return False, "The `claude` command was not found on PATH. Install Claude Code, or switch Claude access to the API key."
        return True, ""

    def models(self):
        return CLAUDE_MODELS

    def workdir(self):
        d = settings.assert_writable(settings.CACHE_DIR / "claude_cli_cwd")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def command(self, web: bool, system: str | None = None) -> list[str]:
        cmd = ["claude", "-p", "--model", self.model, "--output-format", "json", "--system-prompt", system or system_prompt(self.opts.get("role", ""), CLI_SYSTEM),
               "--no-session-persistence", "--disable-slash-commands", "--strict-mcp-config",
               "--permission-mode", "dontAsk", "--effort", str(self.opts.get("effort", "medium"))]
        if web:
            cmd += ["--tools", "WebSearch", "--allowedTools", "WebSearch"]
        else:
            cmd += ["--tools", ""]
        return cmd

    def complete(self, system: str, prompt: str, timeout: int | None = None) -> ProviderResult:
        """One request with its own system prompt and every tool off (used to write the project knowledge document)."""
        env = {k: v for k, v in os.environ.items() if k not in DROP_ENV}
        try:
            p = subprocess.run(self.command(False, system), input=prompt, capture_output=True, text=True, env=env,
                               cwd=self.workdir(), timeout=int(timeout or self.opts.get("timeout", 900)))
        except subprocess.TimeoutExpired as e:
            raise ProviderError("Claude Code did not finish within the time limit.") from e
        except OSError as e:
            raise ProviderError(f"Could not run `claude`: {e}") from e
        try:
            data = json.loads(p.stdout)
        except ValueError:
            raise ProviderError(f"Claude Code returned no usable answer: {(p.stderr or p.stdout).strip()[:400]}")
        if p.returncode != 0 or data.get("is_error"):
            raise ProviderError(f"Claude Code error: {str(data.get('result') or p.stderr).strip()[:400]}")
        u = data.get("usage") or {}
        used = next(iter(data.get("modelUsage") or {}), None)
        return ProviderResult(text=data.get("result") or "", model=used or self.model,
                              usage={"input": u.get("input_tokens", 0), "output": u.get("output_tokens", 0)})

    def explore_command(self) -> list[str]:
        """Claude Code locked to reading: restricted mode (file tools confined to the working folder, no commands, no
        web, no user or project settings), and only Read, Grep and Glob."""
        from explore import EXPLORE_SYSTEM
        return ["claude", "-p", "--model", self.model, "--output-format", "json", "--system-prompt", EXPLORE_SYSTEM,
                "--no-session-persistence", "--disable-slash-commands", "--strict-mcp-config", "--restricted",
                "--permission-mode", "dontAsk", "--effort", str(self.opts.get("effort", "medium")),
                "--tools", "Read,Grep,Glob", "--allowedTools", "Read,Grep,Glob"]

    def explore(self, question, chunks, history, root, timeout: int | None = None) -> ProviderResult:
        """Let Claude read the sanitized copy at `root`, then answer. It runs inside that folder and nowhere else."""
        prompt = history_block(history) + user_prompt(question, chunks) + (
            "\n\nThe projects are in this folder. Explore them as needed, then answer the QUESTION.")
        env = {k: v for k, v in os.environ.items() if k not in DROP_ENV}
        try:
            p = subprocess.run(self.explore_command(), input=prompt, capture_output=True, text=True, env=env,
                               cwd=root, timeout=int(timeout or self.opts.get("timeout", 900)))
        except subprocess.TimeoutExpired as e:
            raise ProviderError("Explore ran out of time. Try a narrower question, or raise explore.timeout_seconds.") from e
        except OSError as e:
            raise ProviderError(f"Could not run `claude`: {e}") from e
        try:
            data = json.loads(p.stdout)
        except ValueError:
            raise ProviderError(f"Claude Code returned no usable answer: {(p.stderr or p.stdout).strip()[:400]}")
        if p.returncode != 0 or data.get("is_error"):
            raise ProviderError(f"Claude Code error: {str(data.get('result') or p.stderr).strip()[:400]}")
        u = data.get("usage") or {}
        used = next(iter(data.get("modelUsage") or {}), None)
        return ProviderResult(text=data.get("result") or "", model=f"{used or self.model} · explore",
                              usage={"input": u.get("input_tokens", 0), "output": u.get("output_tokens", 0)})

    def generate(self, question, chunks, history, web):
        prompt = history_block(history) + user_prompt(question, chunks)
        env = {k: v for k, v in os.environ.items() if k not in DROP_ENV}
        try:
            p = subprocess.run(self.command(web), input=prompt, capture_output=True, text=True, env=env,
                               cwd=self.workdir(), timeout=int(self.opts.get("timeout", 300)))
        except subprocess.TimeoutExpired as e:
            raise ProviderError("Claude Code did not answer within the time limit.") from e
        except OSError as e:
            raise ProviderError(f"Could not run `claude`: {e}") from e
        try:
            data = json.loads(p.stdout)
        except ValueError:
            raise ProviderError(f"Claude Code returned no usable answer: {(p.stderr or p.stdout).strip()[:400]}")
        if p.returncode != 0 or data.get("is_error"):
            raise ProviderError(f"Claude Code error: {str(data.get('result') or p.stderr).strip()[:400]}")
        text, cites = split_web_cites(data.get("result") or "")
        u = data.get("usage") or {}
        usage = {"input": u.get("input_tokens", 0), "output": u.get("output_tokens", 0)}
        used = next(iter(data.get("modelUsage") or {}), None)
        return ProviderResult(text=text, web=cites, model=used or self.model, usage=usage)
