# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- TikTok bot makes videos only from posts the owner forwards to it: channel posts are no longer picked up automatically, the bot does not need to be a channel admin, `/pause` and `/resume` are gone, `TIKTOK_BOT_CHANNEL` is optional (caption handle for private channels), and a skipped short post or a non-channel forward gets a reply

### Added

- Optional local editorial interest model (`INTEREST_MODE=local`) trained on reference Telegram channels, editor decisions and the unlabeled stream, with project settings, retraining API and LLM rollback; the default remains `llm` pending a real-data comparison
- Editorial model dataset exporter and held-out evaluation script with grouped temporal splitting, bootstrap confidence intervals, NWS/topic/optional real-LLM baselines, ablations, resource measurements and reproducible thesis figures; synthetic results are explicitly marked as demonstrations

- Separate TikTok bot (`tiktok-bot/`, `docker-compose.tiktok.yml`): every new post of the Telegram channel becomes a 1080×1920 video (title and image, the post video or GIF, text slides, channel slide; Pillow + bundled ffmpeg, no LLM) uploaded to the TikTok inbox via Content Posting API (Upload mode works without an app audit); a copy and a ready caption go to the owner; `/auth`, `/status`, `/pause`, `/resume`, forwarded posts become videos too
- TikTok bot videos are screenshots of the post as Telegram shows it (the `t.me/<channel>/<id>?embed=1` page in headless Chromium, views counter hidden, date in Russian); the post video plays inside the player frame for its whole length (up to `TIKTOK_BOT_MAX_VIDEO_SECONDS`, default 600) with its own sound, taken from the Telegram CDN without the 20 MB Bot API limit; GIFs loop while the post is read; posts without video stay on screen for the reading time; posts up to a quarter taller than the safe zone are shrunk to fit, taller ones scroll to the end of the text (after the video, if there is one) at a constant whole number of pixels per frame at 60 fps, with RGB overlays so positions are not rounded to even pixels; videos without scrolling keep their own frame rate; slides remain the fallback for private channels and missing posts (`TIKTOK_BOT_SCREENSHOT=0`); videos over 64 MB upload in chunks
- TikTok bot statistics: published inbox videos are found by upload status or by caption, views, likes, comments and shares are recorded at 1 h, 6 h, 24 h, 3 d, 7 d and 30 d (`video.list` scope, Display API); `/status` shows the latest numbers and `/export` sends a CSV of posts, video features and snapshots keyed by the channel message id

- Interest-driven publishing: a project threshold (`instant_interest`) publishes stories the LLM rated at or above it right after rating, without waiting for a slot, capped by `instant_daily_limit` posts per 24 hours; a passing post goes out even in manual-review mode
- `single_min_interest`: news stories with fewer than `min_items` materials are publishable when their LLM interest rating reaches the threshold
- The LLM writes topic aspects from the project topic when the field is left empty, and rewrites them when the topic changes until the editor edits them by hand
- Primary link line: a GitHub/GitLab repository, Hugging Face model/dataset/demo, «official site» link, store page or arXiv paper found in the feed entry, the article page, a Telegram post or the item URL itself is appended to the post as «Репозиторий 👉 [name](url)»
- Post markup: `**bold**` and `[text](url)` in draft bodies, rendered as Telegram HTML, native Discord markdown and plain text for VK; the default prompts ask for one or two bold highlights (`{formatting}` in custom prompts); links the model invents are reduced to their text
- Cover and video from the original source: article enrichment finds the link to the original (URL slug and site name matched against the title, blog paths, «Источник:»/«via» captions) and the article's own illustrations and videos; post preparation loads the original once and prefers its illustrations for the cover; videos (any length, up to Telegram's 50 MB bot limit) go out with `sendVideo` and GIFs with `sendAnimation`, falling back to the image; README media of the «👉» repository (demo GIFs, GitHub video attachments, diagrams; badges skipped) is used as well; up to three extra checked illustrations turn the post into a Telegram album (`sendMediaGroup`) and several VK photos; `drafts.video_url`, `video_kind`, `extra_images`, preview and removal buttons in the draft — no extra LLM calls
- Post statistics: views and reactions of own posts are read from the public `t.me/s/<channel>` preview every three hours (no bot, no LLM); a «Статистика» page compares posts with the channel median, source performance (×0.5–1.5 from posts older than 48 hours) multiplies authority in story scoring, and the best and worst posts are added to the interest-rating prompt without extra calls
- Replies to related posts: a follow-up is sent as a Telegram reply (`reply_parameters`) to the earlier post of the same channel when it updates the same story, shares the «👉» link or the original source, or has a close vector with a shared name in the title (`reply_related_days`, default 14); no LLM calls, «Не отвечать» in the draft
- Per-channel signature (`config.signature`) appended to every post
- Project interest criteria (`interest_criteria`) replace the default rating criteria; open stories are re-rated after a change
- `slot_min_interest`: a schedule slot takes a story only if the LLM rated it at or above the threshold; otherwise the slot stays empty (`nothing_to_publish` with `reason: below_interest_floor`), and unrated stories are skipped too
- The interest prompt rates self-promotion (an author announcing their own product, repository, service, course or channel) at most 4 without independent facts, even with custom criteria; the «don't penalize ads» rule now applies to video transcripts only. Ratings of open stories are reset by migration 029
- The audience hint in the interest prompt is off until the channel's median views reach 30: with a few views per post, «best» and «worst» posts are noise

- Optional `docker-compose.doh.yml` overlay: a DNS-over-HTTPS resolver (AdGuard dnsproxy) for api, worker and searxng, for networks where plain DNS answers for www.youtube.com are spoofed; also shipped with the desktop installer

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

- Listicle titles («14 лучших скиллов…», «Топ-10…») are checked against the post: the source item gets article-sized context, the prompt (including custom instructions) asks for a title that matches the text, the model rewrites a title that promises more items than the post lists, and a remaining mismatch keeps the post for review even in full-auto mode
- A single-source news story gives the model up to 12 sentences from the first 40 instead of `context_sentences` (3) from 14
- Interest rating looks at up to 60 candidates per pass (was 20) with up to 12 fresh ratings (was 6); schedule slots pick the most interesting story from the same wider pool
- Discord webhook URL is no longer returned by the API; empty field on edit keeps the stored value
- Docs and README cover VK, Discord, and MinHash
- Release workflow supports manual trigger via workflow_dispatch in addition to version tags

### Fixed

- Long articles from one source (6000+ characters: analysis, longreads) are no longer posted from their first paragraphs: the model gets the whole article (up to 12000 characters; longer ones as the beginning plus an even sample of the rest with gaps marked «…»), writes it as an article breakdown that starts with the author's main point rather than the news the article opens with, and has its own length limit `article_max_length` (default 1500, «Длина разбора статьи» in project settings); the model answer token budget grows with the limit instead of the fixed 700 tokens that cut long posts
- Album illustrations are compared by content, not only by file name: the same picture under another name, in another size or format (Habr serves one table as two files) and a zoomed crop of the cover (Habr crops the original illustration) no longer appear twice; pictures are matched by normalized correlation of small grayscale copies with a coarse-to-fine search over scales
- Articles found by «Поиск статей» (Google News links `news.google.com/rss/articles/…`) are enriched again: the link is resolved to the article address (from the id in old links, through Google's `batchexecute` request in new ones), the article is downloaded from there and the item URL is replaced, so posts link to the source site; before, a JavaScript redirect stub was downloaded instead and 307 of 351 such items had no full text
- Album illustrations no longer include thumbnails of other articles of the same site (a «read also» slider inside `<article>` on thecode.media), ad banners with utm links, or the page social card (`og:image`) and its resized copy in the article header

- «Применить модели» in «Система» was always disabled when generation and embeddings run on different servers (Groq + Ollama): the model list now takes chat models from `LLM_BASE_URL` and embedding models from `EMBED_BASE_URL`, hides speech, TTS and prompt-guard models, and «Скачать» pulls into Ollama
- Images served with a wrong Content-Type (WebP from Google Cloud Storage as `application/octet-stream`) are recognised by their bytes, and other formats (AVIF, BMP, TIFF) are converted to JPEG with Pillow; when the cover still cannot be sent, the post's other illustrations are tried, then the post goes out as text instead of failing
- Videos embedded through custom components (`<uni-inline-image or-mp4-video-url=…>` on the Google blog, `data-video-src`) and `og:video` files are found; album extras skip copies of the cover in another size or format
- GIFs are converted to H.264 MP4 (bundled ffmpeg via `imageio-ffmpeg`) before `sendAnimation`, so Telegram no longer re-encodes them with visible quality loss
- Articles from sites that block bots (Cloudflare 403 at openai.com), geo-blocks and rate limits (401/403/429/451/503) are read from the latest Wayback Machine snapshot instead of keeping only the RSS teaser
- Number fields in the settings no longer clamp on every keystroke (typing 600 into «Максимальная длина» used to jump to 200 or 3500); limits apply when the field loses focus
- The project's `show_sources` setting was stored but ignored: publishers now drop the «Источники:» footer when it is off, and the settings page has a toggle for it
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
