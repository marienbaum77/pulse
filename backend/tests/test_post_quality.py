"""Качество постов: разметка, первичная ссылка из статьи, контекст одиночного сюжета, подвал источников,
подпись канала и свои критерии интересности."""
import pytest

from app import db
from app.markup import keep_links, to_plain, to_telegram_html, visible
from app.pipeline import interest, primary_links
from app.pipeline.connectors import telegram
from app.pipeline.context import CURATED_CONTEXT_SENTENCES, LONGREAD_CHARS, SINGLE_CONTEXT_SENTENCES, build_blocks, spread_sentences
from app.pipeline.generate import automatic_review, fit_title, gate_auto_publish, generate_for_clusters, run_generation
from app.pipeline.ingest import import_manual, insert_items, parse_feed
from app.pipeline.process import get_project, process_project
from app.providers import StubProvider
from app.publisher import _post_options, render_telegram_caption, render_telegram_html, render_vk_text
from app.textutil import list_items, promised_count, utcnow

from .helpers import make_project

CITES = [{"n": 1, "url": "https://habr.com/ru/news/1/", "source": "habr"}]

def test_markup_converts_bold_and_links_for_each_platform():
    text = 'Модель на **816 млрд** параметров <лучше> & быстрее. Веса 👉 [Muse](https://huggingface.co/m/muse?a=1&b=2)'
    tg = to_telegram_html(text)
    assert "<b>816 млрд</b>" in tg and "&lt;лучше&gt; &amp; быстрее" in tg
    assert '<a href="https://huggingface.co/m/muse?a=1&amp;b=2">Muse</a>' in tg
    assert to_plain(text).endswith("Веса 👉 Muse (https://huggingface.co/m/muse?a=1&b=2)")
    assert "**" not in to_plain(text) and visible(text).endswith("Веса 👉 Muse")
    assert to_telegram_html("2 ** 3 = 8, a**b") == "2 ** 3 = 8, a**b"  # звёздочки без пары и с пробелами — не разметка

def test_keep_links_drops_invented_urls():
    text = "Читайте [тут](https://fake.example/x) и [там](https://github.com/a/b)."
    assert keep_links(text, {"https://github.com/a/b"}) == "Читайте тут и [там](https://github.com/a/b)."

@pytest.mark.parametrize(
    ("url", "anchor", "expected"),
    [
        ("https://github.com/vxcontrol/pentagi/tree/main/docs", "", ("repo", "https://github.com/vxcontrol/pentagi", "pentagi")),
        ("https://github.com/topics/llm", "", None),
        ("https://huggingface.co/meta-models/Muse-Spark-Open-816B", "", ("model", "https://huggingface.co/meta-models/Muse-Spark-Open-816B", "Muse-Spark-Open-816B")),
        ("https://huggingface.co/spaces/org/demo-app", "", ("demo", "https://huggingface.co/spaces/org/demo-app", "demo-app")),
        ("https://huggingface.co/blog/some-post", "", None),
        ("https://arxiv.org/pdf/2510.01234.pdf", "", ("paper", "https://arxiv.org/abs/2510.01234", "arXiv")),
        ("https://store.steampowered.com/app/2097570/StarVaders/", "", ("store", "https://store.steampowered.com/app/2097570", "Steam")),
        ("https://mirelo.ai/", "официальном сайте", ("official", "https://mirelo.ai/", "mirelo.ai")),
        ("https://t.me/some_channel", "официальный канал", None),
        ("https://mirelo.ai/", "читать", None),
    ],
)
def test_classify_primary_links(url, anchor, expected):
    link = primary_links.classify(url, anchor)
    assert (link and (link["kind"], link["url"], link["label"])) == expected

def test_extract_from_html_ignores_site_chrome_and_own_pages():
    page = """<html><body><header><a href="https://github.com/habr/site">GitHub Хабра</a></header>
    <article><p>Проект <a href="/ru/companies/x/">компании</a> на <a href="https://github.com/debpalash/VoiceStudio">GitHub</a>,
    подробнее на <a href="https://habr.com/ru/about/">официальном сайте</a> и <a href="https://voice.studio/">официальном сайте проекта</a>.</p></article>
    <footer><a href="https://github.com/habr/footer">GitHub</a></footer></body></html>"""
    links = primary_links.extract_from_html(page, "https://habr.com/ru/news/1/")
    assert [link["url"] for link in links] == ["https://github.com/debpalash/VoiceStudio", "https://voice.studio/"]

def test_pick_prefers_code_over_site_and_renders_line():
    links = [primary_links.classify("https://voice.studio/", "официальный сайт"), primary_links.classify("https://github.com/a/VoiceStudio")]
    best = primary_links.pick(links)
    assert best["kind"] == "repo"
    assert primary_links.render_line(best, "ru") == "Репозиторий 👉 [VoiceStudio](https://github.com/a/VoiceStudio)"
    assert primary_links.render_line(best, "en").startswith("Repository 👉 ")
    assert primary_links.pick([]) is None

def test_feed_and_telegram_items_keep_primary_links():
    feed = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title><item><title>Repo</title>
    <link>https://example.com/news/1</link><description><![CDATA[<p>Code: <a href="https://github.com/a/b">here</a></p>]]></description>
    </item></channel></rss>"""
    assert parse_feed(feed)[0]["meta"]["links"][0]["url"] == "https://github.com/a/b"

    page = """<div class="tgme_widget_message" data-post="chan/5"><div class="tgme_widget_message_text">Новый агент.
    Репозиторий 👉 <a href="https://github.com/vxcontrol/pentagi">PentAGI</a></div><time datetime="2026-10-20T10:00:00+00:00"></time></div>"""
    [item] = telegram.parse_channel_page(page, "chan")
    assert item["meta"]["links"][0]["url"] == "https://github.com/vxcontrol/pentagi"

def test_telegram_footer_follows_show_sources_and_signature():
    body = "Модель на **816 млрд** параметров [1].\n\nВеса 👉 [Muse](https://huggingface.co/m/muse)"
    html = render_telegram_html("Заголовок", body, CITES)
    assert "Источники:" in html and "<b>816 млрд</b>" in html and "[1]" not in html
    html = render_telegram_html("Заголовок", body, CITES, show_sources=False, signature="[ИИ Пульс](https://t.me/pulse_itnews)")
    assert "Источники:" not in html
    assert html.endswith('\n\n<a href="https://t.me/pulse_itnews">ИИ Пульс</a>')
    caption = render_telegram_caption("Заголовок", body, CITES, show_sources=False, signature="@pulse_itnews")
    assert caption.endswith("\n\n@pulse_itnews") and "Источники:" not in caption

    vk = render_vk_text("Заголовок", body, CITES, show_sources=False, signature="**@pulse_itnews**")
    assert "Источники" not in vk and "**" not in vk
    assert vk.endswith("Веса 👉 Muse (https://huggingface.co/m/muse)\n\n@pulse_itnews")
    assert _post_options({"config": {"signature": "@x"}}, {"show_sources": False}) == {"show_sources": False, "signature": "@x"}

def test_project_criteria_replace_default_interest_criteria():
    assert interest.criteria_for({"interest_criteria": ""}, False) == interest.CRITERIA
    own = interest.criteria_for({"interest_criteria": "Высоко: инструменты, которые можно попробовать."}, True)
    assert "можно попробовать" in own and interest.CURATED_CRITERIA not in own

db_test = pytest.mark.usefixtures("clean")

@db_test
async def test_single_source_story_gets_more_context():
    pid = await make_project()
    text = " ".join(f"Факт номер {n} о новой базе данных ПолярДБ с подробностями." for n in range(1, 31))
    await import_manual(pid, [{"title": "ПолярДБ 5.0 вышла", "text": text, "source": "Одиночка"}])
    await process_project(pid)
    cluster = await db.fetchone("SELECT id FROM clusters WHERE project_id = %s", (pid,))
    [block] = await build_blocks(await get_project(pid), cluster["id"], 1, StubProvider())
    assert len(block.sentences) == SINGLE_CONTEXT_SENTENCES  # было context_sentences = 3

@db_test
async def test_post_ends_with_primary_link_line():
    pid = await make_project(min_items=1)
    await import_manual(pid, [{
        "title": "Вышел PentAGI — ИИ-агент для пентеста", "url": "https://github.com/vxcontrol/pentagi",
        "text": "PentAGI — репозиторий, где десять ИИ-агентов ищут уязвимости в закрытой системе. Проект собрал 30 тысяч звёзд на GitHub. " * 3,
        "source": "Reddit",
    }])
    await process_project(pid)
    cluster = await db.fetchone("SELECT id FROM clusters WHERE project_id = %s", (pid,))
    [did] = await run_generation({"project_id": pid, "kind": "post", "cluster_ids": [cluster["id"]]})
    draft = await db.fetchone("SELECT body, checks FROM drafts WHERE id = %s", (did,))
    assert draft["body"].endswith("\n\nРепозиторий 👉 [pentagi](https://github.com/vxcontrol/pentagi)")
    assert draft["checks"]["primary_link"]["kind"] == "repo"

@db_test
async def test_item_url_goes_first_among_primary_links():
    pid = await make_project()
    src = await db.fetchone("INSERT INTO sources(project_id, type, name, url) VALUES (%s, 'rss', 'Лента', '') RETURNING id", (pid,))

    await insert_items(pid, src["id"], [{"title": "x", "url": "https://github.com/a/b", "text": "t", "published_at": utcnow(),
                                         "meta": {"links": [primary_links.classify("https://arxiv.org/abs/1")]}}])
    meta = (await db.fetchone("SELECT meta FROM items WHERE project_id = %s", (pid,)))["meta"]
    assert [link["kind"] for link in meta["links"]] == ["repo", "paper"]


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("14 лучших скиллов для Claude Code в 2026 году", 14),
        ("14 Best Claude Code Skills for Developers in 2026", 14),
        ("Топ-10 сервисов для генерации видео", 10),
        ("7 инструментов для вайб-кодинга", 7),
        ("GPT-6 вышел: 5 главных изменений", 5),
        ("Вышли 3 игры Valve", None),
        ("Claude Opus 5.5 обошёл GPT-6 в 12 тестах", None),
        ("2 способа ускорить сборку", None),
    ],
)
def test_promised_count_recognises_listicle_titles(title, expected):
    assert promised_count(title) == expected


def test_title_mismatch_holds_post_even_in_full_auto():
    body = "Что умеет:\n• первое [1]\n• второе [1]\n• третье [1]"
    assert list_items(body) == 3
    checks = {"citation_coverage": 1.0, "length": len(body), "title_mismatch": {"promised": 14, "listed": 3}}
    review = automatic_review("14 лучших скиллов", body * 3, [{"n": 1}], checks, 900)
    assert "Заголовок обещает 14 пунктов, а в тексте их 3" in review["reasons"]
    assert gate_auto_publish(review, "full_auto") is False


async def test_fit_title_rewrites_listicle_title_once():
    class Chat:
        supports_chat = True
        calls = 0

        async def chat(self, system, user, **kw):
            Chat.calls += 1
            return "Лучшие скиллы для Claude Code: три главных"

    body = "Что умеет:\n• первое\n• второе\n• третье"
    assert await fit_title(Chat(), "", "14 лучших скиллов для Claude Code", body, None) == "Лучшие скиллы для Claude Code: три главных"
    assert await fit_title(Chat(), "", "Claude Code получил скиллы", body, None) == "Claude Code получил скиллы"
    assert Chat.calls == 1  # заголовок без обещания списка модель не переписывает


@db_test
async def test_listicle_item_gets_article_context():
    pid = await make_project()
    text = " ".join(f"Скилл номер {n} помогает с задачей {n} в Claude Code." for n in range(1, 41))
    await import_manual(pid, [{"title": "14 лучших скиллов для Claude Code", "text": text, "source": "Блог"}])
    await process_project(pid)
    cluster = await db.fetchone("SELECT id FROM clusters WHERE project_id = %s", (pid,))
    [block] = await build_blocks(await get_project(pid), cluster["id"], 1, StubProvider())
    assert len(block.sentences) == CURATED_CONTEXT_SENTENCES


def test_spread_sentences_keeps_short_article_and_samples_the_whole_long_one():
    short = [f"Предложение {n}." for n in range(10)]
    assert spread_sentences(short, cap=1000) == short
    long = [f"Предложение номер {n} о сравнении агентов и бенчмарках." for n in range(400)]
    picked = spread_sentences(long, cap=3000)
    assert sum(len(x) + 1 for x in picked) <= 3000
    assert picked[0] == long[0] and picked[1] == long[1]  # начало подряд
    assert any(x.startswith("… ") for x in picked)  # пропуски отмечены
    assert any("номер 39" in x for x in picked[-3:])  # выборка дошла до конца статьи


@db_test
async def test_longread_from_one_source_is_read_whole_and_written_as_article():
    """Аналитика начинается с новости: по 12 фразам первых 40 модель писала заметку про новость, а не про статью."""
    pid = await make_project()
    await db.execute("UPDATE projects SET article_max_length = 1200 WHERE id = %s", (pid,))
    intro = "Китай предложил пороги для экспорта AI-агентов: 20 подзадач, 60 агентов и 200 инструментов. " * 5
    middle = " ".join(f"Бенчмарк номер {n} измеряет агентов по-своему, и результаты нельзя сравнить напрямую." for n in range(80))
    text = f"{intro}{middle} Вывод автора: силу агента нельзя измерить одной цифрой."
    assert len(text) >= LONGREAD_CHARS
    await import_manual(pid, [{"title": "Почему AI-агентов всё ещё трудно сравнивать", "text": text, "source": "Хабр"}])
    await process_project(pid)
    cluster = await db.fetchone("SELECT id FROM clusters WHERE project_id = %s", (pid,))
    project = await get_project(pid)
    [block] = await build_blocks(project, cluster["id"], 1, StubProvider())
    assert block.longread and "Вывод автора" in block.text  # конец статьи модель видит

    class Chat(StubProvider):
        supports_chat = True
        chat_model = "test-model"
        seen: list = []

        async def chat(self, system, user, **kw):
            Chat.seen.append((system, user, kw))
            return "Почему агентов трудно сравнивать\n\nБенчмарки меряют разное, и одной цифры для силы агента нет [1]."

    res = await generate_for_clusters(project, [cluster["id"]], "post", Chat())
    system, user, kw = Chat.seen[0]
    assert "Напиши пост-разбор этой статьи" in user and "строго до 1200 символов" in user
    assert kw["max_tokens"] >= 900 and res["checks"]["format"] == "article" and res["max_length"] == 1200

