"""Offline stand-in used by tests and by the demo mode. Makes no network calls."""
from __future__ import annotations

from .base import Provider, ProviderResult, WebCite


class FakeProvider(Provider):
    name = "demo"
    supports_web = True

    def __init__(self, model: str = "demo-model", label: str = "demo", locality: str = "local", **opts):
        super().__init__(model, **opts)
        self.name = label
        self.locality_value = locality
        self.calls: list[dict] = []

    @property
    def locality(self):  # type: ignore[override]
        return self.locality_value

    def generate(self, question, chunks, history, web):
        self.calls.append({"question": question, "ids": [i for i, _ in chunks], "history": len(history), "web": web,
                           "text": "\n".join(c.text for _, c in chunks), "refs": [c.label() for _, c in chunks]})
        if chunks:
            sid = chunks[0][0]
            text = f"The top source says something relevant [{sid}]. "
        else:
            text = "No source covers this. "
        text += "Generally this is common practice [M]. A web page agrees."
        cites = [WebCite(end=len(text), url="https://example.org/demo", title="Example page")] if web else []
        return ProviderResult(text=text, web=cites, model=self.model)

    def explore(self, question, chunks, history, root, timeout=None):
        """Records what was inside the explored copy while the call ran, and cites its first real file."""
        from pathlib import Path
        files = sorted(p.relative_to(root).as_posix() for p in Path(root).rglob("*") if p.is_file())
        texts = "\n".join(Path(root, f).read_text(errors="replace") for f in files)
        self.calls.append({"explore": True, "root": str(root), "files": files, "text": texts, "question": question,
                           "ids": [i for i, _ in chunks], "history": len(history)})
        first = next((f for f in files if not f.endswith(("PROJECT_MAP.md", "DATA_FILES.txt"))), None)
        cite = f" [{first}:1-1]" if first else ""
        return ProviderResult(text=f"I explored the project.{cite} Generally this is common practice [M]. A third point [{files[0]}:9999].",
                              model=f"{self.model} · explore")

    def complete(self, system, prompt, timeout=None):
        """Offline stand-in for writing the project knowledge document from the files in the prompt."""
        import re
        self.calls.append({"complete": True, "system": system, "prompt": prompt})
        if system.startswith("You build a candidate profile"):
            return ProviderResult(text="# Profile: the candidate\n\n## Summary\nA demo profile built offline from the uploaded documents. " + "x" * 200 +
                                  "\n\n## Experience\n### Demo role, Demo organisation (2020 to 2024)\n- Did demo work.\n\n## Education\nNot stated.\n",
                                  model=f"{self.model} · profile")
        if system.startswith("You are an interview coach"):
            if "MOCK QUESTION" in prompt:
                return ProviderResult(text="Tell me about a time you had to learn something quickly for a project.", model=f"{self.model} · mock")
            if "FEEDBACK on" in prompt:
                return ProviderResult(text="## What worked\nClear.\n## What to improve\nAdd the result.\n## What your documents could add\nGAP: a result.\n"
                                      "## A stronger version\nI learned it fast [add: the result].", model=f"{self.model} · mock")
            return ProviderResult(text="## Strong matches\nDemo work [Profile].\n## Gaps\nGAP: nothing about leadership. Reached 99% accuracy.", model=f"{self.model} · prep")
        paths = re.findall(r'<file path="([^"]+)">', prompt)
        name = (re.search(r"PROJECT NAME: (\S+)", prompt) or [None, "project"])[1]
        first = paths[0] if paths else "x.py"
        doc = (f"# Project knowledge: {name}\n\n## Overview\nA demo project with {len(paths)} files [{first}:1-1].\n\n"
               f"## Architecture and data flow\nData flows through the files.\n\n## Files\n"
               + "".join(f"### {p}\nPurpose of {p} [{p}:1-1].\n\n" for p in paths)
               + "## Experiments, results and numbers\nThe wide model scored 0.0108 [" + first + ":9999-9999].\n\n"
               "## Design decisions and rationale\nNot stated.\n\n## Caveats, limitations and open questions\nNone.\n\n## Glossary\nNone.\n")
        return ProviderResult(text=doc, model=f"{self.model} · knowledge")

    def generate_image(self, prompt: str, image_model: str) -> bytes:
        self.calls.append({"image": prompt})
        # 1x1 transparent PNG
        import base64
        return base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
        )
