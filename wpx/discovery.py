"""Find the WordPress REST API root for a site URL.

Order of discovery (one request each, stops at the first success):
1. `{site}/wp-json/?_fields=...`                 - pretty permalinks (the default on >95% of sites)
2. the site's HTML page: `<link rel="https://api.w.org/" href="...">` or the `Link` response header
3. `{site}/?rest_route=/&_fields=...`            - sites with plain permalinks
If the root answers 401/403/404 or is not JSON, the site is marked for the sitemap/RSS/HTML fallback.
"""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .convert import coalesce_str
from .models import Site
from .net import Blocked, FetchError, PoliteClient

log = logging.getLogger("wordpress-content-export.discovery")

ROOT_FIELDS = "name,description,url,home,gmt_offset,timezone_string,namespaces,site_logo,site_icon_url"
_LINK_HEADER_RE = re.compile(r"<([^>]+)>\s*;\s*rel=\"?https://api\.w\.org/\"?", re.I)
_GENERATOR_RE = re.compile(r"WordPress\s*([\d.]+)?", re.I)


def normalize_site_url(value: str) -> str:
    """'example.com/blog/' -> 'https://example.com/blog' (no query/fragment, no trailing slash)."""
    v = (value or "").strip()
    if not v:
        raise ValueError("Empty site URL")
    if not re.match(r"^https?://", v, re.I):
        v = "https://" + v
    parts = urlsplit(v)
    host = (parts.hostname or "").lower()
    if not host or "." not in host and host != "localhost":
        raise ValueError(f"'{value}' is not a valid site URL")
    path = re.sub(r"/+$", "", parts.path or "")
    # If the user pasted the API root or a feed URL, strip it back to the site.
    path = re.sub(r"/(wp-json|feed|wp-sitemap\.xml|sitemap[^/]*\.xml)(/.*)?$", "", path, flags=re.I)
    netloc = parts.netloc.lower()
    return urlunsplit((parts.scheme.lower(), netloc, path, "", ""))


def _same_site(a: str, b: str) -> bool:
    """True when two URLs differ only by scheme / 'www.' prefix (redirect to the canonical host)."""
    ha = (urlsplit(a).hostname or "").removeprefix("www.")
    hb = (urlsplit(b).hostname or "").removeprefix("www.")
    return ha == hb


def _rest_root_from_html(html: str, page_url: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    for link in soup.find_all("link", href=True):
        rels = [r.lower() for r in (link.get("rel") or [])]
        if "https://api.w.org/" in rels:
            return urljoin(page_url, link["href"])
    return None


def _html_lang_version_name(html: str) -> tuple[str | None, str | None, str | None]:
    """(html lang, WordPress version from the generator meta, site name from og:site_name)."""
    soup = BeautifulSoup(html, "html.parser")
    lang = None
    if soup.html is not None:
        lang = coalesce_str(soup.html.get("lang"), soup.html.get("xml:lang"))
    version = None
    gen = soup.find("meta", attrs={"name": re.compile("^generator$", re.I)})
    if gen and gen.get("content"):
        m = _GENERATOR_RE.search(gen["content"])
        if m:
            version = m.group(1) or "unknown"
    og = soup.find("meta", attrs={"property": "og:site_name"})
    name = coalesce_str(og.get("content") if og else None)
    return lang, version, name


def _is_wp_root(data: Any) -> bool:
    return isinstance(data, dict) and isinstance(data.get("namespaces"), list) and any(str(ns).startswith("wp/v2") for ns in data["namespaces"])


def _rest_style(api_root: str) -> str:
    return "rest_route" if "rest_route=" in api_root else "pretty"


def root_with_fields(api_root: str, fields: str = ROOT_FIELDS) -> str:
    sep = "&" if "?" in api_root else "?"
    return f"{api_root}{sep}_fields={fields}"


class Discoverer:
    def __init__(self, client: PoliteClient) -> None:
        self.client = client

    async def _probe_root(self, api_root: str) -> tuple[Any | None, str | None]:
        """Returns (root json, error). 401/403 -> error string (REST disabled), 404/HTML -> error string."""
        try:
            status, data, _ = await self.client.get_json(root_with_fields(api_root))
        except Blocked as exc:
            return None, f"REST root {api_root} blocked: {exc}"
        except FetchError as exc:
            return None, f"REST root {api_root} unreachable: {exc}"
        if status == 200 and _is_wp_root(data):
            return data, None
        if isinstance(data, dict) and data.get("code"):
            return None, f"REST root {api_root} answered {status} {data.get('code')}: {data.get('message')}"
        return None, f"REST root {api_root} answered HTTP {status} (not a WordPress REST index)"

    async def discover(self, input_url: str, *, mode: str = "auto") -> Site:
        base = normalize_site_url(input_url)
        site = Site(input_url=input_url, home_url=base)
        errors: list[str] = []

        # Always fetch the HTML page once: it tells us the language, the WordPress version (generator meta) and
        # the canonical REST root (link rel="https://api.w.org/"), and it is the only probe that works when the
        # site is served from a sub-directory with a non-standard REST prefix.
        html_root: str | None = None
        try:
            status, html, final_url = await self.client.get_text(base + "/")
            if status == 200 and html:
                html_root = _rest_root_from_html(html, final_url)
                site.language, site.wp_version, site.name = _html_lang_version_name(html)  # name is overwritten by the REST index when available
                final_base = normalize_site_url(final_url)
                if final_base != base and _same_site(base, final_base):
                    site.home_url = final_base  # follow http->https / www redirects for the canonical URL
            elif status != 200:
                errors.append(f"homepage answered HTTP {status}")
        except Blocked as exc:
            errors.append(f"homepage blocked: {exc}")
            if "robots.txt" in str(exc):
                site.source = "blocked"
                site.rest_error = "; ".join(errors)
                return site
        except FetchError as exc:
            errors.append(f"homepage unreachable: {exc}")

        if mode == "html":
            site.source = "html"
            site.rest_error = "mode=html requested"
            return site

        candidates: list[str] = []
        if html_root:
            candidates.append(html_root)
        for c in (site.home_url + "/wp-json/", site.home_url + "/?rest_route=/"):
            if c not in candidates:
                candidates.append(c)

        for api_root in candidates:
            data, err = await self._probe_root(api_root)
            if data is not None:
                site.api_root = api_root
                site.rest_style = _rest_style(api_root)
                site.name = coalesce_str(data.get("name"))
                site.description = coalesce_str(data.get("description"))
                site.timezone = coalesce_str(data.get("timezone_string")) or (f"UTC{data.get('gmt_offset'):+}" if isinstance(data.get("gmt_offset"), (int, float)) else None)
                site.namespaces = [str(ns) for ns in data.get("namespaces") or []]
                home = coalesce_str(data.get("home"), data.get("url"))
                if home:
                    site.home_url = normalize_site_url(home)
                site.source = "rest"
                log.info("REST API found for %s at %s (%s)", site.home_url, api_root, site.name)
                return site
            errors.append(err or "unknown error")
            # A 401/403 from the first candidate means the API is intentionally disabled; do not keep probing.
            if err and ("401" in err or "403" in err or "rest_" in err):
                break

        if mode == "rest":
            site.source = "error"
            site.rest_error = "REST API not available and mode=rest: " + "; ".join(errors)
            return site
        site.source = "html"
        site.rest_error = "; ".join(errors)
        log.warning("No usable REST API for %s - falling back to sitemap/RSS/HTML (%s)", site.home_url, site.rest_error)
        return site
