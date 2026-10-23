"""Bluesky endpoints with the AppView mocked (respx), using the same fixtures as the Apify Actor's test suite."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from api.index import app

FIX = Path(__file__).parent / "fixtures" / "bsky"
APPVIEW = "https://public.api.bsky.app/xrpc/"


def load(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


@pytest.fixture
def appview():
    with respx.mock(assert_all_called=False) as m:
        m.get(url__regex=r"https://(public\.api|api)\.bsky\.app/xrpc/com\.atproto\.identity\.resolveHandle.*").mock(return_value=httpx.Response(200, json={"did": "did:plc:z72i7hdynmk6r22z27h6tvur"}))
        m.get(url__regex=r".*/xrpc/app\.bsky\.actor\.getProfiles.*").mock(return_value=httpx.Response(200, json=load("profiles.json")))
        m.get(url__regex=r".*/xrpc/app\.bsky\.feed\.getAuthorFeed.*").mock(return_value=httpx.Response(200, json=load("author_feed.json")))
        m.get(url__regex=r".*/xrpc/app\.bsky\.graph\.getFollowers.*").mock(return_value=httpx.Response(200, json=load("followers.json")))
        m.get(url__regex=r".*/xrpc/app\.bsky\.feed\.getLikes.*").mock(return_value=httpx.Response(200, json=load("likes.json")))
        m.get(url__regex=r".*/xrpc/app\.bsky\.feed\.getPostThread.*").mock(return_value=httpx.Response(200, json=load("thread.json")))
        yield m


def test_search_pages_by_time_without_login(appview):
    p1, p2 = load("search_page1.json"), load("search_page2_until.json")
    route = appview.get(url__regex=r".*/xrpc/app\.bsky\.feed\.searchPosts.*").mock(side_effect=[httpx.Response(200, json=p1), httpx.Response(200, json=p2), httpx.Response(200, json={"posts": []})])
    c = TestClient(app)
    r = c.get("/v1/bluesky/search", params={"q": "apify", "limit": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "post" and body["count"] == 5 and body["hitsTotal"] == p1["hitsTotal"]
    assert body["nextUntil"]  # time-window paging token for the next request
    first = route.calls[0].request.url.params
    assert first["q"] == "apify" and first["sort"] == "latest" and "cursor" not in first
    item = body["items"][0]
    for key in ("uri", "url", "text", "createdAt", "likeCount", "repostCount", "replyCount", "hashtags", "mentions", "links"):
        assert key in item, key


def test_posts_cursor_round_trip(appview):
    c = TestClient(app)
    r = c.get("/v1/bluesky/posts", params={"actor": "bsky.app", "limit": 2})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2 and body["nextCursor"]
    sent = appview.calls.last.request.url.params
    assert sent["limit"] == "2"
    r2 = c.get("/v1/bluesky/posts", params={"actor": "bsky.app", "limit": 2, "cursor": body["nextCursor"]})
    assert r2.status_code == 200
    assert appview.calls.last.request.url.params["cursor"] == body["nextCursor"]


def test_profiles_normalized(appview):
    r = TestClient(app).get("/v1/bluesky/profiles", params={"actors": "bsky.app"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "profile" and body["count"] >= 1
    p = body["items"][0]
    assert p["handle"] and "followersCount" in p and "did" in p


def test_engagement_likers_and_thread(appview):
    c = TestClient(app)
    post = "https://bsky.app/profile/bsky.app/post/3mx5e63uvns2d"
    r = c.get("/v1/bluesky/engagement", params={"post": post, "kind": "likers", "limit": 3})
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "profile" and 1 <= r.json()["count"] <= 3
    t = c.get("/v1/bluesky/thread", params={"post": post, "limit": 50})
    assert t.status_code == 200, t.text
    assert t.json()["count"] >= 1


def test_unknown_profile_is_404(appview):
    appview.get(url__regex=r".*/xrpc/app\.bsky\.feed\.getAuthorFeed.*").mock(return_value=httpx.Response(400, json={"error": "InvalidRequest", "message": "Profile not found"}))
    r = TestClient(app).get("/v1/bluesky/posts", params={"actor": "nobody.bsky.social", "limit": 2})
    assert r.status_code == 404
