"""WordPress REST API (wp/v2) client: pagination, post-type discovery, item mapping."""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from .convert import clean_html, coalesce_str, decode_entities, iso_utc, strip_shortcodes
from .models import NON_CONTENT_TYPES, ContentItem, Site, Term, now_iso
from .net import Blocked, FetchError, PoliteClient

log = logging.getLogger("wordpress-content-export.rest")

PER_PAGE = 100
EMBED = "author,wp:featuredmedia,wp:term"
POST_FIELDS = (
    "id,date,date_gmt,modified,modified_gmt,slug,status,type,link,title,content,excerpt,author,featured_media,"
    "comment_status,sticky,template,format,categories,tags,parent,menu_order,lang,language,wpml_current_locale,"
    "yoast_head_json,_links,_embedded"
)


class RestError(Exception):
    pass


class RestClient:
    def __init__(self, client: PoliteClient, site: Site) -> None:
        if not site.api_root:
            raise ValueError("site has no api_root")
        self.client = client
        self.site = site
        self.warnings: list[str] = []

    # ----- URL building -----------------------------------------------------------------------------------------

    def url(self, route: str) -> str:
        """`wp/v2/posts` -> https://x/wp-json/wp/v2/posts  or  https://x/?rest_route=/wp/v2/posts"""
        route = route.strip("/")
        root = self.site.api_root or ""
        if self.site.rest_style == "rest_route":
            return root + route  # root already ends with 'rest_route=/'
        return root.rstrip("/") + "/" + route

    async def get(self, route: str, params: dict[str, Any] | None = None) -> tuple[int, Any, Any]:
        return await self.client.get_json(self.url(route), params=params)

    # ----- pagination -------------------------------------------------------------------------------------------

    async def iterate(self, route: str, params: dict[str, Any] | None = None, *, max_items: int = 0) -> AsyncIterator[dict[str, Any]]:
        """Yield every item of a collection, following `page` up to X-WP-TotalPages. Stops cleanly on the
        `rest_post_invalid_page_number` 400 that WordPress returns past the last page. Lowers per_page when the
        site caps it (some security plugins reject 100)."""
        per_page = PER_PAGE
        page = 1
        yielded = 0
        total_pages: int | None = None
        while True:
            q = {**(params or {}), "per_page": per_page, "page": page}
            try:
                status, data, headers = await self.get(route, q)
            except Blocked as exc:
                raise RestError(f"{route} blocked ({exc})") from exc
            except FetchError as exc:
                raise RestError(f"{route} failed ({exc})") from exc
            if status == 400 and isinstance(data, dict):
                code = str(data.get("code") or "")
                if code == "rest_post_invalid_page_number":
                    return
                if code == "rest_invalid_param" and per_page > 10 and "per_page" in str(data.get("data", {}).get("params", {})):
                    per_page = 10 if per_page <= 20 else 20
                    self.warnings.append(f"{route}: site rejects per_page={PER_PAGE}, using {per_page}")
                    continue
                raise RestError(f"{route} answered 400 {code}: {data.get('message')}")
            if status != 200 or not isinstance(data, list):
                msg = data.get("message") if isinstance(data, dict) else None
                raise RestError(f"{route} answered HTTP {status}{(': ' + str(msg)) if msg else ''}")
            if total_pages is None:
                try:
                    total_pages = int(headers.get("X-WP-TotalPages") or headers.get("x-wp-totalpages") or 0) or None
                except ValueError:
                    total_pages = None
            for item in data:
                if isinstance(item, dict):
                    yield item
                    yielded += 1
                    if max_items and yielded >= max_items:
                        return
            if not data or len(data) < per_page or (total_pages is not None and page >= total_pages):
                return
            page += 1

    # ----- discovery helpers ------------------------------------------------------------------------------------

    async def fetch_types(self) -> dict[str, dict[str, Any]]:
        """Public post types and their REST routes (`rest_base`)."""
        try:
            # NB: no `_fields` here - on /types it filters the top-level keys (post, page, ...) and returns {}.
            status, data, _ = await self.get("wp/v2/types")
        except (Blocked, FetchError) as exc:
            self.warnings.append(f"wp/v2/types unavailable ({exc}); assuming post + page")
            return {"post": {"rest_base": "posts", "name": "Posts"}, "page": {"rest_base": "pages", "name": "Pages"}}
        if status != 200 or not isinstance(data, dict) or not data:
            self.warnings.append(f"wp/v2/types answered HTTP {status}; assuming post + page")
            return {"post": {"rest_base": "posts", "name": "Posts"}, "page": {"rest_base": "pages", "name": "Pages"}}
        types: dict[str, dict[str, Any]] = {}
        for slug, info in data.items():
            if not isinstance(info, dict):
                continue
            types[str(slug)] = {
                "rest_base": info.get("rest_base") or slug,
                "rest_namespace": info.get("rest_namespace") or "wp/v2",
                "name": info.get("name"),
                "taxonomies": info.get("taxonomies") or [],
            }
        return types

    @staticmethod
    def resolve_post_types(requested: list[str], available: dict[str, dict[str, Any]]) -> tuple[list[tuple[str, str]], list[str]]:
        """Map requested type names (or 'all') to (type slug, route). Returns (routes, warnings)."""
        warnings: list[str] = []
        wanted: list[str] = []
        if any(r.lower() == "all" for r in requested):
            wanted = [t for t in available if t not in NON_CONTENT_TYPES]
        else:
            for r in requested:
                r = r.strip()
                if not r:
                    continue
                if r in available:
                    wanted.append(r)
                else:
                    # accept rest_base names too ("posts", "pages", "products")
                    match = next((slug for slug, info in available.items() if info.get("rest_base") == r), None)
                    if match:
                        wanted.append(match)
                    else:
                        warnings.append(f"post type '{r}' is not exposed by this site's REST API; skipped")
        routes: list[tuple[str, str]] = []
        for slug in dict.fromkeys(wanted):
            info = available[slug]
            routes.append((slug, f"{info.get('rest_namespace', 'wp/v2')}/{info['rest_base']}"))
        return routes, warnings

    # ----- item mapping -----------------------------------------------------------------------------------------

    def map_post(self, data: dict[str, Any], *, include_raw: bool) -> ContentItem:
        site = self.site
        embedded = data.get("_embedded") if isinstance(data.get("_embedded"), dict) else {}
        author = _first(embedded.get("author"))
        media = _first(embedded.get("wp:featuredmedia"))
        categories: list[dict[str, Any]] = []
        tags: list[dict[str, Any]] = []
        others: list[dict[str, Any]] = []
        for group in embedded.get("wp:term") or []:
            if not isinstance(group, list):
                continue
            for term in group:
                if not isinstance(term, dict) or term.get("code"):
                    continue
                t = Term(id=term.get("id"), name=decode_entities(term.get("name")), slug=term.get("slug"), taxonomy=term.get("taxonomy"), url=term.get("link")).to_dict()
                if term.get("taxonomy") == "category":
                    categories.append(t)
                elif term.get("taxonomy") == "post_tag":
                    tags.append(t)
                else:
                    others.append(t)
        if not categories and isinstance(data.get("categories"), list):
            categories = [{"id": c, "name": None, "slug": None, "taxonomy": "category", "url": None} for c in data["categories"]]
        if not tags and isinstance(data.get("tags"), list):
            tags = [{"id": c, "name": None, "slug": None, "taxonomy": "post_tag", "url": None} for c in data["tags"]]

        content = data.get("content") if isinstance(data.get("content"), dict) else {}
        excerpt = data.get("excerpt") if isinstance(data.get("excerpt"), dict) else {}
        title = data.get("title") if isinstance(data.get("title"), dict) else {"rendered": data.get("title")}
        protected = bool(content.get("protected"))
        content_html = None if protected else clean_html(strip_shortcodes(content.get("rendered")), base_url=data.get("link") or site.home_url)
        excerpt_html = None if protected else clean_html(strip_shortcodes(excerpt.get("rendered")), base_url=site.home_url)
        yoast = data.get("yoast_head_json") if isinstance(data.get("yoast_head_json"), dict) else {}

        item = ContentItem(
            id=data.get("id"),
            type=str(data.get("type") or "post"),
            siteUrl=site.home_url,
            siteName=site.name,
            url=data.get("link"),
            slug=data.get("slug"),
            title=decode_entities(title.get("rendered")),
            status=data.get("status"),
            excerptHtml=excerpt_html,
            contentHtml=content_html,
            authorId=data.get("author"),
            authorName=decode_entities(author.get("name")) if author and not author.get("code") else None,
            authorSlug=author.get("slug") if author and not author.get("code") else None,
            authorUrl=author.get("link") if author and not author.get("code") else None,
            categories=categories,
            tags=tags,
            otherTerms=others,
            featuredImageId=data.get("featured_media") or None,
            featuredImageUrl=(media.get("source_url") if media and not media.get("code") else None),
            featuredImageAlt=(coalesce_str(media.get("alt_text")) if media and not media.get("code") else None),
            publishedAt=iso_utc(data.get("date_gmt")) or iso_utc(data.get("date")),
            modifiedAt=iso_utc(data.get("modified_gmt")) or iso_utc(data.get("modified")),
            language=coalesce_str(data.get("lang"), data.get("language"), data.get("wpml_current_locale"), site.language),
            format=data.get("format"),
            parentId=data.get("parent") or None,
            menuOrder=data.get("menu_order"),
            sticky=data.get("sticky"),
            commentStatus=data.get("comment_status"),
            source="rest",
            scrapedAt=now_iso(),
            raw=data if include_raw else None,
        )
        if protected:
            item.excerptText = "[password protected - content not exported]"
        if yoast:
            item_seo = {"title": yoast.get("title"), "description": yoast.get("description"), "canonical": yoast.get("canonical")}
            if item.raw is not None:
                item.raw.setdefault("_seo", item_seo)
        return item

    @staticmethod
    def map_term(data: dict[str, Any], kind: str, site: Site) -> dict[str, Any]:
        return {
            "type": kind,  # category | tag
            "id": data.get("id"),
            "siteUrl": site.home_url,
            "siteName": site.name,
            "url": data.get("link"),
            "slug": data.get("slug"),
            "title": decode_entities(data.get("name")),
            "name": decode_entities(data.get("name")),
            "description": decode_entities(data.get("description")) or None,
            "taxonomy": data.get("taxonomy"),
            "count": data.get("count"),
            "parentId": data.get("parent") or None,
            "source": "rest",
            "scrapedAt": now_iso(),
        }

    @staticmethod
    def map_author(data: dict[str, Any], site: Site) -> dict[str, Any]:
        avatars = data.get("avatar_urls") if isinstance(data.get("avatar_urls"), dict) else {}
        return {
            "type": "author",
            "id": data.get("id"),
            "siteUrl": site.home_url,
            "siteName": site.name,
            "url": data.get("link"),
            "slug": data.get("slug"),
            "title": decode_entities(data.get("name")),
            "name": decode_entities(data.get("name")),
            "description": decode_entities(data.get("description")) or None,
            "website": coalesce_str(data.get("url")),
            "avatarUrl": avatars.get("96") or next(iter(avatars.values()), None),
            "source": "rest",
            "scrapedAt": now_iso(),
        }

    @staticmethod
    def map_media(data: dict[str, Any], site: Site) -> dict[str, Any]:
        details = data.get("media_details") if isinstance(data.get("media_details"), dict) else {}
        title = data.get("title") if isinstance(data.get("title"), dict) else {"rendered": data.get("title")}
        caption = data.get("caption") if isinstance(data.get("caption"), dict) else {}
        return {
            "type": "media",
            "id": data.get("id"),
            "siteUrl": site.home_url,
            "siteName": site.name,
            "url": data.get("source_url"),
            "pageUrl": data.get("link"),
            "slug": data.get("slug"),
            "title": decode_entities(title.get("rendered")),
            "altText": coalesce_str(data.get("alt_text")),
            "caption": decode_entities(caption.get("rendered")) or None,
            "mediaType": data.get("media_type"),
            "mimeType": data.get("mime_type"),
            "width": details.get("width"),
            "height": details.get("height"),
            "fileSize": details.get("filesize"),
            "attachedToPostId": data.get("post") or None,
            "authorId": data.get("author"),
            "publishedAt": iso_utc(data.get("date_gmt")) or iso_utc(data.get("date")),
            "modifiedAt": iso_utc(data.get("modified_gmt")) or iso_utc(data.get("modified")),
            "source": "rest",
            "scrapedAt": now_iso(),
        }


def _first(value: Any) -> dict[str, Any] | None:
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    return None
