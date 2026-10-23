"""Polite async HTTP client: per-host concurrency + rate limit, exponential backoff, robots.txt gate, optional proxy."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from .robots import RobotsPolicy

log = logging.getLogger("wordpress-content-export.net")

MAX_RPS_PER_HOST = 2.0  # hard cap - politeness rule for this Actor
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_HOST_INTERVAL = 10.0  # seconds between requests after repeated 429s (adaptive slowdown ceiling)


class FetchError(Exception):
    """Base class for request failures that callers should treat as 'stop this site, log a warning'."""


class RateLimited(FetchError):
    """429 persisted after retries, or Retry-After was unreasonably long."""


class Blocked(FetchError):
    """401/403 or robots.txt disallow - never retried."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class PoliteClient:
    """Thin wrapper around httpx.AsyncClient that enforces politeness per host.

    - at most `concurrency_per_host` in-flight requests per host
    - at most `requests_per_second_per_host` (capped at 2) requests per host, spaced evenly
    - 429 / 5xx / transport errors retried with exponential backoff (honouring Retry-After), `max_retries` times
    - 401 / 403 raise `Blocked` immediately; robots.txt disallow raises `Blocked` without a request
    """

    def __init__(
        self,
        *,
        user_agent: str,
        concurrency_per_host: int = 2,
        requests_per_second_per_host: float = 2.0,
        max_retries: int = 3,
        timeout_seconds: float = 45.0,
        robots: RobotsPolicy | None = None,
        max_retry_after_seconds: float = 60.0,
        proxy_url: str | None = None,
        max_response_bytes: int = 15 * 1024 * 1024,
    ) -> None:
        self.user_agent = user_agent
        self.concurrency_per_host = max(1, int(concurrency_per_host))
        self.min_interval = 1.0 / max(0.1, min(float(requests_per_second_per_host), MAX_RPS_PER_HOST))
        self.max_retries = max(0, int(max_retries))
        self.max_retry_after = max_retry_after_seconds
        self.max_response_bytes = max_response_bytes
        self.robots = robots or RobotsPolicy("strict")
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            headers={"User-Agent": user_agent, "Accept-Language": "en;q=0.9,*;q=0.5"},
            follow_redirects=True,
            proxy=proxy_url,
        )
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._host_interval: dict[str, float] = {}  # adaptive: doubled after every 429 (up to MAX_HOST_INTERVAL)
        self._next_slot: dict[str, float] = {}
        self._slot_locks: dict[str, asyncio.Lock] = {}
        self.stats: dict[str, int] = {"requests": 0, "retries": 0, "rate_limited": 0, "blocked": 0}

    async def __aenter__(self) -> PoliteClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ----- politeness primitives -------------------------------------------------------------------------------

    def _host(self, url: str) -> str:
        return (urlsplit(url).hostname or "unknown").lower()

    async def _wait_for_slot(self, host: str) -> None:
        lock = self._slot_locks.setdefault(host, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            slot = max(now, self._next_slot.get(host, now))
            self._next_slot[host] = slot + self._host_interval.get(host, self.min_interval)
        delay = slot - now
        if delay > 0:
            await asyncio.sleep(delay)

    async def _fetch_text_unchecked(self, url: str) -> tuple[int, str]:
        """Used for robots.txt itself (no robots check, single attempt, no raise on status)."""
        host = self._host(url)
        sem = self._sems.setdefault(host, asyncio.Semaphore(self.concurrency_per_host))
        async with sem:
            await self._wait_for_slot(host)
            self.stats["requests"] += 1
            resp = await self._client.get(url, headers={"Accept": "text/plain,*/*;q=0.5"})
        return resp.status_code, resp.text if resp.status_code < 400 else ""

    # ----- public API -------------------------------------------------------------------------------------------

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        if not await self.robots.can_fetch(url, self._fetch_text_unchecked):
            self.stats["blocked"] += 1
            raise Blocked(f"robots.txt disallows {url}")

        host = self._host(url)
        sem = self._sems.setdefault(host, asyncio.Semaphore(self.concurrency_per_host))
        attempt = 0
        while True:
            async with sem:
                await self._wait_for_slot(host)
                self.stats["requests"] += 1
                try:
                    resp = await self._client.request(method, url, params=params, headers=headers)
                except httpx.TransportError as exc:
                    if attempt >= self.max_retries:
                        raise FetchError(f"{method} {url} failed after {attempt + 1} attempts: {exc}") from exc
                    delay = self._backoff(attempt, None)
                    log.warning("Transport error on %s (%s); retry %d/%d in %.1fs", url, exc, attempt + 1, self.max_retries, delay)
                    attempt += 1
                    self.stats["retries"] += 1
                    await asyncio.sleep(delay)
                    continue

            if resp.status_code in (401, 403):
                self.stats["blocked"] += 1
                raise Blocked(f"{resp.status_code} for {url}", status=resp.status_code)
            if resp.status_code not in RETRYABLE_STATUS:
                if len(resp.content) > self.max_response_bytes:
                    raise FetchError(f"Response from {url} is larger than {self.max_response_bytes} bytes; skipped")
                return resp

            retry_after = self._parse_retry_after(resp.headers.get("Retry-After"))
            if resp.status_code == 429:
                self.stats["rate_limited"] += 1
                self.slow_down(host, retry_after)
            if attempt >= self.max_retries or (retry_after is not None and retry_after > self.max_retry_after):
                if resp.status_code == 429:
                    raise RateLimited(f"429 from {host} persisted after {attempt + 1} attempts (Retry-After={retry_after})")
                return resp  # let the caller decide what a lingering 5xx means
            delay = self._backoff(attempt, retry_after)
            log.warning("HTTP %s from %s; retry %d/%d in %.1fs", resp.status_code, url, attempt + 1, self.max_retries, delay)
            attempt += 1
            self.stats["retries"] += 1
            await asyncio.sleep(delay)

    async def get_json(
        self, url: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None
    ) -> tuple[int, Any, httpx.Headers]:
        """GET and parse JSON. Returns (status, parsed-or-None, headers). Non-JSON bodies yield None, never raise."""
        hdrs = {"Accept": "application/json"}
        if headers:
            hdrs.update(headers)
        resp = await self.request("GET", url, params=params, headers=hdrs)
        return resp.status_code, _safe_json(resp), resp.headers

    async def get_text(self, url: str, *, headers: dict[str, str] | None = None, accept: str | None = None) -> tuple[int, str, str]:
        """GET an HTML/XML page; returns (status, text, final_url)."""
        hdrs = {"Accept": accept or "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
        if headers:
            hdrs.update(headers)
        resp = await self.request("GET", url, headers=hdrs)
        return resp.status_code, resp.text, str(resp.url)

    # ----- helpers ----------------------------------------------------------------------------------------------

    def slow_down(self, host: str, retry_after: float | None = None) -> None:
        """Halve the request rate for `host` for the rest of the run (the site told us 2 req/s is too much)."""
        current = self._host_interval.get(host, self.min_interval)
        new_interval = min(max(current * 2, retry_after or 0.0, 0.5), MAX_HOST_INTERVAL)  # never faster than 2 req/s after a 429
        if new_interval > current:
            self._host_interval[host] = new_interval
            log.info("Slowing down %s to one request every %.1fs after a 429", host, new_interval)

    def host_interval(self, host: str) -> float:
        return self._host_interval.get(host, self.min_interval)

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        base = retry_after if retry_after is not None else min(2.0**attempt, 30.0)
        return base + random.uniform(0, 0.5)  # noqa: S311 - jitter, not crypto

    @staticmethod
    def _parse_retry_after(value: str | None) -> float | None:
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            return None  # HTTP-date form: fall back to exponential backoff


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None
