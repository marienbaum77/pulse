# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Content sources beyond news: YouTube (channel, playlist or search; transcripts as item text, no API key), web search via a bundled SearXNG service, sites without RSS (feed autodiscovery or article links from a listing page), Reddit (Atom feed) and public Telegram channels (t.me/s preview)
- Channel type per project: news, curated (each video/article/post is a standalone candidate with its own window, engagement and depth scoring) or mixed (per-source default with override)
- Post formats by material: video recommendation, article breakdown, post retelling; digests of curated items are titled «Подборка»
- `content_seen` memory so curated links removed by retention are not re-added by search
- `SEARXNG_URL`, `SEARXNG_SECRET`, `YOUTUBE_PROXY_URL` settings
- Source discovery: the LLM turns the project topic into search queries, SearXNG results are grouped into candidate sources (YouTube channels via oEmbed, sites with RSS or article lists, subreddits, Telegram channels), ranked by repetition across queries and model rating, and offered to the editor to add or dismiss
- Content plan: schedule slots filtered by material kind (news/video/article/post), drafts prepared ahead of the slot (`lead_minutes`) and published at the slot time, per-project source cooldown, and a «Контент-план» page with upcoming slots, their drafts, candidate counts and past outcomes

- Publication channels: VK (wall) and Discord (webhook embeds)
- Stage-1 lexical dedup via MinHash/LSH before embeddings (MINHASH_DEDUP_ENABLED, MINHASH_THRESHOLD)
- At-rest encryption for channel secrets (secret, ccess_token, Discord webhook_url) derived from SECRET_KEY; plaintext values are re-encrypted on API/worker startup
- SSRF-safe GuardedAsyncTransport: resolve + validate, then connect to pinned IP (Host/SNI preserved)
- Frontend 	ypecheck and Vitest smoke tests in CI
- Comprehensive filtering and sorting in Stories (/trends): search by title, sort by NWS / interest / date / items / sources / topic fit, filters for min NWS, interest score, images
- Interactive timeline tooltips and timestamps for publication arrival dots (ArrivalStrip)
- Queue cancellation API (DELETE /api/generation-queue/{job_id}) and cancel action in Drafts UI

### Changed

- Discord webhook URL is no longer returned by the API; empty field on edit keeps the stored value
- Docs and README cover VK, Discord, and MinHash
- Release workflow supports manual trigger via workflow_dispatch in addition to version tags

### Fixed

- File import (`/items/import`) now follows the project's channel type instead of always adding news items
- YouTube search sources no longer pass `engines=youtube` to SearXNG (it is combined with the category, not a filter); non-YouTube results are dropped by URL, so videos found by bing/duckduckgo still count when the youtube engine is blocked

- Telegram photo publishing: check visible length after HTML parsing so posts with citations send as native sendPhoto rather than falling back to text preview links
- Post generation length: strictly enforce max_length in LLM prompts and post truncation (eliminated +200 buffer)
- Generation queue timeout: reduced RSS enrichment wait from 10 minutes to 60 seconds to prevent generation stall

## [1.0.1] - 2026-10-07

### Added

- Topic scoring by aspect (clustering quality, source authority, freshness)
- Cover image filtering for generated posts
- Ingest slot management to keep workers free
- Troubleshooting notes for provider 403 errors and Telegram cover images

### Changed

- Topic fit evaluation uses cluster headlines instead of individual articles
- Improved LLM error resilience with transient-error retry
- Long Telegram posts are sent as one message again, with the cover shown as a link preview of the source article

### Fixed

- API helper imports after router split
- Topic threshold default migration
- Topic threshold for existing installations
- Cover image download is retried on temporary failures; a post is no longer published as text-only when its cover could not be loaded
