"""Fallback when the REST API is disabled or blocked: list URLs from sitemaps / RSS, then extract article content
from the HTML of each page using WordPress theme conventions (`.entry-content`, `rel="category tag"`, shortlinks,
body classes, Open Graph / JSON-LD metadata). Heuristic by nature - see README "Limitations".
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from .convert import clean_html, coalesce_str, decode_entities, html_to_text, iso_utc, strip_shortcodes, word_count
from .models import ContentItem, Site, now_iso
from .net import Blocked, FetchError, PoliteClient

log = logging.getLogger("wordpress-content-export.fallback")

MAX_SITEMAP_FILES = 60  # safety cap on nested sitemap files per site
_SITEMAP_SKIP_RE = re.compile(r"(category|tag|author|user|taxonom|image|video|attachment|product_cat|product_tag|local|news)[-_]?sitemap|sitemap[-_]?(category|tag|author|users?|image|video|misc)|wp-sitemap-(taxonomies|users)", re.I)
_SITEMAP_TYPE_RE = re.compile(r"wp-sitemap-posts-([a-z0-9_-]+)-\d+\.xml|/([a-z0-9_-]+?)-sitemap\d*\.xml", re.I)
_SHORTLINK_ID_RE = re.compile(r"[?&]p=(\d+)")
_BODY_ID_RE = re.compile(r"\b(?:postid|page-id)-(\d+)\b")
_BODY_TYPE_RE = re.compile(r"\bsingle-([a-z0-9_-]+)\b")
_WHITESPACE_RE = re.compile(r"\s+")
_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

CONTENT_SELECTORS = [
    ".entry-content",
    "[itemprop='articleBody']",
    ".post-content",
    ".article-content",
    ".post-entry",
    ".td-post-content",
    ".single-content",
    ".post__content",
    ".article__body",
    ".page-content",
    "article .content",
    "article",
    "main",
]
NOISE_SELECTORS = [
    "nav", "aside", "footer", "header", "form", ".sharedaddy", ".jp-relatedposts", ".post-navigation", ".navigation",
    ".comments-area", "#comments", ".comment-respond", ".addtoany_share_save_container", ".related-posts", ".yarpp",
    ".wp-block-post-navigation-link", ".social-share", ".sharing", ".author-box", ".entry-footer", ".entry-meta",
    ".breadcrumbs", ".advert", ".ad", ".adsbygoogle", ".newsletter", ".subscribe", ".cookie",
]


@dataclass(slots=True)
class UrlEntry:
    url: str
    lastmod: str | None = None
    type_hint: str | None = None  # from the sitemap file name (post / page / product ...) when available
    feed_item: dict[str, Any] | None = None  # RSS payload when the URL came from a feed


# ----- sitemap / RSS discovery ----------------------------------------------------------------------------------------


def _parse_xml(text: str) -> Any:
    try:
        import xml.etree.ElementTree as ET  # noqa: PLC0415 - local import keeps module import cheap

        return ET.fromstring(text.strip().encode("utf-8") if isinstance(text, str) else text)
    except Exception:  # noqa: BLE001 - malformed XML
        return None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_sitemap(text: str) -> tuple[list[tuple[str, str | None]], list[tuple[str, str | None]]]:
    """Returns (child sitemaps, urls) as (loc, lastmod) tuples."""
    root = _parse_xml(text)
    if root is None:
        return [], []
    kind = _local(root.tag)
    children: list[tuple[str, str | None]] = []
    urls: list[tuple[str, str | None]] = []
    for el in root:
        name = _local(el.tag)
        if name not in ("sitemap", "url"):
            continue
        loc = lastmod = None
        for sub in el:
            sn = _local(sub.tag)
            if sn == "loc":
                loc = (sub.text or "").strip()
            elif sn == "lastmod":
                lastmod = (sub.text or "").strip() or None
        if not loc:
            continue
        (children if kind == "sitemapindex" or name == "sitemap" else urls).append((loc, lastmod))
    return children, urls


def type_hint_from_sitemap_url(url: str) -> str | None:
    m = _SITEMAP_TYPE_RE.search(url)
    if not m:
        return None
    hint = (m.group(1) or m.group(2) or "").lower()
    if hint in ("post", "posts"):
        return "post"
    if hint in ("page", "pages"):
        return "page"
    return hint or None


def parse_rss(text: str) -> list[dict[str, Any]]:
    """RSS 2.0 (what /feed/ returns) -> list of dicts with link/title/pubDate/creator/categories/content/description."""
    root = _parse_xml(text)
    if root is None:
        return []
    items: list[dict[str, Any]] = []
    for item in root.iter():
        if _local(item.tag) != "item":
            continue
        rec: dict[str, Any] = {"categories": []}
        for sub in item:
            name = _local(sub.tag)
            txt = (sub.text or "").strip()
            if name == "link":
                rec["link"] = txt
            elif name == "title":
                rec["title"] = txt
            elif name == "pubdate":
                rec["pubDate"] = txt
            elif name == "creator":
                rec["creator"] = txt
            elif name == "category":
                rec["categories"].append(txt)
            elif name == "encoded":
                rec["content"] = sub.text or ""
            elif name == "description":
                rec["description"] = sub.text or ""
            elif name == "guid":
                rec["guid"] = txt
        if rec.get("link"):
            items.append(rec)
    return items


class UrlLister:
    def __init__(self, client: PoliteClient, site: Site) -> None:
        self.client = client
        self.site = site
        self.warnings: list[str] = []
        self.sitemaps_read = 0

    async def _fetch_xml(self, url: str) -> str | None:
        try:
            status, text, _ = await self.client.get_text(url, accept="application/xml,text/xml;q=0.9,*/*;q=0.5")
        except Blocked as exc:
            self.warnings.append(f"{url}: {exc}")
            return None
        except FetchError as exc:
            self.warnings.append(f"{url}: {exc}")
            return None
        if status != 200 or "<" not in text[:200]:
            return None
        return text

    async def from_sitemaps(self, *, type_hints: set[str] | None = None, max_urls: int = 0) -> list[UrlEntry]:
        home = self.site.home_url
        candidates = await self.client.robots.sitemap_urls(f"{urlsplit(home).scheme}://{urlsplit(home).netloc}", self.client._fetch_text_unchecked)
        # keep only sitemaps of this site (robots.txt of a multi-site host may list others)
        candidates = [c for c in candidates if c.lower().startswith(home.lower())] or candidates
        for c in (f"{home}/wp-sitemap.xml", f"{home}/sitemap_index.xml", f"{home}/sitemap.xml", f"{home}/post-sitemap.xml", f"{home}/page-sitemap.xml"):
            if c not in candidates:
                candidates.append(c)
        seen_files: set[str] = set()
        entries: dict[str, UrlEntry] = {}
        for root_candidate in candidates:
            queue = [root_candidate]
            found_any = False
            while queue and self.sitemaps_read < MAX_SITEMAP_FILES:
                url = queue.pop(0)
                if url in seen_files or _SITEMAP_SKIP_RE.search(url):
                    continue
                seen_files.add(url)
                text = await self._fetch_xml(url)
                self.sitemaps_read += 1
                if text is None:
                    continue
                children, urls = parse_sitemap(text)
                if not children and not urls:
                    continue
                found_any = True
                hint = type_hint_from_sitemap_url(url)
                if hint and type_hints and hint not in type_hints:
                    continue
                for loc, _lm in children:
                    child_hint = type_hint_from_sitemap_url(loc)
                    if child_hint and type_hints and child_hint not in type_hints:
                        continue
                    queue.append(loc)
                for loc, lastmod in urls:
                    if loc.rstrip("/") == home.rstrip("/"):
                        continue  # homepage
                    if loc not in entries:
                        entries[loc] = UrlEntry(url=loc, lastmod=iso_utc(lastmod), type_hint=hint)
                if max_urls and len(entries) >= max_urls * 3:
                    break  # enough candidates; sorting by lastmod happens later
            if found_any:
                break  # one sitemap tree is enough; do not probe the other conventional locations
        return list(entries.values())

    async def from_rss(self) -> list[UrlEntry]:
        text = await self._fetch_xml(f"{self.site.home_url}/feed/")
        if text is None:
            text = await self._fetch_xml(f"{self.site.home_url}/?feed=rss2")
        if text is None:
            return []
        out: list[UrlEntry] = []
        for rec in parse_rss(text):
            out.append(UrlEntry(url=rec["link"], lastmod=iso_utc(rec.get("pubDate")), type_hint="post", feed_item=rec))
        return out

    async def list_urls(self, *, type_hints: set[str] | None, max_urls: int, modified_after: str | None, ascending: bool = False) -> list[UrlEntry]:
        """URLs newest-first by default; `ascending=True` (sync runs) returns oldest-modified first so a capped run
        can safely advance the sync cursor."""
        entries = await self.from_sitemaps(type_hints=type_hints, max_urls=max_urls)
        source = "sitemap"
        if not entries:
            entries = await self.from_rss()
            source = "rss"
            if not entries:
                self.warnings.append("no sitemap or RSS feed found")
                return []
        if modified_after:
            kept = [e for e in entries if e.lastmod is None or e.lastmod > modified_after]
            if source == "sitemap":
                entries = kept
        entries.sort(key=lambda e: e.lastmod or ("" if ascending else "9"), reverse=not ascending)  # unknown lastmod sorts last
        if max_urls:
            entries = entries[:max_urls]
        log.info("[%s] %d URLs listed from %s", self.site.home_url, len(entries), source)
        return entries


# ----- article extraction ---------------------------------------------------------------------------------------------


def _meta(soup: BeautifulSoup, **attrs: str) -> str | None:
    tag = soup.find("meta", attrs=attrs)
    if tag and tag.get("content"):
        return str(tag["content"]).strip() or None
    return None


def _jsonld_article(soup: BeautifulSoup) -> dict[str, Any]:
    wanted = {"article", "blogposting", "newsarticle", "techarticle", "webpage", "report", "scholarlyarticle"}
    for script in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (ValueError, TypeError):
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                t = node.get("@type")
                types = {str(x).lower() for x in (t if isinstance(t, list) else [t]) if x}
                if types & wanted:
                    return node
                if "@graph" in node:
                    stack.append(node["@graph"])
    return {}


def _text_len(tag: Tag) -> int:
    return len(_WHITESPACE_RE.sub(" ", tag.get_text(" ")).strip())


def _link_density(tag: Tag) -> float:
    total = _text_len(tag) or 1
    links = sum(_text_len(a) for a in tag.find_all("a"))
    return links / total


def pick_content_node(soup: BeautifulSoup) -> Tag | None:
    """First matching WordPress content container with real text; otherwise the densest text block."""
    for sel in CONTENT_SELECTORS:
        for node in soup.select(sel):
            if _text_len(node) >= 200 and _link_density(node) < 0.5:
                return node
    best: Tag | None = None
    best_score = 0
    for node in soup.find_all(["div", "section", "article"]):
        if node.find(["div", "section", "article"], recursive=False) and len(node.find_all("p")) < 3:
            continue
        paragraphs = sum(_text_len(p) for p in node.find_all("p", recursive=False))
        if paragraphs <= 0:
            continue
        score = paragraphs * (1 - _link_density(node))
        if score > best_score:
            best, best_score = node, score
    return best


def extract_article(html: str, url: str, site: Site, *, type_hint: str | None = None, feed_item: dict[str, Any] | None = None, include_raw: bool = False) -> ContentItem | None:
    soup = BeautifulSoup(html, "html.parser")
    ld = _jsonld_article(soup)
    body = soup.body
    body_class = " ".join(body.get("class", [])) if body else ""

    # id and type from WordPress markers
    item_id: int | None = None
    shortlink = soup.find("link", rel=lambda r: r and "shortlink" in r)
    if shortlink and shortlink.get("href"):
        m = _SHORTLINK_ID_RE.search(shortlink["href"])
        if m:
            item_id = int(m.group(1))
    if item_id is None:
        m = _BODY_ID_RE.search(body_class)
        if m:
            item_id = int(m.group(1))
    post_type = type_hint
    m = _BODY_TYPE_RE.search(body_class)
    if m:
        post_type = m.group(1)
    elif re.search(r"\bpage(-id-\d+|\s|$)", body_class) and not re.search(r"\bsingle\b", body_class):
        post_type = "page"
    post_type = post_type or "post"

    # title: JSON-LD headline > article <h1> > og:title > any <h1> > <title>; strip a trailing " | Site name"
    h1 = soup.select_one("h1.entry-title, h1[itemprop='headline'], .entry-header h1, article h1, h1.post-title, h1.article-title")
    any_h1 = h1 or soup.find("h1")
    title = coalesce_str(
        ld.get("headline") if isinstance(ld.get("headline"), str) else None,
        h1.get_text(" ") if h1 else None,
        _meta(soup, property="og:title"),
        any_h1.get_text(" ") if any_h1 else None,
        soup.title.get_text(" ") if soup.title else None,
    )
    title = decode_entities(title)
    site_name = coalesce_str(site.name, _meta(soup, property="og:site_name"))
    if title and site_name:
        for sep in (" - ", " | ", " – ", " — ", " · "):
            if title.lower().endswith(sep + site_name.lower()):
                title = title[: -len(sep + site_name)].strip()
                break
    if feed_item and feed_item.get("title") and not (any_h1 or ld.get("headline")):
        title = decode_entities(feed_item["title"])

    # content container
    node = pick_content_node(soup)
    content_html: str | None = None
    if node is not None:
        for sel in NOISE_SELECTORS:
            for junk in node.select(sel):
                junk.decompose()
        for h in node.find_all("h1"):
            if title and decode_entities(h.get_text(" ")) == title:
                h.decompose()
        content_html = clean_html(strip_shortcodes(str(node)), base_url=url)
    if (not content_html or word_count(html_to_text(content_html)) < 20) and feed_item and feed_item.get("content"):
        content_html = clean_html(strip_shortcodes(feed_item["content"]), base_url=url)
    if not content_html and not title:
        return None

    # metadata
    published = coalesce_str(_meta(soup, property="article:published_time"), ld.get("datePublished") if isinstance(ld.get("datePublished"), str) else None)
    modified = coalesce_str(_meta(soup, property="article:modified_time"), _meta(soup, property="og:updated_time"), ld.get("dateModified") if isinstance(ld.get("dateModified"), str) else None)
    if not published:
        t = soup.select_one("time.entry-date[datetime], time[itemprop='datePublished'][datetime], time.published[datetime], .entry-date time[datetime], time[datetime]")
        published = t.get("datetime") if t else None
    if not modified:
        t = soup.select_one("time.updated[datetime], time[itemprop='dateModified'][datetime]")
        modified = t.get("datetime") if t else None
    if not published and feed_item:
        published = feed_item.get("pubDate")

    author_name = None
    author_url = None
    author_node = ld.get("author")
    if isinstance(author_node, list) and author_node:
        author_node = author_node[0]
    if isinstance(author_node, dict):
        author_name = coalesce_str(author_node.get("name"))
        author_url = coalesce_str(author_node.get("url"))
    if not author_name:
        a = soup.select_one(".author.vcard a, a[rel='author'], .entry-author a, .author-name a, .byline a, .post-author a")
        if a:
            author_name = coalesce_str(a.get_text(" "))
            author_url = urljoin(url, a.get("href")) if a.get("href") else None
    author_name = coalesce_str(author_name, _meta(soup, name="author"), feed_item.get("creator") if feed_item else None)
    author_slug = None
    if author_url:
        m = re.search(r"/author/([^/]+)/?", author_url)
        author_slug = m.group(1) if m else None

    categories: list[dict[str, Any]] = []
    tags: list[dict[str, Any]] = []
    seen_terms: set[tuple[str, str]] = set()
    for a in soup.find_all("a", href=True):
        rels = {r.lower() for r in (a.get("rel") or [])}
        if not rels & {"category", "tag"}:
            continue
        name = decode_entities(a.get_text(" "))
        if not name:
            continue
        href = urljoin(url, a["href"])
        kind = "category" if "category" in rels else "tag"
        if (kind, name) in seen_terms:
            continue
        seen_terms.add((kind, name))
        slug = (urlsplit(href).path.rstrip("/").rsplit("/", 1)[-1] or None)
        term = {"id": None, "name": name, "slug": slug, "taxonomy": "category" if kind == "category" else "post_tag", "url": href}
        (categories if kind == "category" else tags).append(term)
    if not categories:  # themes without rel="category" links: Open Graph article:section (Yoast, RankMath, Jetpack)
        section = decode_entities(_meta(soup, property="article:section"))
        if section:
            categories = [{"id": None, "name": section, "slug": None, "taxonomy": "category", "url": None}]
    if not tags:
        for meta in soup.find_all("meta", attrs={"property": "article:tag"}):
            name = decode_entities(meta.get("content"))
            if name and name not in [t["name"] for t in tags]:
                tags.append({"id": None, "name": name, "slug": None, "taxonomy": "post_tag", "url": None})
    if not categories and feed_item:
        categories = [{"id": None, "name": c, "slug": None, "taxonomy": "category", "url": None} for c in feed_item.get("categories") or [] if c]

    image = _meta(soup, property="og:image")
    image_alt = _meta(soup, property="og:image:alt")
    img_tag = soup.select_one("img.wp-post-image, .post-thumbnail img, figure.featured img")
    if img_tag is not None:
        image = image or img_tag.get("data-src") or img_tag.get("src")
        image_alt = image_alt or coalesce_str(img_tag.get("alt"))
    if image:
        image = urljoin(url, image)

    canonical = soup.find("link", rel=lambda r: r and "canonical" in r)
    final_url = canonical["href"] if canonical and canonical.get("href") else url
    slug = urlsplit(final_url).path.rstrip("/").rsplit("/", 1)[-1] or None
    lang = soup.html.get("lang") if soup.html is not None else None
    excerpt = coalesce_str(_meta(soup, property="og:description"), _meta(soup, name="description"), ld.get("description") if isinstance(ld.get("description"), str) else None)
    if not excerpt and feed_item and feed_item.get("description"):
        excerpt = html_to_text(feed_item["description"])

    item = ContentItem(
        id=item_id,
        type=post_type,
        siteUrl=site.home_url,
        siteName=site.name,
        url=final_url,
        slug=slug,
        title=title,
        status="publish",
        excerptHtml=None,
        excerptText=decode_entities(excerpt),
        contentHtml=content_html,
        authorName=decode_entities(author_name),
        authorSlug=author_slug,
        authorUrl=author_url,
        categories=categories,
        tags=tags,
        featuredImageUrl=image,
        featuredImageAlt=decode_entities(image_alt),
        publishedAt=iso_utc(published),
        modifiedAt=iso_utc(modified),
        language=coalesce_str(lang, site.language),
        source="html",
        scrapedAt=now_iso(),
        raw={"jsonLd": ld, "bodyClass": body_class, "feedItem": feed_item} if include_raw else None,
    )
    return item
