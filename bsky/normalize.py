"""Flatten AppView post / profile views into the stable public output schema."""
from __future__ import annotations

import re

from datetime import datetime, timezone
from typing import Any

from .atproto import parse_at_uri, post_url, profile_url

EMBED_IMAGES = "app.bsky.embed.images#view"
EMBED_VIDEO = "app.bsky.embed.video#view"
EMBED_EXTERNAL = "app.bsky.embed.external#view"
EMBED_RECORD = "app.bsky.embed.record#view"
EMBED_RECORD_WITH_MEDIA = "app.bsky.embed.recordWithMedia#view"
VIEW_RECORD = "app.bsky.embed.record#viewRecord"
REASON_REPOST = "app.bsky.feed.defs#reasonRepost"
REASON_PIN = "app.bsky.feed.defs#reasonPin"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _labels(obj: dict[str, Any] | None) -> list[str]:
    out: list[str] = []
    for label in (obj or {}).get("labels") or []:
        if isinstance(label, dict) and label.get("val"):
            out.append(str(label["val"]))
    return sorted(set(out))


def _facets(record: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    """Extract hashtags, mentioned DIDs and link URIs from rich-text facets."""
    tags: list[str] = []
    mentions: list[str] = []
    links: list[str] = []
    for facet in record.get("facets") or []:
        for feat in (facet or {}).get("features") or []:
            t = feat.get("$type", "")
            if t == "app.bsky.richtext.facet#tag" and feat.get("tag"):
                tags.append(str(feat["tag"]))
            elif t == "app.bsky.richtext.facet#mention" and feat.get("did"):
                mentions.append(str(feat["did"]))
            elif t == "app.bsky.richtext.facet#link" and feat.get("uri"):
                links.append(str(feat["uri"]))
    for tag in record.get("tags") or []:  # self-labelled "outline" tags
        if isinstance(tag, str):
            tags.append(tag)
    return _dedupe(tags), _dedupe(mentions), _dedupe(links)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for i in items:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _quoted(view: dict[str, Any] | None) -> dict[str, Any] | None:
    """Turn an embed.record#view into a compact quoted-post summary (None for blocked/not-found/non-post records)."""
    if not isinstance(view, dict):
        return None
    rec = view.get("record") if view.get("$type") == EMBED_RECORD else view
    if not isinstance(rec, dict) or rec.get("$type") != VIEW_RECORD:
        return None
    author = rec.get("author") or {}
    value = rec.get("value") or {}
    return {
        "uri": rec.get("uri"),
        "cid": rec.get("cid"),
        "url": post_url(author.get("handle") or author.get("did") or "", rec.get("uri") or ""),
        "authorDid": author.get("did"),
        "authorHandle": author.get("handle"),
        "authorDisplayName": author.get("displayName"),
        "text": value.get("text"),
        "createdAt": value.get("createdAt"),
        "likeCount": rec.get("likeCount"),
        "repostCount": rec.get("repostCount"),
        "replyCount": rec.get("replyCount"),
        "quoteCount": rec.get("quoteCount"),
    }


def _embed(embed: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {"images": [], "video": None, "external": None, "quotedPost": None, "embedType": None}
    if not isinstance(embed, dict):
        return out
    t = embed.get("$type", "")
    out["embedType"] = t.replace("app.bsky.embed.", "").replace("#view", "") or None
    media = embed.get("media") if t == EMBED_RECORD_WITH_MEDIA else embed
    mt = (media or {}).get("$type", "") if isinstance(media, dict) else ""
    if mt == EMBED_IMAGES:
        for img in media.get("images") or []:
            ar = img.get("aspectRatio") or {}
            out["images"].append({"url": img.get("fullsize"), "thumb": img.get("thumb"), "alt": img.get("alt") or "", "width": ar.get("width"), "height": ar.get("height")})
    elif mt == EMBED_VIDEO:
        ar = media.get("aspectRatio") or {}
        out["video"] = {"playlist": media.get("playlist"), "thumbnail": media.get("thumbnail"), "alt": media.get("alt") or "", "cid": media.get("cid"), "width": ar.get("width"), "height": ar.get("height")}
    elif mt == EMBED_EXTERNAL:
        ext = media.get("external") or {}
        out["external"] = {"url": ext.get("uri"), "title": ext.get("title"), "description": ext.get("description"), "thumb": ext.get("thumb")}
    if t == EMBED_RECORD_WITH_MEDIA:
        out["quotedPost"] = _quoted(embed.get("record"))
    elif t == EMBED_RECORD:
        out["quotedPost"] = _quoted(embed)
    return out


def normalize_post(
    post: dict[str, Any],
    *,
    reason: dict[str, Any] | None = None,
    include_raw: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """`post` is an app.bsky.feed.defs#postView; `reason` the feedViewPost reason (repost / pin) when known."""
    author = post.get("author") or {}
    record = post.get("record") or {}
    reply = record.get("reply") or {}
    tags, mentions, links = _facets(record)
    emb = _embed(post.get("embed"))
    try:
        _, _, rkey = parse_at_uri(post.get("uri") or "")
    except ValueError:
        rkey = None
    is_repost = bool(reason and reason.get("$type") == REASON_REPOST)
    reposter = (reason or {}).get("by") or {} if is_repost else {}
    parent_uri = (reply.get("parent") or {}).get("uri")
    root_uri = (reply.get("root") or {}).get("uri")
    item: dict[str, Any] = {
        "uri": post.get("uri"),
        "cid": post.get("cid"),
        "rkey": rkey,
        "url": post_url(author.get("handle") or author.get("did") or "", post.get("uri") or ""),
        "authorDid": author.get("did"),
        "authorHandle": author.get("handle"),
        "authorDisplayName": author.get("displayName"),
        "authorAvatar": author.get("avatar"),
        "authorLabels": _labels(author),
        "text": record.get("text") or "",
        "langs": list(record.get("langs") or []),
        "createdAt": record.get("createdAt"),
        "indexedAt": post.get("indexedAt"),
        "isReply": parent_uri is not None,
        "replyParentUri": parent_uri,
        "replyRootUri": root_uri,
        "isQuote": emb["quotedPost"] is not None,
        "quotedPostUri": (emb["quotedPost"] or {}).get("uri"),
        "isRepost": is_repost,
        "repostedByDid": reposter.get("did"),
        "repostedByHandle": reposter.get("handle"),
        "repostIndexedAt": (reason or {}).get("indexedAt") if is_repost else None,
        "isPinned": bool(reason and reason.get("$type") == REASON_PIN),
        "likeCount": post.get("likeCount") or 0,
        "repostCount": post.get("repostCount") or 0,
        "replyCount": post.get("replyCount") or 0,
        "quoteCount": post.get("quoteCount") or 0,
        "bookmarkCount": post.get("bookmarkCount"),
        "embedType": emb["embedType"],
        "images": emb["images"],
        "video": emb["video"],
        "externalLink": emb["external"],
        "quotedPost": emb["quotedPost"],
        "hashtags": tags,
        "mentions": mentions,
        "links": links,
        "labels": _labels(post),
        "threadgate": bool(post.get("threadgate")),
        "scrapedAt": now_iso(),
    }
    if extra:
        item.update(extra)
    if include_raw:
        item["raw"] = {"post": post, "reason": reason} if reason else {"post": post}
    return item


_BIO_URL_RE = re.compile(r"(?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s)\]]*)?", re.I)
_BIO_SKIP = ("bsky.social", "bsky.app")


def bio_links(description: str | None) -> list[str]:
    """Website / link-in-bio URLs written in a profile description (handles like name.bsky.social are skipped)."""
    if not description:
        return []
    out: list[str] = []
    for m in _BIO_URL_RE.finditer(description):
        start = m.start()
        if start > 0 and description[start - 1] in "@.":  # @handle.domain or the domain part of an e-mail address
            continue
        if description[m.end() : m.end() + 1] == "@":  # local part of an e-mail address (jane.doe@...)
            continue
        url = m.group(0).rstrip(".,;:!?")
        host = re.sub(r"^https?://", "", url, flags=re.I).split("/")[0].lower()
        if host.endswith(_BIO_SKIP) or "." not in host or host.replace(".", "").isdigit():
            continue
        full = url if url.lower().startswith("http") else f"https://{url}"
        if full not in out:
            out.append(full)
    return out


def normalize_profile(profile: dict[str, Any], *, include_raw: bool = False, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """`profile` is a profileView / profileViewDetailed (counts are absent in the basic graph views)."""
    assoc = profile.get("associated") or {}
    pinned = profile.get("pinnedPost") or {}
    item: dict[str, Any] = {
        "did": profile.get("did"),
        "handle": profile.get("handle"),
        "displayName": profile.get("displayName"),
        "description": profile.get("description"),
        "avatar": profile.get("avatar"),
        "banner": profile.get("banner"),
        "url": profile_url(profile.get("handle") or profile.get("did") or ""),
        "followersCount": profile.get("followersCount"),
        "followsCount": profile.get("followsCount"),
        "postsCount": profile.get("postsCount"),
        "createdAt": profile.get("createdAt"),
        "indexedAt": profile.get("indexedAt"),
        "labels": _labels(profile),
        "pinnedPostUri": pinned.get("uri"),
        "listsCount": assoc.get("lists"),
        "feedgensCount": assoc.get("feedgens"),
        "starterPacksCount": assoc.get("starterPacks"),
        "isLabeler": bool(assoc.get("labeler")),
        "verificationStatus": (profile.get("verification") or {}).get("verifiedStatus"),
        "bioLinks": bio_links(profile.get("description")),
        "scrapedAt": now_iso(),
    }
    if extra:
        item.update(extra)
    if include_raw:
        item["raw"] = profile
    return item


def flatten_thread(thread: dict[str, Any], *, max_items: int | None = None) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], list[str]]:
    """Walk a getPostThread response into [(postView, {threadPosition, threadDepth, threadRootUri})] + warnings.

    Order: parents (top-most first), the requested post, then replies depth-first. Not-found / blocked nodes become
    warnings instead of items."""
    items: list[tuple[dict[str, Any], dict[str, Any]]] = []
    warnings: list[str] = []
    node = thread
    if node.get("$type") != "app.bsky.feed.defs#threadViewPost":
        warnings.append(f"thread root is {node.get('$type', 'unknown')} ({node.get('uri', '?')})")
        return items, warnings

    # climb parents
    parents: list[dict[str, Any]] = []
    cur = node.get("parent")
    while isinstance(cur, dict):
        if cur.get("$type") != "app.bsky.feed.defs#threadViewPost":
            warnings.append(f"parent is {cur.get('$type', 'unknown')} ({cur.get('uri', '?')})")
            break
        parents.append(cur)
        cur = cur.get("parent")
    parents.reverse()
    root_uri = (parents[0]["post"]["uri"] if parents else node["post"]["uri"]) if node.get("post") else None
    record_reply = ((node.get("post") or {}).get("record") or {}).get("reply") or {}
    if record_reply.get("root", {}).get("uri"):
        root_uri = record_reply["root"]["uri"]

    for depth, p in enumerate(parents, start=-len(parents)):
        items.append((p["post"], {"threadPosition": "parent", "threadDepth": depth, "threadRootUri": root_uri}))
    items.append((node["post"], {"threadPosition": "focus", "threadDepth": 0, "threadRootUri": root_uri}))

    def walk(n: dict[str, Any], depth: int) -> None:
        for child in n.get("replies") or []:
            if max_items is not None and len(items) >= max_items:
                return
            if not isinstance(child, dict):
                continue
            if child.get("$type") != "app.bsky.feed.defs#threadViewPost":
                warnings.append(f"reply is {child.get('$type', 'unknown')} ({child.get('uri', '?')})")
                continue
            items.append((child["post"], {"threadPosition": "reply", "threadDepth": depth, "threadRootUri": root_uri}))
            walk(child, depth + 1)

    walk(node, 1)
    if max_items is not None:
        items = items[:max_items]
    return items, warnings
