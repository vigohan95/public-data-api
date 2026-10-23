"""
Bluesky Public Data API: public Bluesky (AT Protocol) data through the official AppView, normalized, no login.

    GET /v1/bluesky/search      ?q=&limit=&sort=latest&since=&until=&lang=&author=&mentions=&domain=&url=&tag=&min_likes=...
    GET /v1/bluesky/posts       ?actor=&filter=posts_with_replies&include_reposts=true&include_pins=false&limit=&cursor=
    GET /v1/bluesky/feed        ?feed=<feed URL or at:// URI>&limit=&cursor=
    GET /v1/bluesky/thread      ?post=<post URL or at:// URI>&depth=&parent_height=&limit=
    GET /v1/bluesky/profiles    ?actors=a,b,c   (up to 25)
    GET /v1/bluesky/followers   ?actor=&limit=&cursor=&sort=latest|top
    GET /v1/bluesky/follows     ?actor=&limit=&cursor=
    GET /v1/bluesky/engagement  ?post=&kind=likers|reposters|quotes&limit=&cursor=
    GET /v1/bluesky/list        ?list=<starter pack / list URL>&limit=&cursor=

What is different from the AT Protocol passthroughs on the hub: search keeps paging without a login (the public
AppView rejects `cursor` on page 2; this pages by time and returns `nextUntil`), engagement audiences (who liked,
reposted or quoted a post), starter-pack and list members, threads with the full reply tree, one normalized post /
profile schema (hashtags, mentions, links, media + alt text, video, link card, quoted post, labels, counts) and
engagement filters. Accounts that opted out of logged-out visibility (`!no-unauthenticated`) are skipped.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from .atproto import DEFAULT_PDS, BskyClient
from .modes import (
    ModeResult,
    run_author_feed,
    run_custom_feed,
    run_graph,
    run_list_members,
    run_post_engagement,
    run_profiles,
    run_search,
    run_thread,
)
from .net import PoliteClient

router = APIRouter(prefix="/v1/bluesky", tags=["bluesky"])

USER_AGENT = "bluesky-public-data-api/1.0 (+https://github.com/vigohan95/public-data-api)"
MAX_LIMIT = 100  # one AppView page per request; paging continues with cursor / until across requests
HARD_MAX = 500  # upstream items scanned per request when engagement filters are on (serverless time budget)


class CollectSink:
    """Collects normalized items up to `limit`, applying the engagement filters; tells the mode when to stop."""

    def __init__(self, limit: int, *, min_likes: int = 0, min_reposts: int = 0, min_replies: int = 0) -> None:
        self.limit = limit
        self.items: list[dict[str, Any]] = []
        self.filtered = 0
        self.min = (min_likes, min_reposts, min_replies)
        self.last_created: str | None = None
        self.filters_on = bool(min_likes or min_reposts or min_replies)

    def _keep(self, item: dict[str, Any]) -> bool:
        ml, mr, mp = self.min
        if not (ml or mr or mp):
            return True
        return int(item.get("likeCount") or 0) >= ml and int(item.get("repostCount") or 0) >= mr and int(item.get("replyCount") or 0) >= mp

    async def push(self, items: list[dict[str, Any]], *, kind: str, chronological: bool) -> bool:  # noqa: ARG002
        for it in items:
            if len(self.items) >= self.limit:
                return False
            if self._keep(it):
                self.items.append(it)
                if isinstance(it.get("createdAt"), str):
                    self.last_created = it["createdAt"]
            else:
                self.filtered += 1
        return len(self.items) < self.limit


def make_client() -> PoliteClient:
    return PoliteClient(user_agent=USER_AGENT, concurrency_per_host=2, requests_per_second=5.0, max_retries=2, timeout_seconds=20.0, max_retry_after_seconds=10.0)


def _scan(sink: "CollectSink", limit: int) -> int:
    """How many upstream items a mode may fetch: exactly one page when nothing is filtered (so the page is fully
    consumed and the next cursor is exact), up to HARD_MAX when engagement filters drop items."""
    return HARD_MAX if sink.filters_on else _limit(limit)


def _limit(limit: int) -> int:
    return max(1, min(limit, MAX_LIMIT))


def _respond(res: ModeResult, sink: CollectSink, **extra: Any) -> dict[str, Any]:
    if res.stop_reason == "blocked":
        raise HTTPException(status_code=403, detail=res.warnings[-1] if res.warnings else "The AppView refused the request.")
    if res.stop_reason == "api_error" and not sink.items:
        msg = res.warnings[-1] if res.warnings else "AppView error."
        raise HTTPException(status_code=404 if ("not found" in msg.lower() or "could not resolve" in msg.lower() or "InvalidRequest" in msg) else 502, detail=msg)
    if res.stop_reason == "network_error" and not sink.items:
        raise HTTPException(status_code=502, detail=res.warnings[-1] if res.warnings else "Could not reach the AppView.")
    if res.stop_reason == "rate_limited" and not sink.items:
        raise HTTPException(status_code=429, detail=res.warnings[-1] if res.warnings else "Rate limited by the AppView; retry shortly.")
    next_until = res.resolved.get("nextUntil")
    if res.stop_reason == "sink" and res.kind == "post" and sink.last_created and "nextUntil" in res.resolved:
        next_until = sink.last_created  # stopped mid-page: continue from the last item returned (it may repeat once; dedupe on uri)
    body: dict[str, Any] = {
        "kind": res.kind,
        "count": len(sink.items),
        "filteredOut": sink.filtered,
        "complete": res.complete,
        "nextCursor": res.resolved.get("nextCursor"),
        "nextUntil": next_until,
        "resolved": {k: v for k, v in res.resolved.items() if k not in ("nextCursor", "nextUntil")},
        "upstreamRequests": res.requests,
        "warnings": res.warnings,
        **extra,
        "items": sink.items,
    }
    return body


@router.get("/search")
async def bluesky_search(
    q: str = Query(..., description="Search phrase, hashtag or keyword (Bluesky search syntax)"),
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
    sort: str = Query("latest", pattern="^(latest|top)$", description="top returns one page only without a login"),
    since: str | None = Query(None, description="ISO 8601 lower bound"),
    until: str | None = Query(None, description="ISO 8601 upper bound; pass the previous response's nextUntil to page"),
    lang: str | None = None,
    author: str | None = Query(None, description="Handle or DID"),
    mentions: str | None = Query(None, description="Handle or DID mentioned"),
    domain: str | None = Query(None, description="Only posts linking to this domain"),
    url: str | None = Query(None, description="Only posts linking to this URL"),
    tag: str | None = Query(None, description="Comma list of hashtags (without #)"),
    min_likes: int = Query(0, ge=0),
    min_reposts: int = Query(0, ge=0),
    min_replies: int = Query(0, ge=0),
    raw: bool = Query(False, description="Include the raw AppView record per item"),
) -> dict[str, Any]:
    """Posts matching a query, newest first, with deep paging that works without a login: pass `nextUntil` back as `until`."""
    filters: dict[str, Any] = {k: v for k, v in {"since": since, "until": until, "lang": lang, "author": author, "mentions": mentions, "domain": domain, "url": url}.items() if v}
    if tag:
        filters["tag"] = [t.strip().lstrip("#") for t in tag.split(",") if t.strip()]
    sink = CollectSink(_limit(limit), min_likes=min_likes, min_reposts=min_reposts, min_replies=min_replies)
    async with make_client() as http:
        client = BskyClient(http, pds_host=DEFAULT_PDS)
        res = await run_search(client, sink, q, filters=filters, sort=sort, max_items=_scan(sink, limit), include_raw=raw)
    return _respond(res, sink, query=q, hitsTotal=res.resolved.get("hitsTotal"))


@router.get("/posts")
async def bluesky_posts(
    actor: str = Query(..., description="Handle, DID or profile URL"),
    filter: str = Query("posts_with_replies", pattern="^(posts_with_replies|posts_no_replies|posts_with_media|posts_and_author_threads|posts_with_video)$"),
    include_reposts: bool = True,
    include_pins: bool = False,
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(None, description="nextCursor from the previous response"),
    min_likes: int = Query(0, ge=0),
    min_reposts: int = Query(0, ge=0),
    min_replies: int = Query(0, ge=0),
    raw: bool = False,
) -> dict[str, Any]:
    """An account's posts / replies / reposts, newest first, normalized."""
    sink = CollectSink(_limit(limit), min_likes=min_likes, min_reposts=min_reposts, min_replies=min_replies)
    async with make_client() as http:
        client = BskyClient(http, pds_host=DEFAULT_PDS)
        res = await run_author_feed(client, sink, actor, feed_filter=filter, include_pins=include_pins, include_reposts=include_reposts, max_items=_scan(sink, limit), include_raw=raw, start_cursor=cursor)
    return _respond(res, sink, actor=actor)


@router.get("/feed")
async def bluesky_feed(
    feed: str = Query(..., description="Custom feed URL (https://bsky.app/profile/.../feed/...) or at:// URI"),
    include_reposts: bool = True,
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
    cursor: str | None = None,
    min_likes: int = Query(0, ge=0),
    min_reposts: int = Query(0, ge=0),
    min_replies: int = Query(0, ge=0),
    raw: bool = False,
) -> dict[str, Any]:
    """Posts from any custom feed generator (Discover, What's Hot, community feeds)."""
    sink = CollectSink(_limit(limit), min_likes=min_likes, min_reposts=min_reposts, min_replies=min_replies)
    async with make_client() as http:
        client = BskyClient(http, pds_host=DEFAULT_PDS)
        res = await run_custom_feed(client, sink, feed, include_reposts=include_reposts, max_items=_scan(sink, limit), include_raw=raw, start_cursor=cursor)
    return _respond(res, sink, feed=feed)


@router.get("/thread")
async def bluesky_thread(
    post: str = Query(..., description="Post URL (https://bsky.app/profile/<handle>/post/<rkey>) or at:// URI"),
    depth: int = Query(50, ge=0, le=1000),
    parent_height: int = Query(80, ge=0, le=1000),
    limit: int = Query(200, ge=1, le=MAX_LIMIT),
    raw: bool = False,
) -> dict[str, Any]:
    """A post with its parents and the full reply tree, flattened to normalized posts with thread positions."""
    sink = CollectSink(_limit(limit))
    async with make_client() as http:
        client = BskyClient(http, pds_host=DEFAULT_PDS)
        res = await run_thread(client, sink, post, depth=depth, parent_height=parent_height, max_items=_limit(limit), include_raw=raw)
    return _respond(res, sink, post=post)


@router.get("/profiles")
async def bluesky_profiles(actors: str = Query(..., description="Comma list of handles, DIDs or profile URLs (max 25)"), raw: bool = False) -> dict[str, Any]:
    """Detailed profiles: bio, counts, labels, pinned post, links found in the bio."""
    wanted = [a.strip() for a in actors.split(",") if a.strip()][:25]
    if not wanted:
        raise HTTPException(status_code=422, detail="actors must list at least one handle or DID")
    sink = CollectSink(len(wanted))
    async with make_client() as http:
        client = BskyClient(http, pds_host=DEFAULT_PDS)
        res = await run_profiles(client, sink, wanted, include_raw=raw)
    return _respond(res, sink, requested=len(wanted))


async def _graph(actor: str, direction: str, sort: str, limit: int, cursor: str | None, raw: bool) -> dict[str, Any]:
    sink = CollectSink(_limit(limit))
    async with make_client() as http:
        client = BskyClient(http, pds_host=DEFAULT_PDS)
        res = await run_graph(client, sink, actor, direction=direction, sort=sort, max_items=_scan(sink, limit), include_raw=raw, start_cursor=cursor)
    return _respond(res, sink, actor=actor, direction=direction)


@router.get("/followers")
async def bluesky_followers(actor: str = Query(...), limit: int = Query(100, ge=1, le=MAX_LIMIT), cursor: str | None = None, sort: str = Query("latest", pattern="^(latest|top)$"), raw: bool = False) -> dict[str, Any]:
    """Who follows an account (profiles), newest first or by follower count."""
    return await _graph(actor, "followers", sort, limit, cursor, raw)


@router.get("/follows")
async def bluesky_follows(actor: str = Query(...), limit: int = Query(100, ge=1, le=MAX_LIMIT), cursor: str | None = None, raw: bool = False) -> dict[str, Any]:
    """Accounts an account follows (profiles)."""
    return await _graph(actor, "follows", "latest", limit, cursor, raw)


@router.get("/engagement")
async def bluesky_engagement(
    post: str = Query(..., description="Post URL or at:// URI"),
    kind: str = Query("likers", pattern="^(likers|reposters|quotes)$"),
    limit: int = Query(100, ge=1, le=MAX_LIMIT),
    cursor: str | None = None,
    raw: bool = False,
) -> dict[str, Any]:
    """Who liked or reposted a post (profiles, with the like time), or every quote post of it."""
    sink = CollectSink(_limit(limit))
    async with make_client() as http:
        client = BskyClient(http, pds_host=DEFAULT_PDS)
        res = await run_post_engagement(client, sink, post, mode=kind, max_items=_scan(sink, limit), include_raw=raw, start_cursor=cursor)
    return _respond(res, sink, post=post, engagement=kind)


@router.get("/list")
async def bluesky_list(
    list: str = Query(..., description="Starter pack URL, list URL or at:// URI"),  # noqa: A002
    limit: int = Query(100, ge=1, le=MAX_LIMIT),
    cursor: str | None = None,
    raw: bool = False,
) -> dict[str, Any]:
    """Members of a starter pack or a curated / moderation list (profiles)."""
    sink = CollectSink(_limit(limit))
    async with make_client() as http:
        client = BskyClient(http, pds_host=DEFAULT_PDS)
        res = await run_list_members(client, sink, list, max_items=_scan(sink, limit), include_raw=raw, start_cursor=cursor)
    return _respond(res, sink, list=list)
