"""Fetching a web page the user pasted, without letting it reach this computer or the local network."""
from __future__ import annotations

import ipaddress
import socket
import time
from urllib.parse import urljoin, urlparse

import requests

MAX_BYTES = 5_000_000
OK_TYPES = ("text/html", "application/xhtml+xml", "text/plain")


def check_public(url: str) -> None:
    """Raise ValueError unless the link is http(s) on port 80 or 443 and every address behind its host is a public one."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("only http and https links can be added")
    port = u.port or (443 if u.scheme == "https" else 80)
    if port not in (80, 443):
        raise ValueError("only the standard web ports are allowed")
    try:
        infos = socket.getaddrinfo(u.hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise ValueError(f"could not find that address: {e}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError("that address is on this computer or a private network, not a public website")


def fetch(url: str, max_bytes: int = MAX_BYTES, timeout: float = 20.0, max_redirects: int = 3) -> tuple[bytes, str]:
    """The first max_bytes of a public page, and its final address. Each redirect is checked again, and the whole
    request has one time limit, so a server that sends data slowly cannot hold the app."""
    deadline = time.monotonic() + timeout
    for _ in range(max_redirects + 1):
        check_public(url)
        r = requests.get(url, timeout=(5, 10), headers={"User-Agent": "interview-prep-tool"}, stream=True, allow_redirects=False)
        try:
            if r.is_redirect or r.status_code in (301, 302, 303, 307, 308):
                nxt = r.headers.get("Location")
                if not nxt:
                    raise ValueError("the page redirects nowhere")
                url = urljoin(url, nxt)
                continue
            r.raise_for_status()
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype and ctype not in OK_TYPES:
                raise ValueError(f"this link is {ctype}, not a web page")
            body = bytearray()
            for part in r.iter_content(65536):
                body += part
                if len(body) >= max_bytes:
                    break
                if time.monotonic() > deadline:
                    raise ValueError("the page took too long to load")
            return bytes(body[:max_bytes]), url
        finally:
            r.close()
    raise ValueError("too many redirects")
