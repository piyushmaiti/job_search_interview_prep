"""Gemini via google-genai, with Google Search grounding and image generation.

The API key is read from GEMINI_API_KEY or GOOGLE_API_KEY.
"""
from __future__ import annotations

import base64
import os

from .base import Provider, ProviderError, ProviderResult, WebCite, system_prompt, user_prompt

GEMINI_MODELS = ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.1-pro-preview", "gemini-2.5-pro", "gemini-2.5-flash"]


def _key() -> str | None:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


class GeminiProvider(Provider):
    name = "gemini"
    supports_web = True

    def available(self):
        if _key():
            return True, ""
        return False, "Set GEMINI_API_KEY (or GOOGLE_API_KEY) to use Gemini."

    def models(self):
        return GEMINI_MODELS

    def _client(self):
        from google import genai

        return genai.Client(api_key=_key())

    def generate(self, question, chunks, history, web):
        from google.genai import types

        client = self._client()
        contents = []
        for q, a in history:
            contents.append(types.Content(role="user", parts=[types.Part(text=q)]))
            contents.append(types.Content(role="model", parts=[types.Part(text=a)]))
        contents.append(types.Content(role="user", parts=[types.Part(text=user_prompt(question, chunks))]))
        cfg = types.GenerateContentConfig(
            system_instruction=system_prompt(self.opts.get("role", "")),
            tools=[types.Tool(google_search=types.GoogleSearch())] if web else None,
        )
        try:
            resp = client.models.generate_content(model=self.model, contents=contents, config=cfg)
        except Exception as e:
            raise ProviderError(f"Gemini API error: {e}") from e

        text = resp.text or ""
        cites: list[WebCite] = []
        cand = resp.candidates[0] if resp.candidates else None
        gm = getattr(cand, "grounding_metadata", None)
        if gm and gm.grounding_supports and gm.grounding_chunks:
            raw = text.encode("utf-8")
            for sup in gm.grounding_supports:
                if not sup.segment or sup.segment.end_index is None:
                    continue
                end = len(raw[: sup.segment.end_index].decode("utf-8", errors="ignore"))  # byte -> char offset
                for i in sup.grounding_chunk_indices or []:
                    web_chunk = gm.grounding_chunks[i].web
                    if web_chunk and web_chunk.uri:
                        cites.append(WebCite(end=end, url=web_chunk.uri, title=web_chunk.title or ""))
        usage = {}
        um = getattr(resp, "usage_metadata", None)
        if um:
            usage = {"input": um.prompt_token_count, "output": um.candidates_token_count}
        return ProviderResult(text=text, web=cites, model=self.model, usage=usage)

    def generate_image(self, prompt: str, image_model: str) -> bytes:
        """Return PNG bytes from Gemini's image model ("Nano Banana")."""
        client = self._client()
        errors = []
        for model in [image_model]:
            # 1) Interactions API, as documented for the current image models
            try:
                inter = client.interactions.create(model=model, input=prompt)
                return base64.b64decode(inter.output_image.data)
            except Exception as e:
                errors.append(f"interactions: {type(e).__name__}: {e}")
            # 2) classic generate_content with image output
            try:
                from google.genai import types

                resp = client.models.generate_content(
                    model=model, contents=prompt,
                    config=types.GenerateContentConfig(response_modalities=["IMAGE", "TEXT"]),
                )
                for part in resp.candidates[0].content.parts:
                    if getattr(part, "inline_data", None) and part.inline_data.data:
                        return part.inline_data.data
                errors.append("generate_content: no image in response")
            except Exception as e:
                errors.append(f"generate_content: {type(e).__name__}: {e}")
        raise ProviderError("Image generation failed. " + " | ".join(errors))
