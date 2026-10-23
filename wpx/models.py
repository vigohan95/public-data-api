"""Output models. Field names are the public dataset schema - keep them stable."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .convert import html_to_markdown, html_to_text, reading_time_minutes, word_count

CONTENT_TYPES_DEFAULT: tuple[str, ...] = ("post", "page")
# Types that exist in /wp/v2/types but are never useful as content exports when "all" is requested.
NON_CONTENT_TYPES: frozenset[str] = frozenset(
    {"attachment", "nav_menu_item", "wp_block", "wp_template", "wp_template_part", "wp_navigation", "wp_font_family",
     "wp_font_face", "wp_global_styles", "revision", "customize_changeset", "oembed_cache", "user_request"}
)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class Site:
    """A resolved WordPress site - the unit of work."""

    input_url: str
    home_url: str  # canonical site URL (what WordPress calls `home`), no trailing slash
    api_root: str | None = None  # e.g. https://example.com/wp-json/ or https://example.com/?rest_route=/
    rest_style: str | None = None  # "pretty" | "rest_route"
    name: str | None = None
    description: str | None = None
    language: str | None = None
    timezone: str | None = None
    wp_version: str | None = None  # from the generator meta tag when the theme emits it
    source: str = "rest"  # "rest" | "html" | "blocked" | "error"
    rest_error: str | None = None  # why REST was not used (fallback mode)
    namespaces: list[str] = field(default_factory=list)
    types: dict[str, dict[str, Any]] = field(default_factory=dict)  # type slug -> {rest_base, name, ...}

    @property
    def key(self) -> str:
        return self.home_url.lower().replace("https://", "").replace("http://", "").rstrip("/")

    def to_dict(self) -> dict[str, Any]:
        return {
            "inputUrl": self.input_url,
            "homeUrl": self.home_url,
            "apiRoot": self.api_root,
            "name": self.name,
            "description": self.description,
            "language": self.language,
            "timezone": self.timezone,
            "wpVersion": self.wp_version,
            "source": self.source,
            "restError": self.rest_error,
            "namespaces": self.namespaces,
            "types": sorted(self.types),
        }


@dataclass(slots=True)
class Term:
    id: int | None
    name: str | None
    slug: str | None
    taxonomy: str | None = None
    url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "slug": self.slug, "taxonomy": self.taxonomy, "url": self.url}


@dataclass(slots=True)
class ContentItem:
    """One post / page / custom-type entry. `type` is the WordPress post type (post, page, product, ...)."""

    id: int | str | None
    type: str
    siteUrl: str
    url: str | None
    slug: str | None = None
    title: str | None = None
    status: str | None = None
    excerptHtml: str | None = None
    excerptText: str | None = None
    contentHtml: str | None = None
    contentMarkdown: str | None = None
    contentText: str | None = None
    wordCount: int = 0
    readingTimeMinutes: int = 0
    authorId: int | None = None
    authorName: str | None = None
    authorSlug: str | None = None
    authorUrl: str | None = None
    categories: list[dict[str, Any]] = field(default_factory=list)
    tags: list[dict[str, Any]] = field(default_factory=list)
    otherTerms: list[dict[str, Any]] = field(default_factory=list)  # custom taxonomies from _embed wp:term
    categoryNames: list[str] = field(default_factory=list)
    tagNames: list[str] = field(default_factory=list)
    featuredImageId: int | None = None
    featuredImageUrl: str | None = None
    featuredImageAlt: str | None = None
    publishedAt: str | None = None
    modifiedAt: str | None = None
    language: str | None = None
    format: str | None = None
    parentId: int | None = None
    menuOrder: int | None = None
    sticky: bool | None = None
    commentStatus: str | None = None
    commentCount: int | None = None
    siteName: str | None = None
    source: str = "rest"  # rest | html
    scrapedAt: str | None = None
    raw: dict[str, Any] | None = None

    def derive_content_fields(self, *, markdown: bool, text: bool, base_url: str | None = None) -> None:
        """Fill Markdown / plain text / counts from contentHtml (and excerptText from excerptHtml)."""
        if self.excerptHtml and not self.excerptText:
            self.excerptText = html_to_text(self.excerptHtml)
        plain = html_to_text(self.contentHtml) if self.contentHtml else None
        if markdown and self.contentHtml and not self.contentMarkdown:
            self.contentMarkdown = html_to_markdown(self.contentHtml, base_url=base_url)
        if text and not self.contentText:
            self.contentText = plain
        self.wordCount = word_count(plain)
        self.readingTimeMinutes = reading_time_minutes(self.wordCount)
        self.categoryNames = [c["name"] for c in self.categories if c.get("name")]
        self.tagNames = [t["name"] for t in self.tags if t.get("name")]

    def to_dict(self, *, include_html: bool = True, include_markdown: bool = True, include_text: bool = True, include_raw: bool = False) -> dict[str, Any]:
        d = asdict(self)
        if not include_html:
            d["contentHtml"] = None
        if not include_markdown:
            d["contentMarkdown"] = None
        if not include_text:
            d["contentText"] = None
        if not include_raw:
            d.pop("raw", None)
        return d
