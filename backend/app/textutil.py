import hashlib
import html
import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

_TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid", "yclid", "ref"}
_SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+(?=[«\"(\[]?[А-ЯЁA-Z0-9])")
_NUM = re.compile(r"\d+(?:[.,]\d+)?")
_WORD = re.compile(r"\w+", re.UNICODE)
_SOURCE_FOOTER = re.compile(r"(?im)^[ \t]*(?:#{1,6}[ \t]*)?(?:источники(?: и материалы)?|список источников|sources|references)[ \t]*:?.*$")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def strip_html(raw: str) -> str:
    if not raw:
        return ""
    if "<" in raw and ">" in raw:
        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup.find_all(["script", "style"]):
            tag.decompose()
        for br in soup.find_all("br"):
            br.replace_with(" ")
        # Ячейки таблиц без разделителей склеиваются («1 - Python5 - C#») — ставим «; » между ними.
        for tag in soup.find_all(["td", "th"]):
            tag.append("; ")
        for tag in soup.find_all(["p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"]):
            tag.append(" ")  # пробел после блочных тегов, но не внутри строки у inline-тегов
        raw = soup.get_text("")
    raw = html.unescape(raw)
    raw = re.sub(r"\s*;\s*;+", ";", raw)
    return re.sub(r"\s+", " ", raw).strip().strip(";").strip()


def normalize_url(url: str) -> str:
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.lower() not in _TRACKING]
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/") or "/", urlencode(q), ""))


def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def item_hash(url: str, title: str, text: str) -> str:
    return sha1(normalize_url(url)) if url else sha1((title + "\n" + text[:2000]).lower())


def split_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return []
    return [s.strip() for s in _SENT_SPLIT.split(text) if len(s.strip()) > 1]


def numbers_in(text: str) -> set[str]:
    """Числа из текста в нормализованном виде (без пробелов-разделителей тысяч, запятая → точка)."""
    cleaned = re.sub(r"(?<=\d)[\s\u00a0](?=\d{3}\b)", "", text or "")
    return {n.replace(",", ".") for n in _NUM.findall(cleaned)}


def words(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text or "")]


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    m = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return (cut[: m + 1] if m > limit * 0.5 else cut.rstrip() + "…")


_MARK = re.compile(r"([ \t]*)\[(\d+)\]")


def strip_citations(text: str, valid: set[int] | None = None) -> str:
    """Убирает маркеры источников [1][2] из текста, который уходит подписчикам. В черновике они нужны редактору для сверки фактов,
    в опубликованном посте их быть не должно. Маркер, вплотную прижатый к слову (a[0] в технической заметке), удаляется только
    если его номер есть среди источников черновика (valid); всё остальное в тексте не меняется."""
    text = text or ""

    def repl(m: re.Match) -> str:
        before = text[m.start() - 1] if m.start() > 0 else " "
        attached = not m.group(1) and (before.isalnum() or before == "_")
        return m.group(0) if attached and not (valid and int(m.group(2)) in valid) else ""

    out = _MARK.sub(repl, text)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"[ \t]+([.,;:!?…])", r"\1", out)
    out = re.sub(r"(?m)^[ \t]+|[ \t]+$", "", out)
    return out.strip()


def strip_source_footer(text: str) -> str:
    """Удаляет добавленный в конце отдельный список источников, не трогая ссылки внутри текста."""
    matches = list(_SOURCE_FOOTER.finditer(text or ""))
    return text[:matches[-1].start()].strip() if matches else (text or "").strip()
