"""XRPC client for the Bluesky AppView: host routing, optional App-Password session, URI <-> URL helpers.

Hosts (verified 2026-10-07 against the live services and the lexicons in bluesky-social/atproto):

- `https://public.api.bsky.app` - the public, unauthenticated, cached AppView. Serves getAuthorFeed, getPostThread,
  getProfile(s), getFollowers, getFollows, getFeed, getLikes, getRepostedBy, searchActors, resolveHandle ...
  but returns HTTP 403 for `app.bsky.feed.searchPosts` (every request).
- `https://api.bsky.app` - serves `searchPosts` without authentication for the FIRST page only: since mid-2026 any
  request carrying `cursor` is answered with 403 "Request forbidden by administrative rules". Time-window paging
  (`sort=latest` + `until=<oldest createdAt of previous page>`) still works, the boundary post is repeated.
- With an App Password the Actor logs in via `com.atproto.server.createSession` on the user's PDS (default
  `https://bsky.social`) and sends every request there with a bearer token; the PDS proxies app.bsky.* queries to the
  AppView with the user's own rate limits and full cursor pagination.
"""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import unquote, urlsplit

from .net import Blocked, FetchError, PoliteClient

log = logging.getLogger("bluesky-public-data.atproto")

PUBLIC_APPVIEW = "https://public.api.bsky.app"
SEARCH_APPVIEW = "https://api.bsky.app"
DEFAULT_PDS = "https://bsky.social"

POST_COLLECTION = "app.bsky.feed.post"
FEED_COLLECTION = "app.bsky.feed.generator"
LIST_COLLECTION = "app.bsky.graph.list"
STARTER_PACK_COLLECTION = "app.bsky.graph.starterpack"
LABEL_NO_UNAUTHENTICATED = "!no-unauthenticated"

_DID_RE = re.compile(r"^did:[a-z]+:[A-Za-z0-9._:%-]+$")
_HANDLE_RE = re.compile(r"^(?=.{1,253}$)([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$")
_AT_URI_RE = re.compile(r"^at://([^/]+)/([^/]+)/([^/?#]+)$")


class XrpcError(FetchError):
    """The AppView answered with an XRPC error object (e.g. InvalidRequest 'Profile not found')."""

    def __init__(self, status: int, error: str | None, message: str | None) -> None:
        super().__init__(f"HTTP {status} {error or ''}: {message or ''}".strip())
        self.status = status
        self.error = error
        self.message = message


# ----- identifiers ------------------------------------------------------------------------------------------------


def is_did(value: str) -> bool:
    return bool(_DID_RE.match(value))


def is_handle(value: str) -> bool:
    return bool(_HANDLE_RE.match(value))


def normalize_actor(value: str) -> str:
    """Accepts `@handle`, `handle`, `did:...`, `https://bsky.app/profile/<handle|did>[/...]`; returns handle or DID."""
    v = value.strip()
    if v.startswith("http://") or v.startswith("https://"):
        parts = urlsplit(v)
        segs = [s for s in parts.path.split("/") if s]
        if len(segs) >= 2 and segs[0] == "profile":
            v = unquote(segs[1])
        else:
            raise ValueError(f"Not a Bluesky profile URL: {value}")
    v = v.lstrip("@").strip().rstrip("/")
    if is_did(v) or is_handle(v):
        return v
    raise ValueError(f"Not a valid Bluesky handle or DID: {value}")


def parse_at_uri(uri: str) -> tuple[str, str, str]:
    m = _AT_URI_RE.match(uri.strip())
    if not m:
        raise ValueError(f"Not an AT URI: {uri}")
    return m.group(1), m.group(2), m.group(3)


def parse_post_ref(value: str) -> tuple[str, str]:
    """Returns (authority: handle or DID, rkey) for a post given as bsky.app URL or at:// URI."""
    v = value.strip()
    if v.startswith("at://"):
        authority, collection, rkey = parse_at_uri(v)
        if collection != POST_COLLECTION:
            raise ValueError(f"AT URI is not a post ({collection}): {value}")
        return authority, rkey
    parts = urlsplit(v)
    segs = [s for s in parts.path.split("/") if s]
    if len(segs) >= 4 and segs[0] == "profile" and segs[2] == "post":
        return unquote(segs[1]), segs[3]
    raise ValueError(f"Not a Bluesky post URL or AT URI: {value}")


def parse_feed_ref(value: str) -> tuple[str, str]:
    """Returns (authority, rkey) for a feed generator given as bsky.app URL or at:// URI."""
    v = value.strip()
    if v.startswith("at://"):
        authority, collection, rkey = parse_at_uri(v)
        if collection != FEED_COLLECTION:
            raise ValueError(f"AT URI is not a feed generator ({collection}): {value}")
        return authority, rkey
    parts = urlsplit(v)
    segs = [s for s in parts.path.split("/") if s]
    if len(segs) >= 4 and segs[0] == "profile" and segs[2] == "feed":
        return unquote(segs[1]), segs[3]
    raise ValueError(f"Not a Bluesky feed URL or AT URI: {value}")


def parse_list_ref(value: str) -> tuple[str, str, str]:
    """Returns (kind, authority, rkey) for a list or starter pack given as bsky.app URL or at:// URI.

    kind is "list" (https://bsky.app/profile/<h>/lists/<rkey>) or "starterPack" (https://bsky.app/starter-pack/<h>/<rkey>)."""
    v = value.strip()
    if v.startswith("at://"):
        authority, collection, rkey = parse_at_uri(v)
        if collection == LIST_COLLECTION:
            return "list", authority, rkey
        if collection == STARTER_PACK_COLLECTION:
            return "starterPack", authority, rkey
        raise ValueError(f"AT URI is not a list or starter pack ({collection}): {value}")
    parts = urlsplit(v)
    segs = [s for s in parts.path.split("/") if s]
    if len(segs) >= 4 and segs[0] == "profile" and segs[2] == "lists":
        return "list", unquote(segs[1]), segs[3]
    if len(segs) >= 3 and segs[0] == "starter-pack":
        return "starterPack", unquote(segs[1]), segs[2]
    if parts.hostname == "go.bsky.app":
        raise ValueError(f"Short starter-pack links are not supported, open it and copy the bsky.app/starter-pack/... URL: {value}")
    raise ValueError(f"Not a Bluesky list / starter-pack URL or AT URI: {value}")


def post_url(handle_or_did: str, uri: str) -> str | None:
    try:
        _, _, rkey = parse_at_uri(uri)
    except ValueError:
        return None
    return f"https://bsky.app/profile/{handle_or_did}/post/{rkey}"


def profile_url(handle_or_did: str) -> str:
    return f"https://bsky.app/profile/{handle_or_did}"


def has_label(obj: dict[str, Any] | None, value: str) -> bool:
    for label in (obj or {}).get("labels") or []:
        if isinstance(label, dict) and label.get("val") == value:
            return True
    return False


# ----- client -----------------------------------------------------------------------------------------------------


class BskyClient:
    """Minimal XRPC query client with host routing and an optional authenticated session."""

    def __init__(self, http: PoliteClient, *, pds_host: str = DEFAULT_PDS) -> None:
        self.http = http
        self.pds_host = pds_host.rstrip("/")
        self.access_jwt: str | None = None
        self.refresh_jwt: str | None = None
        self.session_did: str | None = None
        self.session_handle: str | None = None
        self._did_cache: dict[str, str] = {}

    @property
    def authenticated(self) -> bool:
        return self.access_jwt is not None

    # ----- session ---------------------------------------------------------------------------------------------

    async def login(self, identifier: str, app_password: str) -> None:
        """Official App Password login (com.atproto.server.createSession). The password is never logged."""
        url = f"{self.pds_host}/xrpc/com.atproto.server.createSession"
        try:
            status, data = await self.http.post_json(url, json={"identifier": identifier, "password": app_password})
        except Blocked as exc:
            raise XrpcError(exc.status, "AuthenticationRequired", "Bluesky rejected the identifier / App Password") from exc
        if status != 200 or not isinstance(data, dict) or not data.get("accessJwt"):
            err = data.get("error") if isinstance(data, dict) else None
            msg = data.get("message") if isinstance(data, dict) else None
            raise XrpcError(status, err or "LoginFailed", msg or "createSession did not return a session")
        self.access_jwt = data["accessJwt"]
        self.refresh_jwt = data.get("refreshJwt")
        self.session_did = data.get("did")
        self.session_handle = data.get("handle")
        # Route every authenticated query through the account's own PDS (it proxies app.bsky.* to the AppView).
        endpoint = _pds_endpoint_from_did_doc(data.get("didDoc"))
        if endpoint:
            self.pds_host = endpoint.rstrip("/")
        self.http.auth_header = lambda: self.access_jwt
        self.http.auth_hosts = {urlsplit(self.pds_host).hostname or ""}
        log.info("Logged in as %s (%s) via %s", self.session_handle, self.session_did, self.pds_host)

    async def refresh_session(self) -> bool:
        if not self.refresh_jwt:
            return False
        url = f"{self.pds_host}/xrpc/com.atproto.server.refreshSession"
        status, data = await self.http.post_json(url, json=None, headers={"Authorization": f"Bearer {self.refresh_jwt}"})
        if status == 200 and isinstance(data, dict) and data.get("accessJwt"):
            self.access_jwt = data["accessJwt"]
            self.refresh_jwt = data.get("refreshJwt") or self.refresh_jwt
            return True
        return False

    # ----- routing ---------------------------------------------------------------------------------------------

    def base_for(self, nsid: str) -> str:
        if self.authenticated:
            return self.pds_host
        if nsid == "app.bsky.feed.searchPosts":
            return SEARCH_APPVIEW
        return PUBLIC_APPVIEW

    async def query(self, nsid: str, params: dict[str, Any]) -> dict[str, Any]:
        """GET /xrpc/{nsid}; raises XrpcError for non-200 JSON error bodies, Blocked/RateLimited/FetchError otherwise."""
        clean = {k: v for k, v in params.items() if v is not None and v != "" and v != []}
        url = f"{self.base_for(nsid)}/xrpc/{nsid}"
        status, data = await self.http.get_json(url, params=clean)
        if status == 200 and isinstance(data, dict):
            return data
        if self.authenticated and status == 400 and isinstance(data, dict) and data.get("error") == "ExpiredToken" and await self.refresh_session():
            status, data = await self.http.get_json(url, params=clean)
            if status == 200 and isinstance(data, dict):
                return data
        err = data.get("error") if isinstance(data, dict) else None
        msg = data.get("message") if isinstance(data, dict) else None
        raise XrpcError(status, err, msg)

    # ----- identity --------------------------------------------------------------------------------------------

    async def resolve_did(self, handle_or_did: str) -> str:
        if is_did(handle_or_did):
            return handle_or_did
        key = handle_or_did.lower()
        if key not in self._did_cache:
            data = await self.query("com.atproto.identity.resolveHandle", {"handle": handle_or_did})
            did = data.get("did")
            if not did:
                raise XrpcError(200, "HandleNotResolved", f"No DID for handle {handle_or_did}")
            self._did_cache[key] = did
        return self._did_cache[key]

    async def post_uri(self, ref: str) -> str:
        authority, rkey = parse_post_ref(ref)
        did = await self.resolve_did(authority)
        return f"at://{did}/{POST_COLLECTION}/{rkey}"

    async def list_uri(self, ref: str) -> tuple[str, str]:
        """(kind, at-uri) for a list or starter pack reference."""
        kind, authority, rkey = parse_list_ref(ref)
        did = await self.resolve_did(authority)
        collection = LIST_COLLECTION if kind == "list" else STARTER_PACK_COLLECTION
        return kind, f"at://{did}/{collection}/{rkey}"

    async def feed_uri(self, ref: str) -> str:
        authority, rkey = parse_feed_ref(ref)
        did = await self.resolve_did(authority)
        return f"at://{did}/{FEED_COLLECTION}/{rkey}"


def _pds_endpoint_from_did_doc(did_doc: Any) -> str | None:
    if not isinstance(did_doc, dict):
        return None
    for svc in did_doc.get("service") or []:
        if isinstance(svc, dict) and str(svc.get("id", "")).endswith("#atproto_pds"):
            endpoint = svc.get("serviceEndpoint")
            if isinstance(endpoint, str) and endpoint.startswith("http"):
                return endpoint
    return None
