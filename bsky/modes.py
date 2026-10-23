"""The read modes (search, feeds, thread, profiles, graph, engagement, lists). Each one pages through the AppView and hands normalized items to a Sink.

A Sink decides what to do with each page (dedupe, compliance filter, incremental monitoring, max items, charging)
and tells the mode whether to keep paging. Modes never talk to the Apify SDK, so they are testable with respx.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from .atproto import LABEL_NO_UNAUTHENTICATED, BskyClient, XrpcError, has_label, normalize_actor
from .net import Blocked, FetchError, RateLimited
from .normalize import flatten_thread, normalize_post, normalize_profile

log = logging.getLogger("bluesky-public-data.modes")

PAGE_LIMIT = 100  # the lexicon maximum for every list endpoint used here
PROFILES_BATCH = 25  # app.bsky.actor.getProfiles max actors


class Sink(Protocol):
    async def push(self, items: list[dict[str, Any]], *, kind: str, chronological: bool) -> bool:
        """Returns True to continue paging, False to stop."""


@dataclass(slots=True)
class ModeResult:
    target: str
    kind: str  # post | profile
    requests: int = 0
    fetched: int = 0  # items received from the API (before sink filtering)
    complete: bool = False
    stop_reason: str | None = None
    warnings: list[str] = field(default_factory=list)
    resolved: dict[str, Any] = field(default_factory=dict)


def _sort_at(post: dict[str, Any]) -> str | None:
    """AppView sorts by sortAt = min(record.createdAt, indexedAt) - mirror that for `until` paging."""
    created = ((post.get("record") or {}).get("createdAt")) or None
    indexed = post.get("indexedAt") or None
    candidates = [t for t in (created, indexed) if isinstance(t, str)]
    return min(candidates) if candidates else None


def _skip_no_unauth(post: dict[str, Any], *, authed: bool, respect: bool) -> bool:
    return respect and not authed and has_label(post.get("author"), LABEL_NO_UNAUTHENTICATED)


async def _guard(res: ModeResult, exc: Exception) -> None:
    if isinstance(exc, Blocked):
        res.stop_reason = "blocked"
        res.warnings.append(f"{exc} - the AppView refused the request (unauthenticated restriction?)")
    elif isinstance(exc, RateLimited):
        res.stop_reason = "rate_limited"
        res.warnings.append(str(exc))
    elif isinstance(exc, XrpcError):
        res.stop_reason = "api_error"
        res.warnings.append(str(exc))
    elif isinstance(exc, FetchError):
        res.stop_reason = "network_error"
        res.warnings.append(str(exc))
    else:
        raise exc
    log.warning("[%s] stopped: %s", res.target, res.warnings[-1])


# ----- search ------------------------------------------------------------------------------------------------------


async def run_search(
    client: BskyClient,
    sink: Sink,
    query: str,
    *,
    filters: dict[str, Any],
    sort: str = "latest",
    max_items: int,
    include_raw: bool,
    respect_no_unauth: bool = True,
) -> ModeResult:
    """Keyword search. Unauthenticated: `cursor` is forbidden (403) since mid-2026, so page by time instead:
    sort=latest + until=<oldest sortAt of the previous page>; the boundary post repeats and is deduped by uri.
    sort=top cannot page without login (first page of up to 100 posts only). Authenticated: regular cursors."""
    res = ModeResult(target=f"search:{query}", kind="post")
    params: dict[str, Any] = {"q": query, "sort": sort, "limit": min(PAGE_LIMIT, max_items), **filters}
    until: str | None = filters.get("until")
    cursor: str | None = None
    seen: set[str] = set()
    pages = 0
    try:
        while True:
            page_params = dict(params)
            if client.authenticated:
                page_params["cursor"] = cursor
            else:
                page_params["until"] = until
            data = await client.query("app.bsky.feed.searchPosts", page_params)
            res.requests += 1
            pages += 1
            if pages == 1 and isinstance(data.get("hitsTotal"), int):
                res.resolved["hitsTotal"] = data["hitsTotal"]
            posts = [p for p in data.get("posts") or [] if isinstance(p, dict)]
            fresh = [p for p in posts if p.get("uri") and p["uri"] not in seen]
            for p in fresh:
                seen.add(p["uri"])
            res.fetched += len(fresh)
            items = [
                normalize_post(p, include_raw=include_raw, extra={"source": "search", "query": query})
                for p in fresh
                if not _skip_no_unauth(p, authed=client.authenticated, respect=respect_no_unauth)
            ]
            oldest_here = min((t for t in (_sort_at(p) for p in posts) if t), default=None)
            if oldest_here and not client.authenticated and sort == "latest":
                res.resolved["nextUntil"] = oldest_here  # pass as `until` to continue in a later request
            if client.authenticated and data.get("cursor"):
                res.resolved["nextCursor"] = data.get("cursor")
            if items and not await sink.push(items, kind="post", chronological=(sort == "latest")):
                res.stop_reason = "sink"
                return res
            if res.fetched >= max_items:
                res.stop_reason = "max_items"
                return res
            if not posts or (not fresh and pages > 1):
                res.complete = True
                res.stop_reason = "exhausted"
                return res
            if client.authenticated:
                cursor = data.get("cursor")
                if not cursor:
                    res.complete = True
                    res.stop_reason = "exhausted"
                    return res
            else:
                if sort != "latest":
                    res.stop_reason = "top_first_page_only"
                    res.warnings.append("sort=top returns only the first page without login; use sort=latest or an App Password")
                    return res
                oldest = min((t for t in (_sort_at(p) for p in posts) if t), default=None)
                if oldest is None or oldest == until:
                    res.complete = True
                    res.stop_reason = "exhausted"
                    return res
                until = oldest
    except Exception as exc:  # noqa: BLE001
        await _guard(res, exc)
        return res


# ----- author feed / custom feed --------------------------------------------------------------------------------------


async def _run_feed_pages(
    client: BskyClient,
    sink: Sink,
    res: ModeResult,
    nsid: str,
    params: dict[str, Any],
    *,
    source: str,
    max_items: int,
    include_raw: bool,
    include_reposts: bool,
    respect_no_unauth: bool,
    extra: dict[str, Any],
    start_cursor: str | None = None,
) -> ModeResult:
    cursor: str | None = start_cursor
    seen: set[str] = set()
    try:
        while True:
            page_cursor = cursor
            data = await client.query(nsid, {**params, "limit": min(PAGE_LIMIT, max_items), "cursor": cursor})
            res.requests += 1
            entries = [e for e in data.get("feed") or [] if isinstance(e, dict) and isinstance(e.get("post"), dict)]
            items: list[dict[str, Any]] = []
            new_in_page = 0
            for e in entries:
                post, reason = e["post"], e.get("reason")
                uri = post.get("uri")
                is_repost = bool(reason and reason.get("$type") == "app.bsky.feed.defs#reasonRepost")
                key = f"{uri}|{(reason or {}).get('by', {}).get('did', '')}" if is_repost else uri
                if not uri or key in seen:
                    continue
                seen.add(key)
                res.fetched += 1
                new_in_page += 1
                if is_repost and not include_reposts:
                    continue
                if _skip_no_unauth(post, authed=client.authenticated, respect=respect_no_unauth):
                    continue
                items.append(normalize_post(post, reason=reason, include_raw=include_raw, extra={"source": source, **extra}))
            if items and not await sink.push(items, kind="post", chronological=True):
                res.stop_reason = "sink"
                res.resolved["nextCursor"] = page_cursor or data.get("cursor")
                return res
            if data.get("cursor"):
                res.resolved["nextCursor"] = data.get("cursor")
            if res.fetched >= max_items:
                res.stop_reason = "max_items"
                return res
            next_cursor = data.get("cursor")
            # Stop on an empty page, a missing cursor, a repeated cursor or a page with nothing new (loop guards).
            if not entries or not next_cursor or next_cursor == cursor or new_in_page == 0:
                res.resolved.pop("nextCursor", None)
                res.complete = True
                res.stop_reason = "exhausted"
                return res
            cursor = next_cursor
    except Exception as exc:  # noqa: BLE001
        await _guard(res, exc)
        return res


async def run_author_feed(
    client: BskyClient,
    sink: Sink,
    actor: str,
    *,
    feed_filter: str = "posts_with_replies",
    include_pins: bool = False,
    include_reposts: bool = True,
    max_items: int,
    include_raw: bool,
    respect_no_unauth: bool = True,
    start_cursor: str | None = None,
) -> ModeResult:
    res = ModeResult(target=f"authorFeed:{actor}", kind="post")
    try:
        ident = normalize_actor(actor)
    except ValueError as exc:
        res.stop_reason = "invalid_input"
        res.warnings.append(str(exc))
        return res
    res.resolved["actor"] = ident
    params = {"actor": ident, "filter": feed_filter, "includePins": "true" if include_pins else None}
    return await _run_feed_pages(
        client, sink, res, "app.bsky.feed.getAuthorFeed", params,
        source="authorFeed", max_items=max_items, include_raw=include_raw, include_reposts=include_reposts,
        respect_no_unauth=respect_no_unauth, start_cursor=start_cursor, extra={"feedActor": ident},
    )


async def run_custom_feed(
    client: BskyClient,
    sink: Sink,
    feed_ref: str,
    *,
    include_reposts: bool = True,
    max_items: int,
    include_raw: bool,
    respect_no_unauth: bool = True,
    start_cursor: str | None = None,
) -> ModeResult:
    res = ModeResult(target=f"customFeed:{feed_ref}", kind="post")
    try:
        feed_uri = await client.feed_uri(feed_ref)
    except ValueError as exc:
        res.stop_reason = "invalid_input"
        res.warnings.append(str(exc))
        return res
    except Exception as exc:  # noqa: BLE001
        await _guard(res, exc)
        return res
    res.resolved["feedUri"] = feed_uri
    return await _run_feed_pages(
        client, sink, res, "app.bsky.feed.getFeed", {"feed": feed_uri},
        source="customFeed", max_items=max_items, include_raw=include_raw, include_reposts=include_reposts,
        respect_no_unauth=respect_no_unauth, start_cursor=start_cursor, extra={"feedUri": feed_uri},
    )


# ----- thread -------------------------------------------------------------------------------------------------------


async def run_thread(
    client: BskyClient,
    sink: Sink,
    post_ref: str,
    *,
    depth: int = 50,
    parent_height: int = 80,
    max_items: int,
    include_raw: bool,
    respect_no_unauth: bool = True,
) -> ModeResult:
    res = ModeResult(target=f"thread:{post_ref}", kind="post")
    try:
        uri = await client.post_uri(post_ref)
    except ValueError as exc:
        res.stop_reason = "invalid_input"
        res.warnings.append(str(exc))
        return res
    except Exception as exc:  # noqa: BLE001
        await _guard(res, exc)
        return res
    res.resolved["postUri"] = uri
    try:
        data = await client.query("app.bsky.feed.getPostThread", {"uri": uri, "depth": depth, "parentHeight": parent_height})
        res.requests += 1
    except Exception as exc:  # noqa: BLE001
        await _guard(res, exc)
        return res
    nodes, warnings = flatten_thread(data.get("thread") or {}, max_items=max_items)
    res.warnings.extend(warnings)
    res.fetched = len(nodes)
    items = [
        normalize_post(post, include_raw=include_raw, extra={"source": "thread", "threadFocusUri": uri, **extra})
        for post, extra in nodes
        if not _skip_no_unauth(post, authed=client.authenticated, respect=respect_no_unauth)
    ]
    if items and not await sink.push(items, kind="post", chronological=False):
        res.stop_reason = "sink"
        return res
    res.complete = len(nodes) < max_items
    res.stop_reason = "exhausted" if res.complete else "max_items"
    return res


# ----- profiles -----------------------------------------------------------------------------------------------------


async def run_profiles(
    client: BskyClient,
    sink: Sink,
    actors: list[str],
    *,
    include_raw: bool,
    respect_no_unauth: bool = True,
) -> ModeResult:
    res = ModeResult(target=f"profile:{len(actors)} accounts", kind="profile")
    idents: list[str] = []
    for a in actors:
        try:
            idents.append(normalize_actor(a))
        except ValueError as exc:
            res.warnings.append(str(exc))
    res.resolved["actors"] = idents
    try:
        for i in range(0, len(idents), PROFILES_BATCH):
            batch = idents[i : i + PROFILES_BATCH]
            data = await client.query("app.bsky.actor.getProfiles", {"actors": batch})
            res.requests += 1
            profiles = [p for p in data.get("profiles") or [] if isinstance(p, dict)]
            res.fetched += len(profiles)
            got = {p.get("handle", "").lower() for p in profiles} | {p.get("did") for p in profiles}
            for ident in batch:
                if ident.lower() not in got and ident not in got:
                    res.warnings.append(f"profile not found: {ident}")
            items = [
                normalize_profile(p, include_raw=include_raw, extra={"source": "profile", "edgeType": "profile"})
                for p in profiles
                if not (respect_no_unauth and not client.authenticated and has_label(p, LABEL_NO_UNAUTHENTICATED))
            ]
            if items and not await sink.push(items, kind="profile", chronological=False):
                res.stop_reason = "sink"
                return res
        res.complete = True
        res.stop_reason = "exhausted"
    except Exception as exc:  # noqa: BLE001
        await _guard(res, exc)
    return res


# ----- graph --------------------------------------------------------------------------------------------------------


async def run_graph(
    client: BskyClient,
    sink: Sink,
    actor: str,
    *,
    direction: str,  # followers | follows
    sort: str = "latest",
    max_items: int,
    include_raw: bool,
    respect_no_unauth: bool = True,
    start_cursor: str | None = None,
) -> ModeResult:
    res = ModeResult(target=f"{direction}:{actor}", kind="profile")
    try:
        ident = normalize_actor(actor)
    except ValueError as exc:
        res.stop_reason = "invalid_input"
        res.warnings.append(str(exc))
        return res
    res.resolved["actor"] = ident
    nsid = "app.bsky.graph.getFollowers" if direction == "followers" else "app.bsky.graph.getFollows"
    list_key = "followers" if direction == "followers" else "follows"
    edge_type = "follower" if direction == "followers" else "following"
    cursor: str | None = start_cursor
    seen: set[str] = set()
    subject: dict[str, Any] = {}
    try:
        while True:
            params = {"actor": ident, "limit": min(PAGE_LIMIT, max_items), "cursor": cursor}
            if direction == "followers":
                params["sort"] = sort
            page_cursor = cursor
            data = await client.query(nsid, params)
            res.requests += 1
            if not subject and isinstance(data.get("subject"), dict):
                subject = data["subject"]
                res.resolved["subjectDid"] = subject.get("did")
                res.resolved["subjectHandle"] = subject.get("handle")
            profiles = [p for p in data.get(list_key) or [] if isinstance(p, dict) and p.get("did") and p["did"] not in seen]
            for p in profiles:
                seen.add(p["did"])
            res.fetched += len(profiles)
            extra = {"source": direction, "edgeType": edge_type, "subjectDid": subject.get("did"), "subjectHandle": subject.get("handle")}
            items = [
                normalize_profile(p, include_raw=include_raw, extra=extra)
                for p in profiles
                if not (respect_no_unauth and not client.authenticated and has_label(p, LABEL_NO_UNAUTHENTICATED))
            ]
            if items and not await sink.push(items, kind="profile", chronological=False):
                res.stop_reason = "sink"
                res.resolved["nextCursor"] = page_cursor or data.get("cursor")
                return res
            if data.get("cursor"):
                res.resolved["nextCursor"] = data.get("cursor")
            if res.fetched >= max_items:
                res.stop_reason = "max_items"
                return res
            next_cursor = data.get("cursor")
            if not profiles or not next_cursor or next_cursor == cursor:
                res.complete = True
                res.stop_reason = "exhausted"
                return res
            cursor = next_cursor
    except Exception as exc:  # noqa: BLE001
        await _guard(res, exc)
        return res


# ----- post engagement: likers / reposters / quotes ---------------------------------------------------------------

_ENGAGEMENT = {
    # mode: (nsid, list key, edge type)
    "likers": ("app.bsky.feed.getLikes", "likes", "liker"),
    "reposters": ("app.bsky.feed.getRepostedBy", "repostedBy", "reposter"),
}


async def run_post_engagement(
    client: BskyClient,
    sink: Sink,
    post_ref: str,
    *,
    mode: str,  # likers | reposters | quotes
    max_items: int,
    include_raw: bool,
    respect_no_unauth: bool = True,
    start_cursor: str | None = None,
) -> ModeResult:
    """Who liked / reposted a post (profiles), or the posts quoting it (posts). Cursor-paged, public AppView."""
    kind = "post" if mode == "quotes" else "profile"
    res = ModeResult(target=f"{mode}:{post_ref}", kind=kind)
    try:
        uri = await client.post_uri(post_ref)
    except ValueError as exc:
        res.stop_reason = "invalid_input"
        res.warnings.append(str(exc))
        return res
    except Exception as exc:  # noqa: BLE001
        await _guard(res, exc)
        return res
    res.resolved["postUri"] = uri
    cursor: str | None = start_cursor
    seen: set[str] = set()
    try:
        while True:
            page_cursor = cursor
            params = {"uri": uri, "limit": min(PAGE_LIMIT, max_items), "cursor": cursor}
            if mode == "quotes":
                data = await client.query("app.bsky.feed.getQuotes", params)
                res.requests += 1
                posts = [p for p in data.get("posts") or [] if isinstance(p, dict) and p.get("uri") and p["uri"] not in seen]
                for p in posts:
                    seen.add(p["uri"])
                res.fetched += len(posts)
                items = [
                    normalize_post(p, include_raw=include_raw, extra={"source": "quotes", "quotedUri": uri})
                    for p in posts
                    if not _skip_no_unauth(p, authed=client.authenticated, respect=respect_no_unauth)
                ]
                page_size = len(posts)
            else:
                nsid, list_key, edge_type = _ENGAGEMENT[mode]
                data = await client.query(nsid, params)
                res.requests += 1
                rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
                for row in data.get(list_key) or []:
                    if not isinstance(row, dict):
                        continue
                    profile = row.get("actor") if mode == "likers" else row
                    if not isinstance(profile, dict) or not profile.get("did") or profile["did"] in seen:
                        continue
                    seen.add(profile["did"])
                    extra = {"source": mode, "edgeType": edge_type, "subjectDid": uri, "subjectUri": uri}
                    if mode == "likers":
                        extra["likedAt"] = row.get("createdAt")
                    rows.append((profile, extra))
                res.fetched += len(rows)
                items = [
                    normalize_profile(p, include_raw=include_raw, extra=extra)
                    for p, extra in rows
                    if not (respect_no_unauth and not client.authenticated and has_label(p, LABEL_NO_UNAUTHENTICATED))
                ]
                page_size = len(rows)
            if items and not await sink.push(items, kind=kind, chronological=False):
                res.stop_reason = "sink"
                res.resolved["nextCursor"] = page_cursor or data.get("cursor")
                return res
            if data.get("cursor"):
                res.resolved["nextCursor"] = data.get("cursor")
            if res.fetched >= max_items:
                res.stop_reason = "max_items"
                return res
            next_cursor = data.get("cursor")
            if not page_size or not next_cursor or next_cursor == cursor:
                res.complete = True
                res.stop_reason = "exhausted"
                return res
            cursor = next_cursor
    except Exception as exc:  # noqa: BLE001
        await _guard(res, exc)
        return res


# ----- list members / starter packs -------------------------------------------------------------------------------


async def run_list_members(
    client: BskyClient,
    sink: Sink,
    list_ref: str,
    *,
    max_items: int,
    include_raw: bool,
    respect_no_unauth: bool = True,
    start_cursor: str | None = None,
) -> ModeResult:
    """Members of a curated / moderation list, or of the list behind a starter pack (resolved first)."""
    res = ModeResult(target=f"listMembers:{list_ref}", kind="profile")
    try:
        kind, uri = await client.list_uri(list_ref)
        if kind == "starterPack":
            data = await client.query("app.bsky.graph.getStarterPack", {"starterPack": uri})
            res.requests += 1
            pack = data.get("starterPack") or {}
            res.resolved["starterPackUri"] = uri
            res.resolved["starterPackName"] = (pack.get("record") or {}).get("name")
            list_uri = (pack.get("list") or {}).get("uri") or (pack.get("record") or {}).get("list")
            if not list_uri:
                res.stop_reason = "api_error"
                res.warnings.append(f"starter pack {uri} has no member list")
                return res
            uri = list_uri
    except ValueError as exc:
        res.stop_reason = "invalid_input"
        res.warnings.append(str(exc))
        return res
    except Exception as exc:  # noqa: BLE001
        await _guard(res, exc)
        return res
    res.resolved["listUri"] = uri
    cursor: str | None = start_cursor
    seen: set[str] = set()
    try:
        while True:
            page_cursor = cursor
            data = await client.query("app.bsky.graph.getList", {"list": uri, "limit": min(PAGE_LIMIT, max_items), "cursor": cursor})
            res.requests += 1
            meta = data.get("list") or {}
            if "listName" not in res.resolved:
                res.resolved["listName"] = meta.get("name")
                res.resolved["listItemCount"] = meta.get("listItemCount")
            profiles = []
            for row in data.get("items") or []:
                p = (row or {}).get("subject") if isinstance(row, dict) else None
                if isinstance(p, dict) and p.get("did") and p["did"] not in seen:
                    seen.add(p["did"])
                    profiles.append(p)
            res.fetched += len(profiles)
            extra = {
                "source": "listMembers",
                "edgeType": "listMember",
                "subjectDid": uri,
                "subjectUri": uri,
                "listName": res.resolved.get("listName"),
                "starterPackName": res.resolved.get("starterPackName"),
            }
            items = [
                normalize_profile(p, include_raw=include_raw, extra=extra)
                for p in profiles
                if not (respect_no_unauth and not client.authenticated and has_label(p, LABEL_NO_UNAUTHENTICATED))
            ]
            if items and not await sink.push(items, kind="profile", chronological=False):
                res.stop_reason = "sink"
                res.resolved["nextCursor"] = page_cursor or data.get("cursor")
                return res
            if data.get("cursor"):
                res.resolved["nextCursor"] = data.get("cursor")
            if res.fetched >= max_items:
                res.stop_reason = "max_items"
                return res
            next_cursor = data.get("cursor")
            if not profiles or not next_cursor or next_cursor == cursor:
                res.complete = True
                res.stop_reason = "exhausted"
                return res
            cursor = next_cursor
    except Exception as exc:  # noqa: BLE001
        await _guard(res, exc)
        return res
