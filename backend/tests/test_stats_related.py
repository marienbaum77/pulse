"""Статистика своих постов (просмотры из t.me/s, вес источников, подсказка для оценки) и ответы на связанные посты."""
import json
from types import SimpleNamespace

import pytest
from psycopg.types.json import Jsonb

from app import db, publisher
from app.pipeline import related, stats
from app.pipeline.connectors import telegram
from app.pipeline.ingest import import_manual

from .helpers import MiniServer, make_project

PAGE = """<div class="tgme_widget_message" data-post="chan/41"><span class="tgme_widget_message_views">1.2K</span>
<div class="tgme_widget_message_reactions"><span class="tgme_reaction"><i class="emoji"><b>🔥</b></i>7</span>
<span class="tgme_reaction"><i class="emoji"><b>👍</b></i>3</span></div></div>
<div class="tgme_widget_message" data-post="chan/42"><span class="tgme_widget_message_views">15</span></div>"""


def test_parse_stats_reads_views_and_reactions():
    assert telegram.parse_stats(PAGE) == {41: {"views": 1200, "reactions": 10}, 42: {"views": 15, "reactions": 0}}


def test_public_name_skips_private_channels():
    assert stats.public_name("@pulse_itnews") == "pulse_itnews"
    assert stats.public_name("https://t.me/pulse_itnews") == "pulse_itnews"
    assert stats.public_name("-1001234567890") is None


def test_entities_are_names_not_sentence_starts():
    assert related.entities("Уязвимость в PHP позволяет взломать сайт") == {"php"}
    assert related.entities("Армия России ударила по Киеву") == {"росс", "киев"}
    assert related.entities("Gears of War: E-Day обновила рекорд") == {"gears", "war", "e-day"}
    assert related.entities("Critical Vulnerabilities in PHP") == {"php"}


def test_reply_parameters_for_json_and_multipart():
    assert publisher._reply_parameters({"reply_to_message_id": 42}) == {"message_id": 42, "allow_sending_without_reply": True}
    assert publisher._reply_parameters({}) is None
    assert json.loads(publisher._reply_form({"reply_to_message_id": 42})["reply_parameters"])["message_id"] == 42
    assert publisher._reply_form({}) == {}


async def _channel(pid: int, chat_id: str = "@chan") -> int:
    row = await db.fetchone(
        "INSERT INTO channels(project_id, type, name, config) VALUES (%s, 'telegram', 'TG', %s) RETURNING id", (pid, Jsonb({"chat_id": chat_id}))
    )
    return row["id"]


async def _published(pid: int, channel_id: int, cluster_ids: list[int], title: str, message_id: int, *, views=None, hours_ago=72,
                     checks=None, citations=None) -> int:
    d = await db.fetchone(
        "INSERT INTO drafts(project_id, kind, cluster_ids, title, body, status, checks, citations) VALUES (%s,'post',%s,%s,'t','approved',%s,%s) RETURNING id",
        (pid, cluster_ids, title, Jsonb(checks or {}), Jsonb(citations or [])),
    )
    await db.execute(
        "INSERT INTO publications(draft_id, channel_id, status, external_id, sent_at, views) VALUES (%s,%s,'sent',%s, now() - make_interval(hours => %s), %s)",
        (d["id"], channel_id, str(message_id), hours_ago, views),
    )
    return d["id"]


@pytest.mark.usefixtures("clean")
async def test_collect_updates_views_and_weights_sources(monkeypatch):
    pid = await make_project(channel=False)
    ch = await _channel(pid)
    await import_manual(pid, [
        {"title": f"Новость {n} про {topic}", "text": f"Подробный текст номер {n} о {topic} и последствиях для отрасли.", "source": src}
        for n, (topic, src) in enumerate([("базы данных", "Сильный"), ("облака", "Сильный"), ("кластеры", "Сильный"),
                                          ("ноутбуки", "Слабый"), ("смартфоны", "Слабый"), ("планшеты", "Слабый")])
    ])
    # Каждый материал — свой сюжет: кластеризация здесь не проверяется.
    clusters = []
    for item in await db.fetchall("SELECT i.id, i.title, s.name FROM items i JOIN sources s ON s.id = i.source_id WHERE i.project_id = %s ORDER BY i.id", (pid,)):
        cid = await _cluster(pid, item["title"], [1.0, 0.0, 0.0])
        await db.execute("UPDATE items SET cluster_id = %s, status = 'clustered' WHERE id = %s", (cid, item["id"]))
        clusters.append({"id": cid, "name": item["name"]})
    assert len(clusters) == 6
    for n, c in enumerate(clusters, start=1):
        await _published(pid, ch, [c["id"]], f"Пост {n} ({c['name']})", 40 + n, views=300 if c["name"] == "Сильный" else 100)
    young = await _published(pid, ch, [clusters[0]["id"]], "Свежий пост", 60, hours_ago=2)

    page = '<div class="tgme_widget_message" data-post="chan/60"><span class="tgme_widget_message_views">77</span></div>'
    requested = []

    async def fake_get(url, headers):
        requested.append(url)
        return SimpleNamespace(text=page if "before" not in url else "", raise_for_status=lambda: None)

    monkeypatch.setattr(stats.ingest, "http_get", fake_get)
    assert await stats.collect_channel(ch) == 1
    assert requested[0] == "https://t.me/s/chan"
    assert (await db.fetchone("SELECT views FROM publications WHERE draft_id = %s", (young,)))["views"] == 77

    perf = {r["name"]: r for r in await db.fetchall("SELECT name, performance, performance_posts FROM sources WHERE project_id = %s", (pid,))}
    assert perf["Сильный"]["performance_posts"] == 3 and perf["Слабый"]["performance_posts"] == 3
    assert perf["Сильный"]["performance"] == 1.5  # 300 при медиане 200 — ×1.5, ровно верхняя граница
    assert perf["Слабый"]["performance"] == 0.5
    hint = await stats.audience_hint(pid)
    assert "(Сильный)" in hint.split("Меньше всего")[0] and "(Слабый)" in hint.split("Меньше всего")[1]


@pytest.mark.usefixtures("clean")
async def test_audience_hint_is_off_while_channel_has_almost_no_views():
    pid = await make_project(channel=False)
    ch = await _channel(pid)
    clusters = [await _cluster(pid, f"Сюжет {n}", [1.0, 0.0, 0.0]) for n in range(6)]
    for n, cid in enumerate(clusters):
        await _published(pid, ch, [cid], f"Пост {n}", 40 + n, views=2 + n % 3)  # 2–4 просмотра, как у нового канала
    await stats.recompute_project(pid)
    assert await stats.audience_hint(pid) == ""  # разница в 1–2 просмотра — шум, а не вкус аудитории

    await db.execute("UPDATE publications SET views = views * 100 WHERE channel_id = %s", (ch,))
    await stats.recompute_project(pid)
    assert "Пост" in await stats.audience_hint(pid)

    # Подсказка, сохранённая до порога по просмотрам, без median_views в промпт не попадает.
    await db.execute("UPDATE kv SET value = %s WHERE key = %s", (Jsonb({"top": ["Пост 1"], "bottom": ["Пост 2"]}), f"audience:{pid}"))
    assert await stats.audience_hint(pid) == ""


async def _cluster(pid: int, title: str, vec: list[float]) -> int:
    row = await db.fetchone(
        "INSERT INTO clusters(project_id, title, centroid, published_centroid, state, published_at, score, first_seen, last_seen) "
        "VALUES (%s, %s, %s, %s, 'published', now(), 0.5, now(), now()) RETURNING id",
        (pid, title, vec, vec),
    )
    return row["id"]


@pytest.mark.usefixtures("clean")
async def test_find_related_by_update_link_and_similar_names():
    pid = await make_project(channel=False)
    ch = await _channel(pid)
    project = await db.fetchone("SELECT * FROM projects WHERE id = %s", (pid,))
    php = await _cluster(pid, "Уязвимость в PHP позволяет выполнить код", [1.0, 0.0, 0.0])
    old_php = await _published(pid, ch, [php], "Уязвимость в PHP позволяет выполнить код", 10)
    repo_draft = await _published(pid, ch, [await _cluster(pid, "Вышел PentAGI", [0.0, 1.0, 0.0])], "Вышел PentAGI", 11,
                                  checks={"primary_link": {"url": "https://github.com/vxcontrol/pentagi"}})

    # тот же сюжет вырос — пост-обновление
    assert (await related.find_related(project, -1, [php], "PHP: вышли исправления", {}, []))["reason"] == "update"

    # тот же репозиторий
    other = await _cluster(pid, "PentAGI получил веб-интерфейс", [0.0, 0.0, 1.0])
    found = await related.find_related(project, -1, [other], "PentAGI получил веб-интерфейс", {"primary_link": {"url": "https://github.com/vxcontrol/pentagi"}}, [])
    assert found == {"draft_id": repo_draft, "title": "Вышел PentAGI", "reason": "link", "similarity": 1.0}

    # близкий вектор и общее имя — ответ; близкий вектор без общего имени — отдельный пост
    near = await _cluster(pid, "Critical Vulnerabilities in PHP", [0.8, 0.0, 0.6])  # косинус 0.8
    assert (await related.find_related(project, -1, [near], "Critical Vulnerabilities in PHP", {}, []))["draft_id"] == old_php
    stranger = await _cluster(pid, "Microsoft представила Copilot", [0.8, 0.0, 0.6])
    assert await related.find_related(project, -1, [stranger], "Microsoft представила Copilot", {}, []) is None

    await db.execute("UPDATE projects SET reply_related_days = 0 WHERE id = %s", (pid,))
    project = await db.fetchone("SELECT * FROM projects WHERE id = %s", (pid,))
    assert await related.find_related(project, -1, [php], "PHP", {}, []) is None


@pytest.mark.usefixtures("clean")
async def test_publisher_replies_with_message_id_of_the_same_channel():
    pid = await make_project(channel=False)
    ch = await _channel(pid)
    first = await _published(pid, ch, [], "Первый пост", 42)
    d = await db.fetchone(
        "INSERT INTO drafts(project_id, kind, title, body, status, reply_to_draft_id) VALUES (%s,'post','Второй','t','approved',%s) RETURNING id",
        (pid, first),
    )
    await db.execute("INSERT INTO publications(draft_id, channel_id) VALUES (%s, %s)", (d["id"], ch))
    seen = {}

    async def fake_send(channel, draft, pub):
        seen.update(draft)
        return publisher.SendResult("sent", external_id="43")

    assert await publisher.process_one({"telegram": fake_send})
    assert seen["reply_to_message_id"] == 42


async def test_telegram_request_carries_reply_parameters(monkeypatch):
    bodies = []

    def handler(method, path, headers, body):
        if path.startswith("/img/"):
            return 200, {"Content-Type": "image/png"}, b"\x89PNG-bytes"
        bodies.append((path.rsplit("/", 1)[-1], body))
        return 200, {"Content-Type": "application/json"}, '{"ok":true,"result":{"message_id":7}}'

    async with MiniServer(handler) as srv:
        monkeypatch.setenv("T_BOT", "OK")
        channel = {"config": {"chat_id": "@c", "token_env": "T_BOT", "api_base": srv.url}}
        draft = {"id": 1, "title": "З", "body": "Текст", "citations": [], "reply_to_message_id": 42}
        await publisher.send_telegram(channel, draft, {"id": 1})
        assert json.loads(bodies[-1][1])["reply_parameters"] == {"message_id": 42, "allow_sending_without_reply": True}
        await publisher.send_telegram(channel, {**draft, "image_url": f"{srv.url}/img/1.png"}, {"id": 2})
        assert bodies[-1][0] == "sendPhoto" and b'"message_id": 42' in bodies[-1][1]
