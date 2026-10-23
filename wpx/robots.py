"""robots.txt handling (RFC 9309 semantics, cached per host).

WordPress sites are ordinary websites, so robots.txt is enforced on every host (there is no "vendor API" exception
like in the ATS Actor). "off" exists only for unit tests.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

log = logging.getLogger("wordpress-content-export.robots")

FetchText = Callable[[str], Awaitable[tuple[int, str]]]


class RobotsPolicy:
    """Resolves whether a URL may be fetched. `fetch_text` is injected so the polite HTTP client is reused."""

    def __init__(self, mode: str = "strict", user_agent: str = "*") -> None:
        if mode not in ("strict", "off"):
            raise ValueError(f"Unknown robots policy: {mode}")
        self.mode = mode
        self.user_agent = user_agent
        self._cache: dict[str, RobotFileParser | None] = {}  # None => robots unavailable (allow all)
        self._unreachable: set[str] = set()  # 5xx / network failure => treat as disallow (conservative)
        self.sitemaps: dict[str, list[str]] = {}  # host -> Sitemap: URLs declared in robots.txt

    def needs_check(self, url: str) -> bool:
        return self.mode != "off"

    async def can_fetch(self, url: str, fetch_text: FetchText) -> bool:
        if not self.needs_check(url):
            return True
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        origin = f"{parts.scheme}://{parts.netloc}"
        if host in self._unreachable:
            return False
        if host not in self._cache:
            await self._load(origin, host, fetch_text)
            if host in self._unreachable:
                return False
        parser = self._cache.get(host)
        if parser is None:
            return True
        return parser.can_fetch(self.user_agent, url)

    async def sitemap_urls(self, origin: str, fetch_text: FetchText) -> list[str]:
        """Sitemap: lines from robots.txt (loads robots.txt if needed)."""
        host = (urlsplit(origin).hostname or "").lower()
        if host not in self._cache:
            await self._load(origin, host, fetch_text)
        return list(self.sitemaps.get(host, []))

    async def _load(self, origin: str, host: str, fetch_text: FetchText) -> None:
        try:
            status, text = await fetch_text(f"{origin}/robots.txt")
        except Exception as exc:  # noqa: BLE001 - any transport failure
            log.warning("robots.txt unreachable for %s (%s); treating host as disallowed for safety", host, exc)
            self._unreachable.add(host)
            self._cache[host] = None
            return
        if status >= 500:
            log.warning("robots.txt returned %s for %s; treating host as disallowed for now", status, host)
            self._unreachable.add(host)
            self._cache[host] = None
            return
        if status >= 400 or not text.strip():
            self._cache[host] = None  # RFC 9309: 4xx => unrestricted
            return
        parser = RobotFileParser()
        parser.parse(text.splitlines())
        self._cache[host] = parser
        self.sitemaps[host] = [
            line.split(":", 1)[1].strip()
            for line in text.splitlines()
            if line.strip().lower().startswith("sitemap:") and ":" in line
        ]
