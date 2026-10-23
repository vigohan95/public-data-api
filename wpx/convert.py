"""HTML -> Markdown / plain-text conversion and small text helpers."""
from __future__ import annotations

import html as html_lib
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from markdownify import MarkdownConverter

_WS_RE = re.compile(r"[ \t ]+")
_NL_RE = re.compile(r"\n{3,}")
_WORD_RE = re.compile(r"[\w'’-]+", re.UNICODE)
_CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")
_BLOCK_TAGS = [
    "p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "table", "section", "article",
    "header", "footer", "blockquote", "pre", "dd", "dt", "figure", "figcaption",
]
_NOISE_TAGS = ["script", "style", "noscript", "template", "iframe", "svg", "form", "button"]
_WP_BLOCK_COMMENT_RE = re.compile(r"<!--\s*/?wp:[^>]*-->")
_SHORTCODE_RE = re.compile(r"\[/?[a-zA-Z_][\w-]*(?:\s[^\]]*)?\]")


class _Converter(MarkdownConverter):
    """markdownify with ATX headings, '-' bullets and absolute links/images."""

    def __init__(self, base_url: str | None, **kwargs: Any) -> None:
        super().__init__(heading_style="ATX", bullets="-", **kwargs)
        self.base_url = base_url

    def convert_a(self, el: Any, text: str, *args: Any, **kwargs: Any) -> str:
        if self.base_url and el.get("href"):
            el["href"] = urljoin(self.base_url, el["href"])
        return super().convert_a(el, text, *args, **kwargs)

    def convert_img(self, el: Any, text: str, *args: Any, **kwargs: Any) -> str:
        src = el.get("data-src") or el.get("src") or ""
        if src.startswith("data:"):
            return ""
        if self.base_url and src:
            el["src"] = urljoin(self.base_url, src)
        return super().convert_img(el, text, *args, **kwargs)


def clean_html(value: str | None, *, base_url: str | None = None) -> str | None:
    """Drop scripts/styles/forms, resolve relative URLs, strip WP block comments. Keeps semantic HTML."""
    if not value:
        return None
    value = _WP_BLOCK_COMMENT_RE.sub("", value)
    soup = BeautifulSoup(value, "html.parser")
    for tag in soup(_NOISE_TAGS):
        tag.decompose()
    for tag in soup.find_all(True):
        if tag.name in ("a", "area") and tag.get("href") and base_url:
            tag["href"] = urljoin(base_url, tag["href"])
        if tag.name == "img":
            src = tag.get("data-src") or tag.get("src")
            if src and base_url:
                tag["src"] = urljoin(base_url, src)
        for attr in ("onclick", "onload", "style", "data-wp-interactive"):
            if attr in tag.attrs:
                del tag[attr]
    out = str(soup).strip()
    return out or None


def html_to_markdown(value: str | None, *, base_url: str | None = None) -> str | None:
    if not value:
        return None
    soup = BeautifulSoup(_WP_BLOCK_COMMENT_RE.sub("", value), "html.parser")
    for tag in soup(_NOISE_TAGS):
        tag.decompose()
    md = _Converter(base_url).convert_soup(soup)
    md = "\n".join(line.rstrip() for line in md.splitlines())
    md = _NL_RE.sub("\n\n", md).strip()
    return md or None


def html_to_text(value: str | None) -> str | None:
    if not value:
        return None
    soup = BeautifulSoup(_WP_BLOCK_COMMENT_RE.sub("", value), "html.parser")
    for tag in soup(_NOISE_TAGS):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for tag in soup.find_all(_BLOCK_TAGS):  # block elements end lines; inline elements (strong, a, span) do not
        tag.insert_after("\n")
    text = soup.get_text("")
    text = html_lib.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    text = _NL_RE.sub("\n\n", text).strip()
    return text or None


def strip_shortcodes(value: str | None) -> str | None:
    """Remove leftover [shortcode] tags (REST `rendered` fields are normally expanded; feeds/HTML may not be)."""
    if not value:
        return value
    return _SHORTCODE_RE.sub("", value)


def word_count(text: str | None) -> int:
    """Words for alphabetic scripts plus one per CJK character (approximation used by most readers)."""
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    latin = len(_WORD_RE.findall(_CJK_RE.sub(" ", text)))
    return latin + cjk


def reading_time_minutes(words: int, wpm: int = 238) -> int:
    if words <= 0:
        return 0
    return max(1, round(words / wpm))


def decode_entities(value: str | None) -> str | None:
    """WordPress `title.rendered` keeps entities like &#8217; - decode to plain Unicode text."""
    if value is None:
        return None
    text = html_lib.unescape(value)
    if "<" in text:  # some themes wrap titles in markup
        text = BeautifulSoup(text, "html.parser").get_text(" ")
    text = _WS_RE.sub(" ", text).strip()
    return text or None


def iso_utc(value: Any) -> str | None:
    """Normalize WordPress dates to ISO-8601 UTC ('Z'). `date_gmt` strings are naive UTC; others may carry offsets."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(v, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def coalesce_str(*values: Any) -> str | None:
    for v in values:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None
