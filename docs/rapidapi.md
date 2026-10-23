# RapidAPI listing copy (WordPress Content Export API)

**Name:** WordPress Content Export API — posts to Markdown, text & JSON

**Short description (≤ 100 chars):** Any public WordPress site to clean JSON: posts, pages, custom types as HTML + Markdown + text, one call.

**Category:** Data

**Long description:**

Give it any URL of a WordPress site and get back structured content ready for a RAG index, a migration or a content audit: title, slug, URL, excerpt, the body as cleaned HTML, Markdown and plain text, word count and reading time, categories, tags and author resolved to names, featured image URL and alt text, published and modified dates. Works on posts, pages and any public custom post type (`product`, `docs`, ...). No plugin to install on the WordPress side, no login.

What you do not get from "wp-json passthrough" APIs:

- Markdown and plain text, not just raw HTML with shortcodes and scripts.
- Names, not IDs, for categories, tags and authors (one call).
- REST root discovery for sub-directory installs and plain-permalink sites; when a site has disabled its REST API, the `/posts` endpoint falls back to sitemap / RSS discovery and article extraction.
- `modified_after` for incremental syncs, `search`, `orderby`, paging with `total` / `totalPages` straight from the site.
- Polite by construction: robots.txt respected, 2 requests/s per host, backoff on 429, so your key does not get a site blocked.

Endpoints: `/v1/wordpress/site`, `/v1/wordpress/posts`, `/v1/wordpress/terms`, `/v1/wordpress/authors`, `/v1/wordpress/media`. Typical latency 2–4 s per call (one upstream listing request plus discovery). Open source (MIT): https://github.com/vigohan95/public-data-api

**Plans (monthly):**

| Plan | Price | Requests / month | Overage | Rate limit |
|---|---|---|---|---|
| Basic | $0 | 50 | hard limit | 1 req/s |
| Pro | $3 | 2,000 | $0.003 / request | 2 req/s |
| Ultra | $9 | 20,000 | $0.002 / request | 5 req/s |
| Mega | $29 | 100,000 | $0.001 / request | 10 req/s |

Rationale (competitor check 2026-10-23, `modules/products/RAPIDAPI-COMPETITORS-2026-10-23.md`): the two WordPress-reading APIs on the hub charge $3 / 3,000 with $0.006 overage (single raw endpoint) and $5 / $25 / $199 tiers (plain wp-json wrapper). Same entry price, lower overage, more per call.

**Base URL:** the Vercel deployment (set after `vercel.com/new` import). **Proxy secret:** copy RapidAPI's `X-RapidAPI-Proxy-Secret` into the Vercel env `RAPIDAPI_PROXY_SECRET` and redeploy, so only RapidAPI traffic reaches `/v1/*`.

**Payout:** RapidAPI pays providers via PayPal; the payout email is entered by the account owner in Provider settings (never by the automation).

## Status 2026-10-23
Done in the RapidAPI Studio: overview, base URL `https://public-data-api.vercel.app`, 5 endpoints with parameters, plans (Basic free 50 hard limit; Pro $3 / 2,000 + $0.003, recommended; Ultra $9 / 20,000 + $0.002; Mega $29 / 100,000 + $0.001), proxy timeout 60 s, health check `/health`.

Two things the automation is not allowed to do (both are one click for the owner):
1. **Make it public**: Studio → Hub Listing → General → Visibility: tick "I confirm that I own or have rights to publish this API", flip the switch, Save. https://rapidapi.com/studio/api_d5b201ba-fdb3-468f-9aa5-fc4a5aa1d041/publish/general
2. **Proxy secret**: Studio → Gateway → X-RapidAPI-Proxy-Secret → Copy; Vercel → project public-data-api → Settings → Environment Variables → add `RAPIDAPI_PROXY_SECRET` → Redeploy. Until then `/v1/*` also answers direct calls (no billing).
Payout email: Provider Dashboard → Payment Settings (PayPal), owner only.
