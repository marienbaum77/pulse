"""Юнит-тесты коннекторов, подборочного (curated) режима и автоподбора источников: разбор выдачи площадок, оценка и форматы постов. Без БД и сети."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pytest

from app.pipeline import connectors, scoring
from app.pipeline.connectors import reddit, searx, site, telegram, youtube
from app.pipeline.context import Block
from app.pipeline.generate import automatic_review, blocks_prompt, content_format, decide_use_llm, render_system

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
CHAT = SimpleNamespace(supports_chat=True)


# ---------- YouTube ----------
@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s",
    "https://youtu.be/dQw4w9WgXcQ?si=abc",
    "https://m.youtube.com/shorts/dQw4w9WgXcQ",
    "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ",
    "https://www.youtube.com/live/dQw4w9WgXcQ",
])
def test_video_id_understands_all_link_forms(url):
    assert youtube.video_id(url) == "dQw4w9WgXcQ"


@pytest.mark.parametrize("url", ["https://vimeo.com/123", "https://www.youtube.com/@channel", "https://www.youtube.com/watch?v=short", "not a url"])
def test_video_id_rejects_non_videos(url):
    assert youtube.video_id(url) is None


def test_classify_youtube_input():
    assert youtube.classify_input("https://www.youtube.com/channel/UCuAXFkgsw1L7xaCfnd5JJOw") == {"kind": "channel", "channel_id": "UCuAXFkgsw1L7xaCfnd5JJOw"}
    assert youtube.classify_input("UCuAXFkgsw1L7xaCfnd5JJOw")["kind"] == "channel"
    assert youtube.classify_input("https://www.youtube.com/playlist?list=PL123abc") == {"kind": "playlist", "playlist_id": "PL123abc"}
    assert youtube.classify_input("youtube.com/@fireship/videos") == {"kind": "handle", "path": "@fireship"}
    assert youtube.classify_input("@fireship") == {"kind": "handle", "path": "@fireship"}
    assert youtube.classify_input("https://www.youtube.com/c/SomeName") == {"kind": "handle", "path": "c/SomeName"}
    assert youtube.classify_input("уроки Python для начинающих") == {"kind": "search", "query": "уроки Python для начинающих"}
    with pytest.raises(ValueError):
        youtube.classify_input("https://www.youtube.com/feed/trending")


def test_channel_id_is_found_on_channel_page():
    page = '<html><head><meta itemprop="identifier" content="UCuAXFkgsw1L7xaCfnd5JJOw"></head></html>'
    assert youtube.channel_id_from_page(page) == "UCuAXFkgsw1L7xaCfnd5JJOw"
    assert youtube.channel_id_from_page('..."externalId":"UCabcdefghijklmnopqrstuv"...') == "UCabcdefghijklmnopqrstuv"
    assert youtube.channel_id_from_page("<html></html>") is None


YT_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom">
 <entry>
  <yt:videoId>dQw4w9WgXcQ</yt:videoId>
  <title>How databases work</title>
  <link rel="alternate" href="https://www.youtube.com/watch?v=dQw4w9WgXcQ"/>
  <author><name>Tech Channel</name></author>
  <published>2024-01-02T10:00:00+00:00</published>
  <media:group>
   <media:thumbnail url="https://i1.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg" width="480" height="360"/>
   <media:description>Indexes, B-trees and query planning.</media:description>
   <media:community><media:starRating count="1500" average="5.00"/><media:statistics views="123456"/></media:community>
  </media:group>
 </entry>
 <entry><title>No id</title></entry>
</feed>"""


def test_parse_video_feed_reads_media_group():
    [video] = youtube.parse_video_feed(YT_FEED)
    assert video["kind"] == "video"
    assert video["url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert video["text"] == "Indexes, B-trees and query planning."
    assert video["image_url"] == "https://i1.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"
    assert video["publisher_name"] == "Tech Channel"
    assert video["meta"] == {"video_id": "dQw4w9WgXcQ", "author": "Tech Channel", "views": 123456, "likes": 1500}


def test_feed_url_for_channel_and_playlist():
    assert youtube.feed_url({"kind": "channel", "channel_id": "UC1"}) == "https://www.youtube.com/feeds/videos.xml?channel_id=UC1"
    assert youtube.feed_url({"kind": "playlist", "playlist_id": "PL1"}) == "https://www.youtube.com/feeds/videos.xml?playlist_id=PL1"


def test_auto_captions_without_punctuation_are_chunked_into_sentences():
    snippets = [" ".join(["слово"] * 10)] * 9 + ["[Музыка]"]
    text = youtube.captions_to_text(snippets)
    assert "[Музыка]" not in text
    assert text.count(".") == 3  # 90 слов → три куска по 30
    punctuated = youtube.captions_to_text(["Первая мысль.", "Вторая мысль!", "Третья?"])
    assert punctuated == "Первая мысль. Вторая мысль! Третья?"


def test_search_result_becomes_video_only_for_youtube_links():
    video = youtube.search_result_to_video({"url": "https://youtu.be/dQw4w9WgXcQ", "title": "t", "meta": {"engine": "youtube"}})
    assert video["url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert video["image_url"] == "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"
    assert video["meta"] == {"engine": "youtube", "video_id": "dQw4w9WgXcQ"}
    assert youtube.search_result_to_video({"url": "https://vimeo.com/1", "title": "t"}) is None


# ---------- SearXNG ----------
def test_parse_searx_results_dedupes_and_skips_junk():
    data = {"results": [
        {"url": "https://blog.example.com/postgres-tuning", "title": "<b>Postgres</b> tuning", "content": "Shared buffers and work_mem.",
         "publishedDate": "2025-05-01T12:00:00", "img_src": "https://blog.example.com/cover.jpg", "engine": "duckduckgo"},
        {"url": "https://blog.example.com/postgres-tuning", "title": "dup"},
        {"url": "ftp://files.example.com/x", "title": "ftp"},
        {"url": "https://x.example.com/", "title": ""},
    ]}
    [r] = searx.parse_results(data)
    assert r["title"] == "Postgres tuning" and r["publisher_name"] == "blog.example.com"
    assert r["published_at"] == datetime(2025, 5, 1, 12, tzinfo=timezone.utc)
    assert r["image_url"] == "https://blog.example.com/cover.jpg" and r["meta"] == {"engine": "duckduckgo"}


async def test_searx_search_requires_configured_url(monkeypatch):
    monkeypatch.setattr(searx, "get_settings", lambda: SimpleNamespace(searxng_url=""))
    with pytest.raises(searx.SearchUnavailable, match="SEARXNG_URL"):
        await searx.search("python", language="ru")


# ---------- сайт без RSS ----------
SITE = """<html><head>
<link rel="alternate" type="application/rss+xml" href="/feed.xml"><link rel="stylesheet" href="/s.css">
</head><body>
<nav><a href="/blog/how-we-scaled-postgres-to-1m-rps">Навигация: как мы масштабировали Postgres</a></nav>
<main>
<a href="/blog/how-we-scaled-postgres-to-1m-rps">Как мы масштабировали Postgres до миллиона запросов</a>
<a href="/blog/how-we-scaled-postgres-to-1m-rps#comments">Как мы масштабировали Postgres до миллиона запросов</a>
<a href="/blog/2025/05/indexes">Индексы: что нужно знать каждому разработчику</a>
<a href="/tag/postgres">Все материалы с тегом Postgres и базы данных</a>
<a href="/blog">Блог компании про базы данных и не только</a>
<a href="/blog/short">Читать</a>
<a href="https://other.example.org/blog/foreign-article-about-db">Чужая статья про базы данных на другом сайте</a>
<a href="/files/report-2025.pdf">Отчёт о состоянии баз данных за 2025 год</a>
</main></body></html>"""


def test_site_finds_feed_and_article_links():
    assert site.feed_links(SITE, "https://www.example.com/blog") == ["https://www.example.com/feed.xml"]
    links = site.article_links(SITE, "https://www.example.com/blog")
    assert [link["url"] for link in links] == [
        "https://www.example.com/blog/how-we-scaled-postgres-to-1m-rps",
        "https://www.example.com/blog/2025/05/indexes",
    ]
    assert links[0]["title"] == "Как мы масштабировали Postgres до миллиона запросов"


# ---------- Reddit ----------
def test_normalize_subreddit():
    assert reddit.normalize_subreddit("r/MachineLearning") == "MachineLearning"
    assert reddit.normalize_subreddit("https://www.reddit.com/r/Python/top/?t=week") == "Python"
    assert reddit.normalize_subreddit("rust") == "rust"
    with pytest.raises(ValueError):
        reddit.normalize_subreddit("not a sub!")


def test_reddit_listing_url():
    assert reddit.listing_url({"subreddit": "Python", "sort": "top", "period": "month"}) == "https://www.reddit.com/r/Python/top/.rss?t=month"
    assert reddit.listing_url({"subreddit": "Python", "sort": "hot"}) == "https://www.reddit.com/r/Python/hot/.rss"


def _reddit_entry(title: str, slug: str, content: str, thumb: str = "") -> str:
    media = f'<media:thumbnail url="{thumb}"/>' if thumb else ""
    return (f"<entry><author><name>/u/someone</name></author><content type=\"html\">{content}</content>"
            f"<link href=\"https://www.reddit.com/r/x/comments/{slug}/\"/><updated>2026-10-19T15:32:01+00:00</updated>"
            f"<published>2026-10-19T15:32:01+00:00</published><title>{title}</title>{media}</entry>")


def _links(external: str, slug: str) -> str:
    return (f"&#32; submitted by &lt;a href=&quot;https://www.reddit.com/user/someone&quot;&gt; /u/someone &lt;/a&gt; &lt;br/&gt; "
            f"&lt;span&gt;&lt;a href=&quot;{external}&quot;&gt;[link]&lt;/a&gt;&lt;/span&gt; "
            f"&lt;span&gt;&lt;a href=&quot;https://www.reddit.com/r/x/comments/{slug}/&quot;&gt;[comments]&lt;/a&gt;&lt;/span&gt;")


def test_parse_reddit_feed():
    self_post = "&lt;div class=&quot;md&quot;&gt;&lt;p&gt;We have been thinking about caching.&lt;/p&gt;&lt;/div&gt;" + _links("https://www.reddit.com/r/x/comments/s1/", "s1")
    feed = ('<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom" xmlns:media="http://search.yahoo.com/mrss/">'
            + _reddit_entry("Self post", "s1", self_post)
            + _reddit_entry("Link post", "l1", _links("https://blog.example.com/a", "l1"), thumb="https://b.thumbs.redditmedia.com/p.jpg")
            + _reddit_entry("Video post", "v1", _links("https://youtu.be/dQw4w9WgXcQ", "v1"))
            + "</feed>").encode()
    items = reddit.parse_feed(feed, "x")
    assert [(i["title"], i["kind"]) for i in items] == [("Self post", "post"), ("Link post", "article"), ("Video post", "video")]
    assert items[0]["url"] == "https://www.reddit.com/r/x/comments/s1/" and items[0]["text"] == "We have been thinking about caching."
    assert items[1]["url"] == "https://blog.example.com/a" and items[1]["text"] == ""
    assert items[1]["image_url"] == "https://b.thumbs.redditmedia.com/p.jpg"
    assert items[2]["url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert items[1]["meta"] == {"permalink": "https://www.reddit.com/r/x/comments/l1/", "author": "/u/someone"}
    assert items[0]["publisher_name"] == "r/x"
    assert items[0]["published_at"].year == 2026


# ---------- Telegram ----------
TG_PAGE = """<html><body><div class="tgme_channel_info"></div>
<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" data-post="durov/100">
  <a class="tgme_widget_message_photo_wrap" style="width:100%;background-image:url('https://cdn4.telegram-cdn.org/file/abc.jpg')"></a>
  <div class="tgme_widget_message_text">Большое обновление приложения<br>Добавили папки, темы и <b>реакции</b>.</div>
  <span class="tgme_widget_message_views">12.5K</span>
  <time datetime="2025-03-01T10:00:00+00:00"></time>
</div></div>
<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" data-post="durov/101"><a class="tgme_widget_message_photo_wrap"></a></div></div>
</body></html>"""


def test_normalize_telegram_channel():
    assert telegram.normalize_channel("@durov") == "durov"
    assert telegram.normalize_channel("https://t.me/s/durov") == "durov"
    assert telegram.normalize_channel("t.me/durov/123") == "durov"
    with pytest.raises(ValueError):
        telegram.normalize_channel("@ab")


def test_parse_views():
    assert telegram.parse_views("12.5K") == 12500
    assert telegram.parse_views("1,2M") == 1_200_000
    assert telegram.parse_views("987") == 987
    assert telegram.parse_views("") is None


def test_parse_telegram_channel_page():
    [post] = telegram.parse_channel_page(TG_PAGE, "durov")
    assert post["title"] == "Большое обновление приложения"
    assert post["text"] == "Большое обновление приложения\nДобавили папки, темы и реакции."
    assert post["url"] == "https://t.me/durov/100" and post["kind"] == "post"
    assert post["image_url"] == "https://cdn4.telegram-cdn.org/file/abc.jpg"
    assert post["meta"] == {"channel": "durov", "views": 12500}
    assert post["published_at"] == datetime(2025, 3, 1, 10, tzinfo=timezone.utc)


# ---------- режимы и оценка ----------
def test_item_mode_follows_profile_and_source_override():
    assert connectors.item_mode("news", "youtube", {}) == "news"
    assert connectors.item_mode("curated", "rss", {}) == "curated"
    assert connectors.item_mode("mixed", "rss", {}) == "news"
    assert connectors.item_mode("mixed", "telegram", {}) == "news"
    assert connectors.item_mode("mixed", "youtube", {}) == "curated"
    assert connectors.item_mode("mixed", "telegram", {"mode": "curated"}) == "curated"


def test_engagement_uses_best_platform_metric_and_is_unmeasured_without_one():
    assert scoring.engagement([]) is None
    assert scoring.engagement([{"author": "x"}]) is None
    assert scoring.engagement([{"views": 1_000_000}]) == pytest.approx(1.0)
    low, high = scoring.engagement([{"views": 1_000}]), scoring.engagement([{"views": 100_000}])
    assert 0 < low < high < 1
    assert scoring.engagement([{"views": 10}, {"score": 10_000}]) == pytest.approx(1.0)
    assert scoring.engagement([{"views": True}]) is None


def test_curated_score_has_its_own_components_and_marks_missing_engagement_neutral():
    feats, neutral = scoring.compute_curated_features(
        centroid=np.ones(4), authority=0.6, age_hours=0, window_hours=336, topic_vec=None, topic_fit=0.8, metas=[{}], content_length=2000,
    )
    assert neutral == {"engagement"}
    assert feats == {"topic_fit": 0.8, "engagement": 0.5, "depth": 0.5, "authority": 0.6, "freshness": 1.0}
    score, parts = scoring.nws(feats, {"coverage": 1.0}, neutral, defaults=scoring.CURATED_WEIGHTS)
    assert set(parts) == set(scoring.CURATED_WEIGHTS) and parts["engagement"]["neutral"]
    assert score == pytest.approx(0.55 * 0.8 + 0.15 * 0.5 + 0.1 * 0.5 + 0.1 * 0.6 + 0.1 * 1.0, abs=1e-4)


# ---------- форматы постов ----------
PROJECT = {"prompt_template": "", "language": "ru", "tone": "живой", "topic": "базы данных", "max_length": 900}


def test_content_format_prefers_video_for_curated_and_news_otherwise():
    assert content_format("news", ["video"]) == "news"
    assert content_format("curated", ["article", "video"]) == "video"
    assert content_format("curated", ["post"]) == "post"
    assert content_format("curated", []) == "article"


def test_format_prompts_and_custom_template_override():
    assert "расшифровка видео" in render_system(PROJECT, 700, "video")
    assert "пост-разбор" in render_system(PROJECT, 700, "article")
    assert "новостного канала" in render_system(PROJECT, 700, "news")
    assert render_system({**PROJECT, "prompt_template": "Свой шаблон {topic}"}, 700, "video") == "Свой шаблон базы данных"
    blocks = [Block(1, 1, "Tech Channel", "How databases work", "https://youtu.be/x", ["Индексы ускоряют поиск."], T0, kind="video")]
    assert "рекомендацию этого видео" in blocks_prompt(blocks, "ru", 700, fmt="video")
    assert "Объедини источники" in blocks_prompt(blocks, "ru", 700)


def test_curated_auto_mode_uses_model_even_for_single_source():
    blocks = [Block(1, 1, "Tech Channel", "Видео о базах данных", "", ["Индексы ускоряют поиск по таблицам."], T0, kind="video")]
    assert decide_use_llm("auto", blocks, CHAT, "ru", "video") == (True, "llm")
    assert decide_use_llm("auto", blocks, CHAT, "ru", "news") == (False, "extractive")


def test_single_source_curated_post_is_not_blocked_by_citation_coverage():
    body = "Видео подробно объясняет, как работают индексы в базах данных. Автор сравнивает B-деревья и хеш-индексы на реальных примерах. Полезно всем, кто пишет SQL-запросы."
    citations = [{"n": 1, "url": "https://youtu.be/x", "source": "Tech", "content_length": 5000}]
    checks = {"invalid_citations": [], "citation_coverage": 0.0, "length": len(body)}
    assert automatic_review("Как работают индексы", body, citations, checks, 900, fmt="video")["passed"]
    news = automatic_review("Как работают индексы", body, citations, checks, 900)
    assert any("Покрытие" in reason for reason in news["reasons"])



# ---------- автоподбор источников ----------
def test_parse_discovery_queries():
    from app.pipeline.discovery import parse_queries

    raw = 'Вот запросы: ["индексы PostgreSQL", "репликация  баз данных", "индексы postgresql", "ab", 42]'
    assert parse_queries(raw) == ["индексы PostgreSQL", "репликация баз данных"]  # дубль, короткое и число отброшены
    assert parse_queries("не JSON") == []
    assert len(parse_queries(json.dumps([f"запрос номер {i}" for i in range(20)]))) == 8


def test_collect_groups_search_results_into_source_candidates():
    from app.pipeline.discovery import collect

    results = {
        "индексы postgres": [
            {"url": "https://www.habr.com/ru/articles/1/", "title": "Индексы в PostgreSQL"},
            {"url": "https://youtu.be/dQw4w9WgXcQ", "title": "Видео про индексы", "meta": {"author": "DB Channel"}},
            {"url": "https://t.me/dbweekly/120", "title": "Пост про индексы"},
            {"url": "https://www.reddit.com/r/PostgreSQL/comments/abc/x/", "title": "Обсуждение индексов"},
            {"url": "https://ru.wikipedia.org/wiki/Индекс", "title": "Индекс — Википедия"},
            {"url": "https://www.youtube.com/watch?v=aircAruvnKk", "title": "Без автора"},
        ],
        "репликация": [
            {"url": "https://habr.com/ru/articles/2/", "title": "Репликация"},
            {"url": "https://www.youtube.com/watch?v=fNk_zzaMoSs", "title": "Видео про репликацию - YouTube", "meta": {"author": "db channel"}},
        ],
    }
    found, videos = collect(results)
    assert set(found) == {("site", "https://habr.com/"), ("telegram", "@dbweekly"), ("reddit", "r/PostgreSQL")}
    habr = found[("site", "https://habr.com/")]
    assert habr.hits == 2 and habr.samples == ["Индексы в PostgreSQL", "Репликация"]
    assert found[("telegram", "@dbweekly")].hits == 1
    # видео — отдельно и в каноническом виде; канал узнаётся потом через oEmbed, автору из выдачи не доверяем
    assert videos == [
        ("индексы postgres", "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "Видео про индексы"),
        ("индексы postgres", "https://www.youtube.com/watch?v=aircAruvnKk", "Без автора"),
        ("репликация", "https://www.youtube.com/watch?v=fNk_zzaMoSs", "Видео про репликацию"),
    ]


async def test_youtube_channels_group_videos_by_owner(monkeypatch):
    from app.pipeline import discovery

    owners = {
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ": ("https://www.youtube.com/@db", "DB Channel"),
        "https://www.youtube.com/watch?v=fNk_zzaMoSs": ("https://www.youtube.com/@db", "DB Channel"),
        "https://www.youtube.com/watch?v=aircAruvnKk": None,  # oEmbed не ответил — видео пропускаем
    }

    async def video_channel(url):
        return owners[url]

    monkeypatch.setattr(discovery, "video_channel", video_channel)
    [channel] = await discovery._youtube_channels([
        ("a", "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "Первое"),
        ("b", "https://www.youtube.com/watch?v=fNk_zzaMoSs", "Второе"),
        ("b", "https://www.youtube.com/watch?v=aircAruvnKk", "Третье"),
    ])
    assert (channel.value, channel.name, channel.hits, channel.samples) == ("https://www.youtube.com/@db", "DB Channel", 2, ["Первое", "Второе"])


def test_candidate_score_combines_repetition_and_model_rating():
    from app.pipeline.discovery import Candidate

    c = Candidate("site", "https://habr.com/", "habr.com", queries={"a", "b", "c", "d"})
    assert c.score == 1.0
    c.rating = 5
    assert c.score == pytest.approx(0.4 + 0.3)
    assert Candidate("site", "x", "x", queries={"a"}).score == pytest.approx(1 / 3, abs=1e-4)


def test_parse_discovery_ratings():
    from app.pipeline.discovery import parse_ratings

    raw = 'Оценки: {"1": {"score": 8, "reason": "Профильный блог"}, "2": 3, "x": {"score": 9}, "3": {"score": "плохо"}}'
    assert parse_ratings(raw) == {1: (8.0, "Профильный блог"), 2: (3.0, "")}
    assert parse_ratings("[]") == {}


def test_suggestions_are_capped_per_source_type():
    from app.pipeline.discovery import MAX_PER_TYPE, Candidate, cap_per_type

    channels = [Candidate("youtube", f"https://www.youtube.com/@c{i}", f"c{i}", queries={"q"}) for i in range(25)]
    sites = [Candidate("site", f"https://s{i}.example/", f"s{i}", queries={"q", "r"}) for i in range(3)]
    capped = cap_per_type(channels + sites)
    assert [c.type for c in capped[:3]] == ["site"] * 3  # чаще встречающиеся — первыми
    assert sum(c.type == "youtube" for c in capped) == MAX_PER_TYPE


def test_parse_ratings_recovers_entries_from_truncated_json():
    from app.pipeline.discovery import parse_ratings

    raw = ('{\n  "1": {"score": 9, "reason": "Профильный ресурс по \\"PostgreSQL\\""},\n'
           '  "2": {"score": 0, "reason": "Магазин"},\n  "3": {"score": 7, "reason": "Активное сообщ')
    assert parse_ratings(raw) == {1: (9.0, 'Профильный ресурс по \\"PostgreSQL\\"'), 2: (0.0, "Магазин"), 3: (7.0, "")}
