"""Polite async HTTP client: global + per-host rate limit, exponential backoff, Retry-After, optional bearer token."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("bluesky-public-data.net")

MAX_RPS = 8.0  # hard cap - politeness rule for this Actor
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class FetchError(Exception):
    """Base class for request failures that modes should treat as 'stop this query, log a warning'."""


class RateLimited(FetchError):
    """429 persisted after retries, or Retry-After was unreasonably long."""


class Blocked(FetchError):
    """401/403 - never retried."""

    def __init__(self, message: str, status: int = 403) -> None:
        super().__init__(message)
        self.status = status


class PoliteClient:
    """Thin wrapper around httpx.AsyncClient that enforces politeness.

    - at most `concurrency_per_host` in-flight requests per host
    - at most `requests_per_second` requests overall (capped at 8), spaced evenly
    - 429 / 5xx / transport errors retried with exponential backoff (honouring Retry-After), `max_retries` times
    - 401 / 403 raise `Blocked` immediately
    - an optional `auth_header` callable adds `Authorization: Bearer ...` to requests on hosts listed in `auth_hosts`
    """

    def __init__(
        self,
        *,
        user_agent: str,
        concurrency_per_host: int = 2,
        requests_per_second: float = 3.0,
        max_retries: int = 3,
        timeout_seconds: float = 30.0,
        max_retry_after_seconds: float = 60.0,
    ) -> None:
        self.user_agent = user_agent
        self.concurrency_per_host = max(1, int(concurrency_per_host))
        self.min_interval = 1.0 / max(0.1, min(float(requests_per_second), MAX_RPS))
        self.max_retries = max(0, int(max_retries))
        self.max_retry_after = max_retry_after_seconds
        self.auth_header: Callable[[], str | None] | None = None
        self.auth_hosts: set[str] = set()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            follow_redirects=True,
        )
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._next_slot = 0.0
        self._slot_lock = asyncio.Lock()
        self.stats: dict[str, int] = {"requests": 0, "retries": 0, "rate_limited": 0, "blocked": 0}

    async def __aenter__(self) -> PoliteClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ----- politeness primitives -------------------------------------------------------------------------------

    @staticmethod
    def _host(url: str) -> str:
        return (urlsplit(url).hostname or "unknown").lower()

    async def _wait_for_slot(self) -> None:
        async with self._slot_lock:
            now = time.monotonic()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self.min_interval
        delay = slot - now
        if delay > 0:
            await asyncio.sleep(delay)

    # ----- public API -------------------------------------------------------------------------------------------

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        host = self._host(url)
        sem = self._sems.setdefault(host, asyncio.Semaphore(self.concurrency_per_host))
        hdrs = dict(headers or {})
        if self.auth_header is not None and host in self.auth_hosts and "Authorization" not in hdrs:
            token = self.auth_header()
            if token:
                hdrs["Authorization"] = f"Bearer {token}"
        attempt = 0
        while True:
            async with sem:
                await self._wait_for_slot()
                self.stats["requests"] += 1
                try:
                    resp = await self._client.request(method, url, params=params, json=json, headers=hdrs)
                except httpx.TransportError as exc:
                    if attempt >= self.max_retries:
                        raise FetchError(f"{method} {url} failed after {attempt + 1} attempts: {exc}") from exc
                    delay = self._backoff(attempt, None)
                    log.warning("Transport error on %s (%s); retry %d/%d in %.1fs", _redact(url), exc, attempt + 1, self.max_retries, delay)
                    attempt += 1
                    self.stats["retries"] += 1
                    await asyncio.sleep(delay)
                    continue

            if resp.status_code in (401, 403):
                self.stats["blocked"] += 1
                raise Blocked(f"HTTP {resp.status_code} for {_redact(url)}", status=resp.status_code)
            if resp.status_code not in RETRYABLE_STATUS:
                return resp

            retry_after = self._parse_retry_after(resp.headers.get("Retry-After"))
            if retry_after is None and resp.status_code == 429:
                retry_after = self._parse_ratelimit_reset(resp.headers.get("RateLimit-Reset"))
            if resp.status_code == 429:
                self.stats["rate_limited"] += 1
            if attempt >= self.max_retries or (retry_after is not None and retry_after > self.max_retry_after):
                if resp.status_code == 429:
                    raise RateLimited(f"429 from {host} persisted after {attempt + 1} attempts (Retry-After={retry_after})")
                return resp  # let the caller decide what a lingering 5xx means
            delay = self._backoff(attempt, retry_after)
            log.warning("HTTP %s from %s; retry %d/%d in %.1fs", resp.status_code, host, attempt + 1, self.max_retries, delay)
            attempt += 1
            self.stats["retries"] += 1
            await asyncio.sleep(delay)

    async def get_json(self, url: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> tuple[int, Any]:
        """GET and parse JSON. Returns (status, parsed-or-None). Non-JSON bodies yield None, never raise."""
        resp = await self.request("GET", url, params=params, headers=headers)
        return resp.status_code, _safe_json(resp)

    async def post_json(self, url: str, *, json: Any, headers: dict[str, str] | None = None) -> tuple[int, Any]:
        hdrs = {"Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        resp = await self.request("POST", url, json=json, headers=hdrs)
        return resp.status_code, _safe_json(resp)

    # ----- helpers ----------------------------------------------------------------------------------------------

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

    @staticmethod
    def _parse_ratelimit_reset(value: str | None) -> float | None:
        """Bluesky PDS hosts send draft `RateLimit-Reset` (unix seconds) on 429."""
        if not value:
            return None
        try:
            reset = float(value)
        except ValueError:
            return None
        if reset > 1_000_000_000:  # absolute unix timestamp
            return max(0.0, reset - time.time())
        return max(0.0, reset)


def _redact(url: str) -> str:
    """Never let credentials or tokens reach the logs (we only ever GET with params, but be defensive)."""
    return url.split("?", 1)[0]


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None
