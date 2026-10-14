"""Keeps text that a model wrote (or a document supplied) from making the browser fetch anything by itself."""
from __future__ import annotations

import re

_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)|!\[([^\]]*)\]\[[^\]]*\]|<img\b[^>]*>", re.I)


def strip_images(text: str) -> str:
    """Replace markdown and HTML images with their alt text. An image URL is fetched by the browser without a click,
    so injected text could use one to carry your data to another site."""
    return _IMAGE.sub(lambda m: f"[image removed: {(m.group(1) or m.group(2) or '').strip()}]" if (m.group(1) or m.group(2)) else "[image removed]", text)
