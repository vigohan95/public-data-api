# RapidAPI listing copy (Bluesky Public Data)

**Name:** Bluesky Public Data

**Short description:** Public Bluesky data, normalized: deep search paging without login, who liked/reposted/quoted a post, starter-pack members, threads, profiles, followers.

**Category:** Data

**Long description:**

Public Bluesky (AT Protocol) data through the official AppView, no Bluesky login, one normalized schema. Nine endpoints: search, an account's posts, custom feeds, threads with the full reply tree, profiles, followers, follows, post engagement (who liked, reposted or quoted), and starter-pack / list members.

What the plain AT Protocol passthroughs on the hub do not do:

- Search that keeps paging without a login. The public AppView rejects `cursor` on page 2 for logged-out search; this API pages by time and returns `nextUntil`, so you can walk thousands of results per keyword.
- Engagement audiences: who liked a post (with the like time), who reposted it, and every quote post of it. Ready-made niche audiences from starter packs and lists.
- One schema for posts (hashtags, mentions, links, images with alt text, video, link card, quoted post, labels, like / repost / reply / quote counts) and one for profiles (bio, counts, labels, pinned post, links found in the bio).
- Engagement filters (`min_likes`, `min_reposts`, `min_replies`) on every post endpoint: you receive only the posts that took off.
- Respects accounts that opted out of logged-out visibility, exposes labels, keeps to the AppView's rate limits with backoff.

Typical latency 1-2 s per call (one AppView page). Open source (MIT): https://github.com/vigohan95/public-data-api

**Plans (monthly):**

| Plan | Price | Requests / month | Overage | Rate limit |
|---|---|---|---|---|
| Basic | $0 | 100 | hard limit | 1 req/s |
| Pro | $5 | 5,000 | $0.002 / request | 3 req/s |
| Ultra | $15 | 30,000 | $0.001 / request | 5 req/s |
| Mega | $39 | 120,000 | $0.0005 / request | 10 req/s |

Rationale (2026-10-23 competitor check, 7 Bluesky APIs read): $5 / 10,000 and $15 / 50,000 hard-limit passthroughs, $4.99 / 1,500 for a two-endpoint API, $12.99 / 10,000 + $0.01 overage for the 34-endpoint one. Ours is priced between them and sells things they do not have.

**Endpoints (base URL https://public-data-api.vercel.app):**

- GET /v1/bluesky/search — q (required), limit (1-100), sort (latest|top), since, until, lang, author, mentions, domain, url, tag, min_likes, min_reposts, min_replies, raw
- GET /v1/bluesky/posts — actor (required), filter, include_reposts, include_pins, limit, cursor, min_likes, min_reposts, min_replies, raw
- GET /v1/bluesky/feed — feed (required), include_reposts, limit, cursor, min_* , raw
- GET /v1/bluesky/thread — post (required), depth, parent_height, limit, raw
- GET /v1/bluesky/profiles — actors (required, comma list, max 25), raw
- GET /v1/bluesky/followers — actor (required), limit, cursor, sort, raw
- GET /v1/bluesky/follows — actor (required), limit, cursor, raw
- GET /v1/bluesky/engagement — post (required), kind (likers|reposters|quotes), limit, cursor, raw
- GET /v1/bluesky/list — list (required), limit, cursor, raw

**Owner steps after the listing exists:** copy this API's X-RapidAPI-Proxy-Secret (Studio → Gateway) and append it to the Vercel env `RAPIDAPI_PROXY_SECRET` as a comma-separated second value, then Redeploy; then make the listing public.

## Status 2026-10-23
Listing created: https://rapidapi.com/HanVigo/api/bluesky-public-data (Studio id api_3c69b44a-1a47-41d2-b432-9a4c63607bae). Overview, terms, base URL, 9 endpoints with parameters and examples, four plans, README docs, gateway timeout 60 s, health check /health: done by the automation.

Owner steps (the automation is not allowed to read or write secrets, nor accept the publishing terms):
1. Studio → Gateway → X-RapidAPI-Proxy-Secret → Copy. https://rapidapi.com/studio/api_3c69b44a-1a47-41d2-b432-9a4c63607bae/publish/gateway
2. Vercel → public-data-api → Settings → Environment Variables → Add Environment Variable: Key `RAPIDAPI_PROXY_SECRET_2`, Value = the copied secret → Save (Vercel secrets are write-only, so the first variable cannot be appended to; the app accepts RAPIDAPI_PROXY_SECRET, _2, _3, ...). Then tell Claude, who triggers the redeploy.
3. Studio → General → Visibility: tick the rights confirmation, switch to Public, Save. https://rapidapi.com/studio/api_3c69b44a-1a47-41d2-b432-9a4c63607bae/publish/general

### 2026-10-23 evening: public
Owner completed steps 1-3. Verified: second proxy secret live (direct call to /v1/* 403, RapidAPI Playground 200), and an anonymous fetch of https://rapidapi.com/HanVigo/api/bluesky-public-data returns the full listing (title, 9 endpoints, 4 plans). Both APIs of this repo are now public on the hub. The plan carousel also shows a deprecated $0 BASIC entry left over from the first draft; it is hidden from subscribers and does nothing.
