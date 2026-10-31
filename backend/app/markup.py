"""Лёгкая разметка текста поста: **жирный** и [текст](https://ссылка). Черновик хранит её как есть,
а каждый канал публикации переводит в свой формат: Telegram — в HTML, Discord понимает её сам, ВКонтакте — простой текст."""
import html
import re

LINK = re.compile(r"\[([^\[\]\n]{1,200})\]\((https?://[^\s()<>]+)\)")
BOLD = re.compile(r"\*\*(?=\S)([^*\n]{1,300}?)(?<=\S)\*\*")


def to_telegram_html(text: str) -> str:
    """Экранирует текст и превращает разметку в теги Telegram. Ссылки ищутся после экранирования:
    в адресе остаётся &amp;, как и положено в атрибуте href."""
    out = html.escape(text or "")
    out = LINK.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', out)
    return BOLD.sub(r"<b>\1</b>", out)


def to_plain(text: str) -> str:
    """Для площадок без форматирования: жирный — обычный текст, ссылка — «текст (адрес)»."""
    out = LINK.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text or "")
    return BOLD.sub(r"\1", out)


def visible(text: str) -> str:
    """Текст, который увидит читатель: без звёздочек и адресов ссылок."""
    return BOLD.sub(r"\1", LINK.sub(r"\1", text or ""))


def keep_links(text: str, allowed: set[str]) -> str:
    """Ссылки, которых нет в источниках, модель могла выдумать: оставляем от них только текст."""
    return LINK.sub(lambda m: m.group(0) if m.group(2) in allowed else m.group(1), text or "")
