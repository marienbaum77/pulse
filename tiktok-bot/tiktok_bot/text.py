"""Текст поста для ролика: заголовок, абзацы для слайдов и подпись для TikTok. Языковая модель не нужна —
пост уже написан, его надо только разложить по кадрам."""
import re
from dataclasses import dataclass

SLIDE_CHARS = 230  # столько текста читается с экрана телефона за 5–8 секунд
MAX_BODY_SLIDES = 5
_MARK = re.compile(r"\s*\[\d+\]")
_SIGNATURE = re.compile(r"^\s*@\w{4,}\s*$")
_SOURCES = re.compile(r"^\s*(?:источники|sources)\s*:", re.I)
# Шрифт слайдов не рисует эмодзи (вместо них были бы квадраты); в подписи TikTok они остаются.
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍⃣\U000E0000-\U000E007F]")
_SENTENCE = re.compile(r"(?<=[.!?…])\s+")


@dataclass
class Script:
    title: str
    slides: list[str]
    links: list[str]  # строки «Репозиторий 👉 …»: в TikTok ссылки не кликаются, поэтому только упоминание
    caption: str


def plain(text: str) -> str:
    return re.sub(r"[ \t]{2,}", " ", _EMOJI.sub("", text)).strip()


def _chunks(paragraph: str, limit: int = SLIDE_CHARS) -> list[str]:
    if len(paragraph) <= limit:
        return [paragraph]
    out, cur = [], ""
    for sentence in _SENTENCE.split(paragraph):
        if cur and len(cur) + 1 + len(sentence) > limit:
            out.append(cur)
            cur = sentence
        else:
            cur = f"{cur} {sentence}".strip()
    if cur:
        out.append(cur)
    return out


def build_script(text: str, channel: str, hashtags: str = "") -> Script:
    """Первая строка — заголовок; списки «•» держатся вместе, если влезают в слайд."""
    lines = [_MARK.sub("", line).rstrip() for line in (text or "").splitlines()]
    lines = [line for line in lines if not _SIGNATURE.match(line) and not _SOURCES.match(line)]
    while lines and not lines[0].strip():
        lines.pop(0)
    title = plain(lines[0]) if lines else ""
    links = [plain(line) for line in lines[1:] if "👉" in line]
    body = "\n".join(line for line in lines[1:] if "👉" not in line)
    paragraphs = [plain(p) for p in re.split(r"\n\s*\n", body) if plain(p)]
    slides: list[str] = []
    for p in paragraphs:
        slides += _chunks(p) if "\n" not in p else ([p] if len(p) <= SLIDE_CHARS else _chunks(p.replace("\n", " ")))
    handle = f"@{channel.removeprefix('@')}" if channel else ""
    first = _SENTENCE.split(paragraphs[0])[0] if paragraphs else ""
    caption = "\n\n".join(x for x in (title, first, f"Подробности и ссылки — в Telegram {handle}".strip(), hashtags.strip()) if x)
    return Script(title=title, slides=slides[:MAX_BODY_SLIDES], links=links, caption=caption[:2200])
