"""Первичная ссылка материала: репозиторий, модель, официальный сайт, страница в магазине, статья с arXiv.
Новость о проекте ведёт на пересказ, а читателю нужен сам проект — ссылку ищем в тексте статьи
и ставим в конец поста строкой «Репозиторий 👉 Название»."""
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

MAX_LINKS = 5
# Чем раньше вид в списке, тем он полезнее читателю: код и веса важнее сайта, сайт — статьи.
KIND_ORDER = ("repo", "model", "dataset", "demo", "official", "store", "paper")
PREFIXES = {
    "ru": {"repo": "Репозиторий", "model": "Модель", "dataset": "Датасет", "demo": "Демо", "official": "Сайт",
           "store": "В магазине", "paper": "Статья"},
    "en": {"repo": "Repository", "model": "Model", "dataset": "Dataset", "demo": "Demo", "official": "Website",
           "store": "Store", "paper": "Paper"},
}
# Служебные разделы GitHub и Hugging Face: первый сегмент пути — не владелец репозитория.
_GITHUB_SKIP = {
    "about", "apps", "collections", "contact", "customer-stories", "enterprise", "events", "explore", "features", "login",
    "marketplace", "orgs", "pricing", "readme", "resources", "security", "settings", "signup", "site", "solutions",
    "sponsors", "team", "topics", "trending",
}
_HF_SKIP = {"blog", "docs", "join", "learn", "login", "models", "organizations", "papers", "posts", "pricing", "settings", "tasks"}
# Ссылка «официальный сайт» узнаётся по тексту; соцсети, агрегаторы и видеохостинги официальным сайтом не считаем.
_OFFICIAL_TEXT = re.compile(
    r"официальн\w*\s+(?:сайт|страниц|блог|анонс)|сайт\w*\s+(?:проекта|компании|разработчик)|"
    r"official\s+(?:site|website|page|blog|announcement)|project\s+page|homepage",
    re.I,
)
_NOT_OFFICIAL = {
    "t.me", "telegram.me", "twitter.com", "x.com", "facebook.com", "instagram.com", "vk.com", "youtube.com", "youtu.be",
    "reddit.com", "news.google.com", "wikipedia.org", "linkedin.com", "tiktok.com", "discord.gg", "discord.com",
}


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def _on(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _label(text: str, fallback: str) -> str:
    text = " ".join(re.sub(r"[\[\]]", "", text or "").split()).strip(" ()«»\"'")[:60]
    return text if len(text) >= 2 else fallback


def classify(url: str, anchor: str = "", own_host: str = "") -> dict | None:
    """{url, kind, label} для полезной читателю ссылки или None."""
    host = _host(url)
    if not host or not url.startswith(("http://", "https://")):
        return None
    parts = [p for p in urlsplit(url).path.split("/") if p]
    if host in ("github.com", "gitlab.com") and len(parts) >= 2 and parts[0].lower() not in _GITHUB_SKIP:
        repo = parts[1].removesuffix(".git")
        return {"url": f"https://{host}/{parts[0]}/{repo}", "kind": "repo", "label": repo}
    if host == "huggingface.co" and parts:
        if parts[0] in ("datasets", "spaces") and len(parts) >= 3:
            kind = "dataset" if parts[0] == "datasets" else "demo"
            return {"url": f"https://huggingface.co/{'/'.join(parts[:3])}", "kind": kind, "label": parts[2]}
        if len(parts) >= 2 and parts[0] not in _HF_SKIP and parts[0] not in ("datasets", "spaces"):
            return {"url": f"https://huggingface.co/{parts[0]}/{parts[1]}", "kind": "model", "label": parts[1]}
        return None
    if host == "arxiv.org" and len(parts) >= 2 and parts[0] in ("abs", "pdf"):
        paper = parts[1].removesuffix(".pdf")
        return {"url": f"https://arxiv.org/abs/{paper}", "kind": "paper", "label": "arXiv"}
    if host == "store.steampowered.com" and len(parts) >= 2 and parts[0] == "app":
        return {"url": f"https://store.steampowered.com/app/{parts[1]}", "kind": "store", "label": "Steam"}
    if host == "apps.apple.com" or (host == "play.google.com" and parts[:2] == ["store", "apps"]):
        return {"url": url, "kind": "store", "label": "App Store" if host == "apps.apple.com" else "Google Play"}
    if anchor and _OFFICIAL_TEXT.search(anchor) and host != own_host and not any(_on(host, d) for d in _NOT_OFFICIAL):
        return {"url": url, "kind": "official", "label": host}
    return None


def content_root(soup: BeautifulSoup):
    """Основной текст страницы: <article>, <main> или <body> без шапки, меню, подвала и боковых колонок —
    там ссылки и картинки самого сайта, а не того, о чём статья. Дерево меняется на месте."""
    root = soup.find("article") or soup.find("main") or soup.body or soup
    for tag in root.find_all(["nav", "header", "footer", "aside", "form"]):
        tag.decompose()
    return root


def extract_from_html(page: str, base_url: str) -> list[dict]:
    """Полезные ссылки из основного текста страницы."""
    if not page or "<a" not in page:
        return []
    root = content_root(BeautifulSoup(page, "html.parser"))
    own = _host(base_url)
    out: list[dict] = []
    seen: set[str] = set()
    for a in root.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "mailto:", "javascript:")):
            continue
        link = classify(urljoin(base_url, href), a.get_text(" ", strip=True), own)
        if link and link["url"] not in seen:
            seen.add(link["url"])
            out.append(link)
            if len(out) >= MAX_LINKS:
                break
    return out


def merge(item_url: str, links: list[dict]) -> list[dict]:
    """Ссылки материала: сам адрес материала, если он первичный (пост Reddit на репозиторий), затем найденные в тексте."""
    own = classify(item_url)
    out = [own] if own else []
    for link in links:
        if link["url"] not in {x["url"] for x in out}:
            out.append(link)
    return out[:MAX_LINKS]


def pick(links: list[dict]) -> dict | None:
    """Самая полезная ссылка; при равном виде — та, что встретилась раньше (у главного материала)."""
    ranked = [(KIND_ORDER.index(link["kind"]), i, link) for i, link in enumerate(links) if link.get("kind") in KIND_ORDER]
    return min(ranked, key=lambda r: r[:2])[2] if ranked else None


def render_line(link: dict, language: str = "ru") -> str:
    code = (language or "ru").casefold().split("-", 1)[0]
    prefix = PREFIXES.get(code, PREFIXES["en"])[link["kind"]]
    return f"{prefix} 👉 [{_label(link.get('label', ''), _host(link['url']))}]({link['url']})"
