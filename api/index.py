"""
WordPress Content Export API: any public WordPress site -> clean JSON (HTML + Markdown + plain text) in one call.

    GET /v1/wordpress/site     ?url=https://example.com
    GET /v1/wordpress/posts    ?url=...&type=post&page=1&per_page=20&search=&modified_after=&fields=markdown,text
    GET /v1/wordpress/terms    ?url=...&kind=category|tag&page=1&per_page=100
    GET /v1/wordpress/authors  ?url=...&page=1&per_page=100
    GET /v1/wordpress/media    ?url=...&page=1&per_page=50
    GET /health

The core (discovery, REST paging, HTML fallback, Markdown conversion, politeness) is the same code as the Apify Actor
"wordpress-content-export"; this module only adds the HTTP surface. Each request does at most a handful of upstream
calls (one listing page plus, in fallback mode, one fetch per returned item), so it fits a serverless time budget.

Deployed on Vercel (Python runtime, `vercel.json` rewrites everything to this app) and listed on RapidAPI; when
RAPIDAPI_PROXY_SECRET is set, requests must carry the matching X-RapidAPI-Proxy-Secret header.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Vercel runs api/index.py with its own cwd

from fastapi import FastAPI, HTTPException, Query, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from wpx.discovery import Discoverer  # noqa: E402
from wpx.fallback import UrlLister, extract_article  # noqa: E402
from wpx.models import ContentItem, Site  # noqa: E402
from wpx.net import Blocked, FetchError, PoliteClient  # noqa: E402
from wpx.rest import RestClient, RestError  # noqa: E402
from wpx.robots import RobotsPolicy  # noqa: E402

VERSION = "1.0.0"
USER_AGENT = f"wordpress-content-export-api/{VERSION} (+https://github.com/vigohan95/public-data-api)"
MAX_PER_PAGE = 100
MAX_PER_PAGE_FALLBACK = 20  # each item is one page fetch in fallback mode
FIELD_NAMES = ("html", "markdown", "text", "raw")

app = FastAPI(
    title="WordPress Content Export API",
    version=VERSION,
    description=__doc__,
    docs_url="/docs",
    redoc_url=None,
)


# ----------------------------------------------------------------------------------------------------------------
# plumbing
# ----------------------------------------------------------------------------------------------------------------


@app.middleware("http")
async def rapidapi_gate(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Only the RapidAPI proxy may call the deployed app once the secret is configured (direct calls would bypass billing)."""
    secret = os.environ.get("RAPIDAPI_PROXY_SECRET")
    if secret and request.url.path.startswith("/v1/") and request.headers.get("x-rapidapi-proxy-secret") != secret:
        return JSONResponse({"error": "forbidden", "message": "Call this API through RapidAPI."}, status_code=403)
    return await call_next(request)


def make_client() -> PoliteClient:
    return PoliteClient(
        user_agent=USER_AGENT,
        concurrency_per_host=2,
        requests_per_second_per_host=2.0,
        max_retries=2,
        timeout_seconds=20.0,
        robots=RobotsPolicy("strict", user_agent="wordpress-content-export-api"),
        max_retry_after_seconds=10.0,
    )


async def resolve_site(client: PoliteClient, url: str) -> Site:
    try:
        site = await Discoverer(client).discover(url, mode="auto")
    except Blocked as exc:
        raise HTTPException(status_code=403, detail=f"The site refuses automated access ({exc}).") from exc
    except FetchError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach the site ({exc}).") from exc
    if site.source in ("blocked", "error"):
        raise HTTPException(status_code=502, detail=site.rest_error or "The site could not be read.")
    return site


def parse_fields(fields: str | None) -> set[str]:
    chosen = {f.strip().lower() for f in (fields or "markdown,text").split(",") if f.strip()}
    unknown = chosen - set(FIELD_NAMES)
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown fields: {', '.join(sorted(unknown))}. Choose from {', '.join(FIELD_NAMES)}.")
    return chosen


def item_payload(item: ContentItem, site: Site, fields: set[str]) -> dict[str, Any]:
    item.derive_content_fields(markdown="markdown" in fields, text="text" in fields, base_url=item.url or site.home_url)
    return item.to_dict(include_html="html" in fields, include_markdown="markdown" in fields, include_text="text" in fields, include_raw="raw" in fields)


def site_summary(site: Site) -> dict[str, Any]:
    d = site.to_dict()
    d["types"] = {slug: {"name": t.get("name"), "restBase": t.get("rest_base")} for slug, t in sorted(site.types.items())}
    return d


# ----------------------------------------------------------------------------------------------------------------
# endpoints
# ----------------------------------------------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "version": VERSION}


@app.get("/")
async def index() -> dict[str, Any]:
    return {
        "name": app.title,
        "version": VERSION,
        "docs": "/docs",
        "endpoints": ["/v1/wordpress/site", "/v1/wordpress/posts", "/v1/wordpress/terms", "/v1/wordpress/authors", "/v1/wordpress/media"],
    }


@app.get("/v1/wordpress/site")
async def wordpress_site(url: str = Query(..., description="Any URL of the site (homepage, a post, a blog in a sub-directory)")) -> dict[str, Any]:
    """Detect WordPress, find the REST root and list the public post types. Free in the RapidAPI plans."""
    async with make_client() as client:
        site = await resolve_site(client, url)
        warnings: list[str] = []
        if site.api_root:
            try:
                site.types = await RestClient(client, site).fetch_types()
            except RestError as exc:
                warnings.append(str(exc))
        return {"site": site_summary(site), "warnings": warnings}


@app.get("/v1/wordpress/posts")
async def wordpress_posts(
    url: str = Query(..., description="Any URL of the WordPress site"),
    type: str = Query("post", description="Post type slug: post, page, or any public custom type (see /site)"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=MAX_PER_PAGE),
    search: str | None = Query(None, description="Full-text search phrase (REST mode only)"),
    modified_after: str | None = Query(None, description="ISO 8601; only items modified after this instant"),
    orderby: str = Query("date", pattern="^(date|modified|title|id)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    fields: str | None = Query(None, description="Comma list of content fields to include: html, markdown, text, raw (default markdown,text)"),
) -> dict[str, Any]:
    """One page of posts (or pages / any custom type) with cleaned HTML, Markdown, plain text, word count, resolved
    categories, tags, author and featured image. Uses the WordPress REST API; falls back to sitemap/RSS discovery plus
    article extraction when the REST API is disabled (then `source` is "html" and `search`/`orderby` do not apply)."""
    chosen = parse_fields(fields)
    async with make_client() as client:
        site = await resolve_site(client, url)
        warnings: list[str] = []
        if site.api_root:
            rest = RestClient(client, site)
            try:
                site.types = await rest.fetch_types()
            except RestError as exc:
                warnings.append(str(exc))
            routes, type_warnings = RestClient.resolve_post_types([type], site.types)
            warnings.extend(type_warnings)
            if not routes:
                raise HTTPException(status_code=404, detail=f"Post type '{type}' does not exist on this site. Available: {', '.join(sorted(site.types)) or 'unknown'}.")
            _, route = routes[0]
            params: dict[str, Any] = {"status": "publish", "_embed": "author,wp:featuredmedia,wp:term", "orderby": orderby, "order": order, "per_page": per_page, "page": page}
            if modified_after:
                params["modified_after"] = modified_after
            if search:
                params["search"] = search
            try:
                status, data, headers = await rest.get(route, params)
            except Blocked as exc:
                raise HTTPException(status_code=403, detail=f"The site refuses automated access ({exc}).") from exc
            except FetchError as exc:
                raise HTTPException(status_code=502, detail=f"Upstream request failed ({exc}).") from exc
            if status == 400 and isinstance(data, dict) and data.get("code") == "rest_post_invalid_page_number":
                return {"site": site_summary(site), "source": "rest", "page": page, "perPage": per_page, "total": None, "totalPages": None, "items": [], "warnings": warnings}
            if status != 200 or not isinstance(data, list):
                msg = data.get("message") if isinstance(data, dict) else None
                raise HTTPException(status_code=502, detail=f"WordPress answered HTTP {status}{(': ' + str(msg)) if msg else ''}")
            items = []
            for raw in data:
                if not isinstance(raw, dict):
                    continue
                item = rest.map_post(raw, include_raw="raw" in chosen)
                if modified_after and item.modifiedAt and item.modifiedAt <= modified_after:
                    continue  # WordPress < 5.7 ignores modified_after
                items.append(item_payload(item, site, chosen))
            warnings.extend(rest.warnings)

            def _int(name: str) -> int | None:
                try:
                    return int(headers.get(name) or headers.get(name.lower()) or 0) or None
                except (TypeError, ValueError):
                    return None

            return {"site": site_summary(site), "source": "rest", "page": page, "perPage": per_page, "total": _int("X-WP-Total"), "totalPages": _int("X-WP-TotalPages"), "items": items, "warnings": warnings}

        # ---- fallback: no REST API ---------------------------------------------------------------------------------
        if per_page > MAX_PER_PAGE_FALLBACK:
            per_page = MAX_PER_PAGE_FALLBACK
            warnings.append(f"per_page capped at {MAX_PER_PAGE_FALLBACK} in HTML fallback mode (one fetch per item)")
        if search:
            warnings.append("search is not available in HTML fallback mode; ignored")
        lister = UrlLister(client, site)
        try:
            entries = await lister.list_urls(type_hints={type.lower()}, max_urls=page * per_page, modified_after=modified_after, ascending=(order == "asc"))
        except (Blocked, FetchError) as exc:
            raise HTTPException(status_code=502, detail=f"Could not list the site's URLs ({exc}).") from exc
        warnings.extend(lister.warnings)
        if not entries:
            raise HTTPException(status_code=404, detail="No WordPress REST API, sitemap or RSS feed was found at this URL. Is it a WordPress site? " + "; ".join(w for w in [site.rest_error, *lister.warnings] if w))
        chunk = entries[(page - 1) * per_page : page * per_page]
        items = []
        for entry in chunk:
            try:
                status, html, final_url = await client.get_text(entry.url)
            except (Blocked, FetchError) as exc:
                warnings.append(f"{entry.url}: {exc}")
                continue
            if status != 200:
                warnings.append(f"{entry.url}: HTTP {status}")
                continue
            item = extract_article(html, final_url, site, type_hint=entry.type_hint, feed_item=entry.feed_item, include_raw="raw" in chosen)
            if item is None:
                warnings.append(f"{entry.url}: no article content found")
                continue
            if item.modifiedAt is None and entry.lastmod:
                item.modifiedAt = entry.lastmod
            items.append(item_payload(item, site, chosen))
        return {"site": site_summary(site), "source": "html", "page": page, "perPage": per_page, "total": len(entries) if len(entries) < page * per_page else None, "totalPages": None, "items": items, "warnings": warnings}


async def _rest_collection(url: str, route: str, params: dict[str, Any], page: int, per_page: int, mapper) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    async with make_client() as client:
        site = await resolve_site(client, url)
        if not site.api_root:
            raise HTTPException(status_code=404, detail="This site has no reachable REST API; taxonomies, authors and media need it.")
        rest = RestClient(client, site)
        try:
            status, data, headers = await rest.get(route, {**params, "per_page": per_page, "page": page})
        except Blocked as exc:
            raise HTTPException(status_code=403, detail=f"The site refuses automated access ({exc}).") from exc
        except FetchError as exc:
            raise HTTPException(status_code=502, detail=f"Upstream request failed ({exc}).") from exc
        if status == 400 and isinstance(data, dict) and data.get("code") == "rest_post_invalid_page_number":
            data = []
        elif status != 200 or not isinstance(data, list):
            msg = data.get("message") if isinstance(data, dict) else None
            raise HTTPException(status_code=502, detail=f"WordPress answered HTTP {status}{(': ' + str(msg)) if msg else ''}")
        total = headers.get("X-WP-Total") or headers.get("x-wp-total")
        return {"site": site_summary(site), "page": page, "perPage": per_page, "total": int(total) if str(total).isdigit() else None, "items": [mapper(d, site) for d in data if isinstance(d, dict)]}


@app.get("/v1/wordpress/terms")
async def wordpress_terms(url: str = Query(...), kind: str = Query("category", pattern="^(category|tag)$"), page: int = Query(1, ge=1), per_page: int = Query(100, ge=1, le=MAX_PER_PAGE)) -> dict[str, Any]:
    """Categories or tags with counts, descriptions and links."""
    route = "wp/v2/categories" if kind == "category" else "wp/v2/tags"
    return await _rest_collection(url, route, {"_fields": "id,count,description,link,name,slug,taxonomy,parent"}, page, per_page, lambda d, s: RestClient.map_term(d, kind, s))


@app.get("/v1/wordpress/authors")
async def wordpress_authors(url: str = Query(...), page: int = Query(1, ge=1), per_page: int = Query(100, ge=1, le=MAX_PER_PAGE)) -> dict[str, Any]:
    """Public author profiles (users with published content)."""
    return await _rest_collection(url, "wp/v2/users", {"_fields": "id,name,slug,description,link,url,avatar_urls"}, page, per_page, RestClient.map_author)


@app.get("/v1/wordpress/media")
async def wordpress_media(url: str = Query(...), page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=MAX_PER_PAGE)) -> dict[str, Any]:
    """Media library metadata: source URLs, alt text, mime type, dimensions."""
    return await _rest_collection(url, "wp/v2/media", {"_fields": "id,date,modified,slug,link,title,alt_text,caption,mime_type,media_type,source_url,media_details,post,author"}, page, per_page, RestClient.map_media)
