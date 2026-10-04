from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

from app.pipeline import scoring
from app.pipeline.clustering_core import Cluster, OnlineClusterer
from app.pipeline.context import Block
from app.pipeline.generate import automatic_review, check_text, decide_use_llm, extractive_text, gate_auto_publish, normalize_generated_post, parse_output
from app.api.sources import parse_opml
from app.publisher import classify_exception, classify_telegram, render_telegram_html, sources_footer
from app.providers import LLMUnavailable, OpenAICompatProvider, _provider_error_detail
from app.textutil import normalize_url, numbers_in, split_sentences, strip_citations, strip_html, strip_source_footer, truncate

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def vec(*xs):
    return np.array(xs, dtype=np.float32)


# ---------- кластеризация ----------
def test_online_clusterer_groups_similar_and_splits_different():
    c = OnlineClusterer(0.8, timedelta(hours=48))
    a1, _ = c.add(vec(1, 0.1, 0), T0)
    a2, _ = c.add(vec(0.9, 0.2, 0), T0 + timedelta(hours=1))
    b1, _ = c.add(vec(0, 0, 1), T0 + timedelta(hours=2))
    assert a1 is a2 and a1 is not b1
    assert a1.n == 2 and len(c.clusters) == 2


def test_online_clusterer_respects_window():
    c = OnlineClusterer(0.8, timedelta(hours=10))
    first, _ = c.add(vec(1, 0, 0), T0)
    second, _ = c.add(vec(1, 0, 0), T0 + timedelta(hours=30))
    assert first is not second  # тот же вектор, но кластер вышел из окна


def test_search_result_threshold_splits_loosely_related_articles():
    from app.pipeline.process import SEARCH_SIM_THRESHOLD

    c = OnlineClusterer(SEARCH_SIM_THRESHOLD, timedelta(hours=48))
    first, _ = c.add(vec(1, 0), T0)
    second, similarity = c.add(vec(0.8, 0.6), T0 + timedelta(hours=1))
    assert similarity == pytest.approx(0.8)
    assert first is not second


def test_cluster_roundtrip_through_db_representation():
    c = OnlineClusterer(0.5, timedelta(hours=48))
    cl, _ = c.add(vec(1, 0), T0)
    c.add(vec(0.8, 0.6), T0 + timedelta(hours=1))
    restored = Cluster.from_db(1, cl.centroid, cl.n, cl.first_seen, cl.last_seen)
    assert np.allclose(restored.sum, cl.sum, atol=1e-5)
    restored_clusterer = OnlineClusterer(0.5, timedelta(hours=48), [restored])
    joined, _ = restored_clusterer.add(vec(0.9, 0.4), T0 + timedelta(hours=2))
    assert joined is restored and restored.n == 3


# ---------- скоринг ----------
def _features(**over):
    base = dict(centroid=vec(1, 0), source_count=4, authority=0.7, age_hours=2, recent_items=3, window_hours=48, topic_vec=vec(1, 0))
    base.update(over)
    return scoring.compute_features(**base)


def test_nws_components_and_breakdown():
    score, parts = scoring.nws(_features(), scoring.DEFAULT_WEIGHTS)
    assert set(parts) == set(scoring.COMPONENTS)
    assert abs(score - sum(p["contribution"] for p in parts.values())) < 1e-3
    assert "redundancy" not in parts


def test_topic_fit_is_the_dominant_score_weight():
    assert scoring.DEFAULT_WEIGHTS["topic_fit"] > sum(weight for name, weight in scoring.DEFAULT_WEIGHTS.items() if name != "topic_fit")


def test_topic_threshold_rejects_low_fit_but_keeps_neutral_and_disabled():
    assert not scoring.topic_passes_threshold({"value": 0.3791}, 0.4)
    assert scoring.topic_passes_threshold({"value": 0.4}, 0.4)
    assert scoring.topic_passes_threshold({"value": 0.5, "neutral": True}, 0.8)
    assert scoring.topic_passes_threshold({"value": 0.1}, 0)


def test_freshness_halves_every_half_life():
    assert abs(scoring.freshness(12, 12) - 0.5) < 1e-9
    assert scoring.freshness(0, 12) == 1.0


def test_weights_override_and_ignore_unknown():
    s1, _ = scoring.nws(_features(), {"coverage": 1.0, "bogus": 5})
    s2, _ = scoring.nws(_features(), {"coverage": 0.0})
    assert s1 > s2


# ---------- публикатор ----------
def test_classify_telegram():
    assert classify_telegram(200, {"ok": True, "result": {"message_id": 42}}).external_id == "42"
    r = classify_telegram(429, {"parameters": {"retry_after": 7}})
    assert r.kind == "retry" and r.retry_after == 7
    assert classify_telegram(400, {"description": "chat not found"}).kind == "failed"
    assert classify_telegram(502, {}).kind == "unknown"  # запрос мог дойти — автоповтор небезопасен


def test_classify_exception_distinguishes_safe_retry_from_unknown():
    assert classify_exception(httpx.ConnectError("boom")).kind == "retry"
    assert classify_exception(httpx.ConnectTimeout("boom")).kind == "retry"
    assert classify_exception(httpx.ReadTimeout("boom")).kind == "unknown"
    assert classify_exception(httpx.RemoteProtocolError("boom")).kind == "unknown"
    assert classify_exception(RuntimeError("bug")).kind == "unknown"


def test_provider_error_detail_includes_read_timeout_limit_and_http_body():
    timeout = httpx.ReadTimeout("", request=httpx.Request("POST", "http://ollama/v1/chat/completions"))
    assert _provider_error_detail(timeout, 180) == "ReadTimeout: модель не ответила за 180 с (LLM_TIMEOUT)"

    request = httpx.Request("POST", "http://ollama/v1/chat/completions")
    response = httpx.Response(400, json={"error": "model context too large"}, request=request)
    error = httpx.HTTPStatusError("bad request", request=request, response=response)
    assert "HTTP 400" in _provider_error_detail(error, 180)
    assert "model context too large" in _provider_error_detail(error, 180)


async def test_chat_generation_can_be_disabled_while_embeddings_remain_configured(monkeypatch):
    settings = SimpleNamespace(
        llm_chat_enabled=False,
        llm_model="qwen2.5:7b-instruct",
        embed_model="bge-m3",
        llm_timeout=1,
        llm_base_url="http://ollama:11434/v1",
        llm_api_key="ollama",
        embed_base_url="",
        embed_api_key="",
    )
    monkeypatch.setattr("app.providers.get_settings", lambda: settings)
    provider = OpenAICompatProvider()
    try:
        assert provider.supports_chat is False
        assert provider._embed_client is provider._client
        with pytest.raises(LLMUnavailable, match="LLM_CHAT_ENABLED=true"):
            await provider.chat("system", "user")
    finally:
        await provider._client.aclose()


def test_render_telegram_escapes_and_limits():
    cites = [{"n": 1, "url": 'https://e.com/?a="1"&b=2', "source": "S<1>"}]
    html = render_telegram_html("A [1] <b>&", "x < y & z " * 600, cites, limit=1000)
    assert "&lt;b&gt;" in html and len(html) <= 1000
    assert "[1]" not in html
    assert 'href="https://e.com/?a=&quot;1&quot;&amp;b=2"' in html and "S&lt;1&gt;" in html


def test_published_post_has_no_citation_markers_but_has_source_links():
    cites = [{"n": 1, "url": "https://a.test/1", "source": "ТехДень"}, {"n": 2, "url": "https://b.test/2", "source": "ТехДень"},
             {"n": 3, "url": "https://c.test/3", "source": "Облачный дозор"}]
    html = render_telegram_html("Заголовок", "Первый факт. [1][2] Второй факт [3].\n\nИсточники:\nТехДень, 27.09 17:57", cites)
    assert "[1]" not in html and "[3]" not in html and "Первый факт. Второй факт." in html
    assert 'Источники: <a href="https://a.test/1">ТехДень</a>, <a href="https://b.test/2">ТехДень</a>, <a href="https://c.test/3">Облачный дозор</a>' in html


def test_sources_footer_skips_citations_without_url():
    assert sources_footer([{"n": 1, "source": "Без ссылки", "url": ""}]) == ""
    assert sources_footer([]) == ""


def test_strip_source_footer_removes_model_generated_reference_list():
    text = "Текст поста [1].\n\nИсточники:\nhabr, 27.09 17:57\nкоммерсант, 26.09 22:34"
    assert strip_source_footer(text) == "Текст поста [1]."


def test_strip_citations():
    assert strip_citations("Факт. [1][2] Следующий.") == "Факт. Следующий."
    assert strip_citations("Факт [1].") == "Факт."
    assert strip_citations("1. Заголовок\nТекст [3][4]. Ещё [5]\n\n2. Второй") == "1. Заголовок\nТекст. Ещё\n\n2. Второй"  # нумерация списка не пострадала
    assert strip_citations("Массив a[0] остаётся, как и b[1]") == "Массив a[0] остаётся, как и b[1]"  # индекс в тексте — не ссылка
    assert strip_citations("Ключ[1] и [2] тоже", {1, 2}) == "Ключ и тоже"  # номера из источников черновика удаляются даже вплотную


# ---------- проверки генерации ----------
def test_check_text_flags_bad_citations_and_coverage():
    cites = [{"n": 1, "title": "Тариф вырос на 15%", "excerpt": "Цена вырастет на 15% с 1 октября."}]
    ok = check_text("Тариф вырастет на 15% [1].", cites)
    assert ok["invalid_citations"] == [] and ok["citation_coverage"] == 1.0
    bad = check_text("Тариф вырастет на 25% [1]. Есть данные [3].", cites)
    assert bad["invalid_citations"] == [3]
    # числа больше не сверяются с источниками: 25% против 15% замечанием не считается
    assert "unsupported_numbers" not in bad


def test_automatic_review_requires_supported_cited_content_and_length():
    citations = [{"n": 1, "title": "Тарифы", "excerpt": "С 1 октября тариф вырастет на 15%."}]
    body = "Тариф вырастет на 15% [1]. Это подтверждают опубликованные сведения [1]. Изменение вступит в силу согласно сообщению источника [1]."
    checks = check_text(body, citations)
    assert automatic_review("Тарифы изменятся", body, citations, checks, 200)["passed"]

    invalid = check_text("Тариф вырастет на 15% [1][2].", citations)
    assert not automatic_review("Тарифы изменятся", "Тариф вырастет на 15% [1][2].", citations, invalid, 100)["passed"]
    uncited = check_text("Тариф вырастет на 15%.", citations)
    assert "Покрытие предложений ссылками на источники ниже 50%" in automatic_review("Тарифы изменятся", "Тариф вырастет на 15%.", citations, uncited, 100)["reasons"]
    assert "Превышен лимит длины (5 символов)" in automatic_review("Тарифы изменятся", body, citations, checks, 5)["reasons"]


def test_automatic_review_blocks_short_unenriched_and_wrong_language_posts():
    failed_short_source = [{"n": 1, "title": "Бангкок", "excerpt": "Бангкок", "article_content_status": "failed", "content_length": 12}]
    short = automatic_review("Бангкок", "Бангкок [1]", failed_short_source, {}, 900)
    assert "Текст слишком короткий (меньше 80 символов)" in short["reasons"]
    assert "Полный текст короткой RSS-заметки не удалось получить" in short["reasons"]
    assert gate_auto_publish(short, "full_auto") is False

    english = "The government announced a major economic policy update today. " * 2
    wrong_language = automatic_review("Update", english, [{"n": 1, "excerpt": english}], {}, 900)
    assert any(reason.startswith("Текст, вероятно, не на заданном языке") for reason in wrong_language["reasons"])
    assert gate_auto_publish(wrong_language, "full_auto") is False


def test_automatic_review_does_not_block_multisource_post_for_one_short_rss_source():
    citations = [
        {"n": 1, "title": "Короткая заметка", "excerpt": "Короткий анонс.", "article_content_status": "failed", "content_length": 12},
        {"n": 2, "title": "Полная статья", "excerpt": "Подробный текст статьи." * 20, "article_content_status": "done", "content_length": 1200},
    ]
    body = "Подробный материал подтверждён источниками и описывает основные факты достаточно полно. [1][2]"
    review = automatic_review(
        "Подробный материал",
        body,
        citations,
        {"citation_coverage": 1.0},
        900,
    )
    assert "Полный текст короткой RSS-заметки не удалось получить" not in review["reasons"]


async def test_manual_schedule_run_does_not_force_editor_review(monkeypatch):
    from app.api import schedules

    captured = {}

    async def get_schedule(_sql, _params):
        return {"project_id": 7, "kind": "post", "top_n": 1}

    async def enqueue(_kind, payload):
        captured.update(payload)
        return 123

    monkeypatch.setattr(schedules.db, "fetchone", get_schedule)
    monkeypatch.setattr(schedules.jobs, "enqueue", enqueue)
    result = await schedules.run_schedule(9, {"id": 1})

    assert result == {"job_id": 123}
    assert captured == {"project_id": 7, "kind": "post", "top_n": 1, "user_id": 1}


def test_gate_auto_publish_modes():
    passed = {"passed": True, "reasons": []}
    failed = {"passed": False, "reasons": ["Пустой текст"]}
    # «auto» публикует только прошедшие проверки
    assert gate_auto_publish(passed, "auto") is True
    assert gate_auto_publish(failed, "auto") is False
    # «full_auto» пропускает обычные замечания, но не критические проблемы качества.
    assert gate_auto_publish(passed, "full_auto") is True
    assert gate_auto_publish({"passed": False, "reasons": ["Есть числа, не найденные в источниках"]}, "full_auto") is True
    assert gate_auto_publish(failed, "full_auto") is False
    # «review» никогда не участвует в этой ветке, но функция всё равно детерминирована
    assert gate_auto_publish(passed, "review") is True
    assert gate_auto_publish(failed, "review") is False


def test_parse_output_extracts_title():
    t, b = parse_output("## Заголовок: Новый релиз\n\nТекст поста [1].")
    assert t == "Новый релиз" and b == "Текст поста [1]."


def test_generated_post_uses_one_clean_headline_and_moves_extra_sentences_to_body():
    title = (
        "Nvidia запустила платформу Open Agent Safety Platform для предотвращения атак "
        "со стороны ИИ-агентов [1][2]. "
        "Эта платформа позволяет разработчикам задавать ограничения для ИИ-агентов [1]. "
        "Nvidia утверждает, что платформа могла предотвратить инцидент с Hugging Face [2]."
    )
    body = "По данным источников, система проверяет ограничения во время выполнения задач [2]."

    headline, result_body = normalize_generated_post(title, body)

    assert headline == (
        "Nvidia запустила платформу Open Agent Safety Platform для предотвращения атак "
        "со стороны ИИ-агентов."
    )
    assert len(split_sentences(headline)) == 1
    assert "[1]" not in headline and "[2]" not in headline
    assert "Эта платформа позволяет разработчикам задавать ограничения для ИИ-агентов [1]." in result_body
    assert "Nvidia утверждает, что платформа могла предотвратить инцидент с Hugging Face [2]." in result_body
    assert body in result_body


def test_generated_post_shortens_long_headline_and_preserves_original_in_body():
    long_title = "Новый инструмент компании позволяет разработчикам защищать автономные системы от атак и контролировать их действия."

    headline, body = normalize_generated_post(long_title, "Подробности ниже.", max_headline_chars=80)

    assert len(headline) <= 120
    assert headline.endswith("…")
    assert long_title in body
    assert "Подробности ниже." in body


# ---------- утилиты ----------
def test_textutil():
    assert strip_html("<p>Привет&nbsp;<b>мир</b></p>") == "Привет мир"
    # ячейки таблицы не должны склеиваться без разделителя
    table = strip_html("<table><tr><td>1 - Python</td><td>5 - C#</td></tr></table>")
    assert "Python" in table and "C#" in table and "Python5" not in table.replace(" ", "")
    assert normalize_url("HTTPS://Example.com/a/?utm_source=x&id=5#frag") == "https://example.com/a?id=5"
    assert numbers_in("Выросло на 1 200 рублей и 15,5%") == {"1200", "15.5"}
    assert len(split_sentences("Первое предложение. Второе предложение! Третье?")) == 3
    assert len(truncate("Слово. " * 100, 50)) <= 51


def test_decide_use_llm_auto():
    class P:
        supports_chat = True

    short = [Block(1, 1, "habr", "TIOBE", "", ["Fortran на 11 месте."], T0)]
    assert decide_use_llm("auto", short, P()) == (False, "extractive")
    assert decide_use_llm("llm", short, P()) == (True, "llm")
    assert decide_use_llm("extractive", short, P()) == (False, "extractive")
    multi = [
        Block(1, 1, "A", "t", "", ["Факт один про событие."], T0),
        Block(2, 2, "B", "t", "", ["Факт два про то же событие."], T0),
    ]
    assert decide_use_llm("auto", multi, P()) == (True, "llm")
    same_source = [
        Block(1, 1, "A", "t", "", ["Факт один."], T0),
        Block(2, 2, "A", "t", "", ["Факт два."], T0),
    ]
    assert decide_use_llm("auto", same_source, P()) == (False, "extractive")
    long = [Block(1, 1, "A", "t", "", ["x" * 700], T0)]
    assert decide_use_llm("auto", long, P()) == (False, "extractive")


def test_default_settings_use_groq_chat_and_local_embeddings(monkeypatch):
    from app.config import Settings

    for key in ("LLM_PROVIDER", "LLM_CHAT_ENABLED", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "EMBED_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    settings = Settings(_env_file=None)
    assert settings.llm_provider == "openai"
    assert settings.llm_chat_enabled is True
    assert settings.llm_base_url == "https://api.groq.com/openai/v1"
    assert settings.llm_model == "qwen/qwen3.8-27b"
    assert settings.embed_base_url == "http://ollama:11434/v1"


def test_language_mismatch_forces_translation_even_for_single_source_and_extractive_mode():
    class P:
        supports_chat = True

    english = [Block(1, 1, "source", "OpenAI tests a new always-on assistant.", "", [
        "The assistant may help users manage their email accounts and organize incoming messages.",
    ], T0)]
    assert decide_use_llm("auto", english, P(), "ru") == (True, "llm")
    assert decide_use_llm("extractive", english, P(), "ru") == (True, "llm")
    assert decide_use_llm("auto", english, P(), "en") == (False, "extractive")


def test_parse_opml():
    xml = """<?xml version="1.0"?>
    <opml version="2.0"><body>
      <outline text="News" title="News" xmlUrl="https://example.com/rss"/>
      <outline text="Dup" xmlUrl="https://example.com/rss"/>
      <outline text="Folder"><outline text="Inner" xmlUrl="https://habr.com/rss"/></outline>
      <outline text="NoUrl"/>
    </body></opml>"""
    feeds = parse_opml(xml)
    assert len(feeds) == 2
    assert feeds[0]["url"] == "https://example.com/rss"
    assert feeds[1]["url"] == "https://habr.com/rss"


def test_extractive_text_merges_duplicate_facts_and_cites_all_sources():
    fact = "Затронуты около 120 тысяч учётных записей."
    blocks = [
        Block(1, 10, "А", "т", "", [fact, "Компания сбросила пароли."], T0),
        Block(2, 11, "Б", "т", "", [fact], T0),
        Block(3, 12, "В", "т", "", ["Причиной назвали ошибку в настройке хранилища."], T0),
    ]
    text = extractive_text(blocks, 900)
    assert text.count("120 тысяч") == 1  # один и тот же факт не повторяется
    assert f"{fact} [1][2]" in text  # но подтверждён обоими источниками
    assert "[3]" in text


def test_extractive_text_keeps_later_facts_after_duplicate_lead():
    block = Block(
        1,
        10,
        "А",
        "т",
        "",
        [
            "OpenAI тестирует нового помощника для работы с почтой.",
            "OpenAI тестирует нового помощника для работы с почтой.",
            "Пользователи заметили упоминание функции в тарифе Pro.",
            "В конфигурации найдены параметры почтового помощника.",
        ],
        T0,
    )
    text = extractive_text([block], 900)
    assert text.count("OpenAI тестирует") == 1
    assert "упоминание функции в тарифе Pro" in text
    assert "параметры почтового помощника" in text


# ---------- картинки ----------
def test_google_news_search_url_uses_project_language_and_encodes_query():
    from urllib.parse import parse_qs, urlsplit

    from app.pipeline.ingest import google_news_search_url

    url = google_news_search_url('"robotics" site:arxiv.org', "ru-RU")
    parsed = urlsplit(url)
    params = parse_qs(parsed.query)
    assert parsed.scheme == "https" and parsed.netloc == "news.google.com"
    assert parsed.path == "/rss/search"
    assert params == {"q": ['"robotics" site:arxiv.org'], "hl": ["ru"], "gl": ["RU"], "ceid": ["RU:ru"]}


def test_parse_google_news_feed_keeps_publisher():
    from app.pipeline.ingest import parse_feed

    feed = b"""<?xml version="1.0"?><rss version="2.0"><channel><item>
      <title>Article title</title><link>https://news.google.com/rss/articles/example</link>
      <source url="https://example.com">Example News</source>
    </item></channel></rss>"""
    assert parse_feed(feed)[0]["publisher_name"] == "Example News"


def test_parse_feed_extracts_image_from_media_enclosure_and_body():
    from app.pipeline.ingest import parse_feed

    xml = """<?xml version="1.0"?><rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel><title>t</title>
    <item><title>A</title><link>https://s.test/a</link><description>x</description><media:content url="https://cdn.test/a.jpg" medium="image"/></item>
    <item><title>B</title><link>https://s.test/b</link><description>x</description><enclosure url="https://cdn.test/b.png" type="image/png" length="1"/></item>
    <item><title>C</title><link>https://s.test/c</link><description>&lt;img src="/img/c.webp" width="600"&gt; текст</description></item>
    <item><title>D</title><link>https://s.test/d</link><description>&lt;img src="https://s.test/pixel.gif" width="1"&gt;&lt;img src="/logo.png"&gt; текст</description></item>
    </channel></rss>""".encode()
    urls = {e["title"]: e["image_url"] for e in parse_feed(xml)}
    assert urls == {"A": "https://cdn.test/a.jpg", "B": "https://cdn.test/b.png", "C": "https://s.test/img/c.webp", "D": None}


def test_normalize_image_url_rejects_unsafe_and_junk():
    from app.images import normalize_image_url

    assert normalize_image_url("javascript:alert(1)") is None
    assert normalize_image_url("data:image/png;base64,AAAA") is None
    assert normalize_image_url("https://x.test/icon.svg") is None
    assert normalize_image_url("https://x.test/" + "a" * 1100 + ".jpg") is None
    assert normalize_image_url("/pics/1.jpg", "https://x.test/post") == "https://x.test/pics/1.jpg"
    # логотипы и карточки для соцсетей — оформление сайта, а не картинка статьи
    assert normalize_image_url("https://tass.com/img/blocks/common/tass_logo_share_eng.png") is None
    assert normalize_image_url("https://cdnstatic.rg.ru/images/rg-social-dummy-logo-650x360.jpg") is None
    assert normalize_image_url("https://cdn.test/opengraph-cover.jpg") is None


def test_image_dimensions_reads_headers_of_known_formats():
    import struct

    from app.images import image_dimensions

    png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", 800, 600)
    assert image_dimensions(png) == (800, 600)

    gif = b"GIF89a" + struct.pack("<HH", 320, 240)
    assert image_dimensions(gif) == (320, 240)

    # JPEG: SOI, сегмент APP0, затем SOF0 с высотой 480 и шириной 640
    jpeg = (
        b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 16) + b"\x00" * 14
        + b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", 480, 640) + b"\x03" + b"\x00" * 9
    )
    assert image_dimensions(jpeg) == (640, 480)

    # WebP (расширенный формат VP8X): холст 1200x630 хранится как размер минус один
    webp = b"RIFF" + struct.pack("<I", 30) + b"WEBP" + b"VP8X" + struct.pack("<I", 10) + b"\x00" * 4 + (1199).to_bytes(3, "little") + (629).to_bytes(3, "little")
    assert image_dimensions(webp) == (1200, 630)

    assert image_dimensions(b"not an image") is None


async def test_image_is_downloaded_once_and_served_from_disk_cache(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app import images

    monkeypatch.setattr(images, "get_settings", lambda: SimpleNamespace(image_cache_dir=str(tmp_path)))
    calls = 0

    async def download(_url):
        nonlocal calls
        calls += 1
        return b"fake-image", "image/png"

    monkeypatch.setattr(images, "download_image", download)
    url = "https://cache-test.invalid/image.png"
    assert await images.get_image(url) == (b"fake-image", "image/png")
    assert await images.get_image(url) == (b"fake-image", "image/png")
    assert calls == 1


async def test_pick_cover_image_skips_site_defaults_and_small_images(monkeypatch):
    from app.pipeline import generate

    async def defaults(urls):
        return {"https://a.test/default.png"} if "https://a.test/default.png" in urls else set()

    async def quality(url):
        return {"https://a.test/small.jpg": False}.get(url)  # None — размер неизвестен, картинку не выбрасываем

    monkeypatch.setattr(generate, "default_image_urls", defaults)
    monkeypatch.setattr(generate, "_cover_ok", quality)

    citations = [
        {"image_url": "https://a.test/default.png"},  # дефолтная картинка сайта
        {"image_url": "https://a.test/small.jpg"},    # слишком мелкая
        {"image_url": "https://a.test/good.jpg"},     # годится
    ]
    assert await generate.pick_cover_image(citations) == "https://a.test/good.jpg"
    assert await generate.pick_cover_image([{"image_url": "https://a.test/default.png"}]) is None
    assert await generate.pick_cover_image([{"n": 1}]) is None
    # недоступный для проверки источник не наказываем: обложкой станет его картинка
    assert await generate.pick_cover_image([{"image_url": "https://a.test/unknown.jpg"}]) == "https://a.test/unknown.jpg"


async def test_drop_default_images_clears_citation_pictures(monkeypatch):
    from app.pipeline import generate

    async def defaults(urls):
        return {"https://a.test/default.png"}

    monkeypatch.setattr(generate, "default_image_urls", defaults)
    citations = [{"image_url": "https://a.test/default.png"}, {"image_url": "https://a.test/photo.jpg"}]
    assert await generate.drop_default_images(citations) == {"https://a.test/default.png"}
    assert [c["image_url"] for c in citations] == [None, "https://a.test/photo.jpg"]


def test_source_dump_detector_flags_sequential_retelling_but_not_synthesis():
    from app.pipeline.generate import looks_like_source_dump

    def block(n, text):
        return Block(n=n, item_id=n, source=f"S{n}", title=f"T{n}", url="", sentences=[text], published_at=T0)

    blocks = [
        block(1, "Хакеры взломали базу данных Пентагона и получили данные более трёх миллионов человек."),
        block(2, "Злоумышленники девять месяцев имели доступ к системе учёта сотрудников Минобороны США."),
    ]
    dump = "[1] Хакеры взломали базу данных Пентагона и получили данные более трёх миллионов человек.\n\n[2] Злоумышленники девять месяцев имели доступ к системе учёта сотрудников Минобороны США."
    merged = "Хакеры почти девять месяцев имели доступ к кадровой базе Минобороны США, утекли данные миллионов людей [1][2]."
    assert looks_like_source_dump(dump, blocks) is True
    assert looks_like_source_dump(merged, blocks) is False


def test_interest_rating_parsing_and_review_gate():
    from app.pipeline.interest import parse_rating
    from app.pipeline.generate import gate_auto_publish

    assert parse_rating('Вот ответ: {"score": 12, "reason": "важно"}') == (10.0, "важно")
    assert parse_rating("Оценка 4") == (4.0, "")
    assert parse_rating("не знаю") is None
    review = {"passed": False, "reasons": ["Модель сочла сюжет малоинтересным (3 из 10)"]}
    assert gate_auto_publish(review, "full_auto") is False


async def test_post_with_retry_retries_transient_gateway_errors(monkeypatch):
    import httpx
    from app import providers

    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503 if len(calls) < 3 else 200, json={"ok": True})

    async def no_sleep(_):
        return None

    monkeypatch.setattr(providers.asyncio, "sleep", no_sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://x") as client:
        r = await providers._post_with_retry(client, "/p", {})
    assert r.status_code == 200 and len(calls) == 3


def test_worker_lanes_reserve_ingest_and_generate_slots():
    from app.worker import plan_lanes

    assert plan_lanes(1) == [(None, None)]
    # при двух воркерах сбор лент получает отдельную полосу и не ждёт обработку проектов
    assert plan_lanes(2) == [(("ingest_source", "enrich_articles"), None), (None, None)]
    # при трёх и больше — плюс отдельная полоса генерации, общие не берут generate
    lanes = plan_lanes(3)
    assert lanes[0] == (("ingest_source", "enrich_articles"), None)
    assert lanes[1] == (("generate",), None)
    assert lanes[2] == (None, ("generate",))
    assert plan_lanes(5) == [*lanes, (None, ("generate",)), (None, ("generate",))]


def test_parse_topic_aspects_splits_and_dedupes():
    from app.pipeline.process import parse_topic_aspects

    assert parse_topic_aspects("Технологии: базы данных, облака; ИИ. ии") == ["Технологии", "базы данных", "облака", "ИИ"]
    assert parse_topic_aspects("") == []


def test_max_aspect_similarity_takes_closest_aspect():
    from app.pipeline.process import max_aspect_similarity

    items = np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    aspects = np.asarray([[0.0, 1.0], [1.0, 0.0]], dtype=np.float32)
    assert max_aspect_similarity(items, aspects).tolist() == [1.0, 1.0]

    # заголовок ближе ко второму аспекту (0.6), а не к первому (0.0)
    single = max_aspect_similarity(np.asarray([[1.0, 0.0]], dtype=np.float32), np.asarray([[0.0, 1.0], [0.6, 0.8]], dtype=np.float32))
    assert abs(float(single[0]) - 0.6) < 1e-6