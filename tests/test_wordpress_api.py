"""API-level tests with the upstream WordPress site mocked (respx). No network."""
from __future__ import annotations

import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from api.index import app

SITE = "https://blog.example"
HOME_HTML = '<html lang="en-US"><head><link rel="https://api.w.org/" href="https://blog.example/wp-json/" /><meta name="generator" content="WordPress 7.1" /><title>Example Blog</title></head><body></body></html>'
REST_INDEX = {"name": "Example Blog", "description": "d", "url": SITE, "home": SITE, "gmt_offset": 0, "timezone_string": "UTC", "namespaces": ["wp/v2"]}
TYPES = {"post": {"slug": "post", "name": "Posts", "rest_base": "posts"}, "page": {"slug": "page", "name": "Pages", "rest_base": "pages"}}
POST = {
    "id": 7,
    "type": "post",
    "slug": "hello",
    "link": f"{SITE}/2026/10/hello/",
    "status": "publish",
    "date_gmt": "2026-10-20T10:00:00",
    "modified_gmt": "2026-10-21T10:00:00",
    "title": {"rendered": "Hello &amp; welcome"},
    "excerpt": {"rendered": "<p>Short.</p>"},
    "content": {"rendered": "<h2>Heading</h2><p>Body with <a href=\"/x\">a link</a>.</p>"},
    "author": 1,
    "categories": [3],
    "tags": [],
    "featured_media": 0,
    "_embedded": {"author": [{"id": 1, "name": "Ann", "slug": "ann", "link": f"{SITE}/author/ann/"}], "wp:term": [[{"id": 3, "name": "News", "slug": "news", "taxonomy": "category", "link": f"{SITE}/category/news/"}]]},
}


@pytest.fixture
def wp():
    with respx.mock(assert_all_called=False) as m:
        m.get(f"{SITE}/").mock(return_value=httpx.Response(200, text=HOME_HTML, headers={"content-type": "text/html"}))
        m.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(404))
        m.get(url__regex=rf"{SITE}/wp-json/\?_fields=.*").mock(return_value=httpx.Response(200, json=REST_INDEX))
        m.get(url__regex=rf"{SITE}/wp-json/wp/v2/types.*").mock(return_value=httpx.Response(200, json=TYPES))
        m.get(url__regex=rf"{SITE}/wp-json/wp/v2/posts.*").mock(return_value=httpx.Response(200, json=[POST], headers={"X-WP-Total": "1", "X-WP-TotalPages": "1"}))
        yield m


def test_site_discovery(wp):
    r = TestClient(app).get("/v1/wordpress/site", params={"url": SITE})
    assert r.status_code == 200, r.text
    site = r.json()["site"]
    assert site["apiRoot"] == f"{SITE}/wp-json/"
    assert site["name"] == "Example Blog"
    assert set(site["types"]) == {"post", "page"}


def test_posts_markdown_and_text(wp):
    r = TestClient(app).get("/v1/wordpress/posts", params={"url": SITE, "per_page": 5, "fields": "markdown,text,html"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "rest" and body["total"] == 1 and body["totalPages"] == 1
    item = body["items"][0]
    assert item["title"] == "Hello & welcome"
    assert item["authorName"] == "Ann" and item["categoryNames"] == ["News"]
    assert "## Heading" in item["contentMarkdown"]
    assert "a link" in item["contentText"] and "<" not in item["contentText"]
    assert item["contentHtml"].startswith("<h2>")
    assert item["wordCount"] > 0 and item["publishedAt"] == "2026-10-20T10:00:00Z"
    sent = json.loads(json.dumps(dict(wp.calls.last.request.url.params)))
    assert sent["per_page"] == "5" and sent["page"] == "1" and sent["status"] == "publish"


def test_unknown_post_type_is_404(wp):
    r = TestClient(app).get("/v1/wordpress/posts", params={"url": SITE, "type": "nope"})
    assert r.status_code == 404
    assert "Available: page, post" in r.json()["detail"]


def test_bad_fields_is_422(wp):
    r = TestClient(app).get("/v1/wordpress/posts", params={"url": SITE, "fields": "markdown,pdf"})
    assert r.status_code == 422
    assert "pdf" in r.json()["detail"]


def test_rapidapi_secret_gate(wp, monkeypatch):
    monkeypatch.setenv("RAPIDAPI_PROXY_SECRET", "s3cret")
    monkeypatch.setenv("RAPIDAPI_PROXY_SECRET_2", "other-listing")
    c = TestClient(app)
    assert c.get("/health").status_code == 200  # health stays open for uptime checks
    assert c.get("/v1/wordpress/site", params={"url": SITE}).status_code == 403
    assert c.get("/v1/wordpress/site", params={"url": SITE}, headers={"X-RapidAPI-Proxy-Secret": "s3cret"}).status_code == 200
    assert c.get("/v1/wordpress/site", params={"url": SITE}, headers={"X-RapidAPI-Proxy-Secret": "other-listing"}).status_code == 200
    assert c.get("/v1/wordpress/site", params={"url": SITE}, headers={"X-RapidAPI-Proxy-Secret": "wrong"}).status_code == 403
