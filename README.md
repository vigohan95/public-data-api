# WordPress Content Export API

Any public WordPress site in, clean JSON out: posts, pages and custom post types with **HTML + Markdown + plain text**, word count and reading time, categories/tags/author resolved to names, featured image URL and alt text, published/modified dates. One call per page of results; no plugin, no login, no API key on the WordPress side.

Live: deployed on Vercel and listed on RapidAPI (see `docs/rapidapi.md`). Core modules are shared with the Apify Actor [wordpress-content-export](https://apify.com/vigohan95/wordpress-content-export).

## Endpoints

| Endpoint | What it returns |
|---|---|
| `GET /v1/wordpress/site?url=` | Is it WordPress, where is the REST root, name, language, WordPress version, public post types |
| `GET /v1/wordpress/posts?url=&type=post&page=1&per_page=20&search=&modified_after=&orderby=date&order=desc&fields=markdown,text` | One page of items with cleaned content fields; `total` and `totalPages` from the site |
| `GET /v1/wordpress/terms?url=&kind=category\|tag` | Categories or tags with counts |
| `GET /v1/wordpress/authors?url=` | Public author profiles |
| `GET /v1/wordpress/media?url=` | Media library metadata (source URL, alt, mime, dimensions) |
| `GET /docs` | OpenAPI UI |

`fields` picks the content representations: `html`, `markdown`, `text`, `raw` (the untouched REST object). Default `markdown,text`.

When a site has disabled its REST API, `/posts` falls back to sitemap / RSS discovery plus article extraction from the page HTML (`source: "html"`, `per_page` capped at 20, no `search`).

## What makes it different from "wp-json passthrough" APIs

- Content comes back three ways (HTML cleaned of scripts/shortcodes, Markdown, plain text) with word count and reading time, so it drops straight into a RAG index or a migration.
- Terms and authors are resolved to names in the same call (`_embed`), not just IDs.
- Any public custom post type (`product`, `docs`, ...) works; `/site` lists them.
- REST root discovery handles sub-directory installs and plain permalinks (`?rest_route=`); sites without REST still export through the HTML fallback.
- Polite by construction: robots.txt respected, 2 requests/s per host, backoff on 429.

## Run locally

```bash
pip install -r requirements-dev.txt
uvicorn api.index:app --reload
curl "http://127.0.0.1:8000/v1/wordpress/posts?url=https://wordpress.org/news&per_page=3"
pytest -q
```

Measured against `wordpress.org/news` on 2026-10-23: `/site` 3.3 s, `/posts` (3 items, Markdown + text) 3.4 s, `/terms` 5.6 s, `/authors` 2.7 s; a non-WordPress site answers 404 in about 7 s after the fallback probes.

## Deploy (Vercel)

`vercel.json` rewrites every path to `api/index.py` (FastAPI on the Python runtime) with a 60 s budget. Set `RAPIDAPI_PROXY_SECRET` in the project to make `/v1/*` accept only requests that come through the RapidAPI proxy.

MIT.
