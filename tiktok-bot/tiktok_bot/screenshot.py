"""Скриншот поста таким, каким его видят в Telegram: страница встраивания t.me/<канал>/<номер>?embed=1 в безголовом
Chromium. Для поста с видео — ещё прямая ссылка на файл в CDN Telegram (без лимита Bot API в 20 МБ) и место плеера на
скриншоте: в эту рамку потом вставляется само видео, и ролик выглядит как запись экрана с постом."""
import asyncio
from dataclasses import dataclass

CSS_WIDTH = 420  # ширина страницы в CSS-пикселях: пост свёрстан, как в мобильном Telegram
SCALE = 2.2  # пикселей на CSS-пиксель: карточка выходит около 900 px — чётко на экране 1080
EMBED = "https://t.me/{username}/{message_id}?embed=1&mode=tme"
CARD = ".tgme_widget_message"
# Счётчик просмотров у молодого канала только отпугивает. Фон страницы прозрачный: карточка ляжет на свой фон.
STYLE = ".tgme_widget_message_views{visibility:hidden!important} html,body{background:transparent!important}"
FIND = """(sel) => {
  const card = document.querySelector(sel);
  if (!card || !card.querySelector('.tgme_widget_message_bubble')) return null;
  const c = card.getBoundingClientRect();
  // У большого видео страница даёт только превью с надписью «Media is too big» — без <video>, но рамка плеера есть.
  const player = card.querySelector('.tgme_widget_message_video_player');
  const v = player ? player.querySelector('video') : card.querySelector('video.tgme_widget_message_video');
  const p = (player || v) && (player || v).getBoundingClientRect();
  return {video_url: v ? (v.currentSrc || v.src || null) : null, gif: !!(v && v.loop),
          box: p ? [p.left - c.left, p.top - c.top, p.width, p.height] : null};
}"""
# Страница всегда пишет дату по-английски («Oct 25 at 08:01»): переписываем по-русски в часовом поясе браузера.
RUSSIAN = """() => {
  for (const t of document.querySelectorAll('time[datetime]')) {
    const d = new Date(t.getAttribute('datetime'));
    if (isNaN(d)) continue;
    const day = new Intl.DateTimeFormat('ru-RU', {day: 'numeric', month: 'long'}).format(d);
    const time = new Intl.DateTimeFormat('ru-RU', {hour: '2-digit', minute: '2-digit'}).format(d);
    t.textContent = day + ' в ' + time;
  }
  for (const m of document.querySelectorAll('.tgme_widget_message_meta'))
    for (const n of m.childNodes) if (n.nodeType === 3) n.textContent = n.textContent.replace(/\\bedited\\b/, 'изменено');
}"""


@dataclass
class Shot:
    png: bytes  # карточка поста; за скруглёнными краями — прозрачность
    video_box: tuple[int, int, int, int] | None = None  # x, y, ширина, высота плеера в пикселях png
    video_url: str | None = None  # нет — видео слишком большое для страницы, его берут через Bot API
    gif: bool = False  # GIF Telegram отдаёт как MP4 без звука, на странице он зациклен


def embed_url(username: str, message_id: int, dark: bool = True) -> str:
    return EMBED.format(username=username.removeprefix("@"), message_id=message_id) + ("&dark=1" if dark else "")


async def capture(url: str, timezone: str = "Europe/Moscow", timeout: float = 45, attempts: int = 3, retry_delay: float = 5) -> Shot | None:
    """None — поста на странице нет: канал приватный, пост удалён или ещё не появился в веб-версии.
    Дату поста страница пишет по языку и часовому поясу браузера."""
    from playwright.async_api import async_playwright  # Chromium нужен только здесь

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--disable-dev-shm-usage"])  # /dev/shm в контейнере всего 64 МБ
        try:
            page = await browser.new_page(viewport={"width": CSS_WIDTH, "height": 900}, device_scale_factor=SCALE,
                                          locale="ru-RU", timezone_id=timezone)
            for attempt in range(attempts):
                await page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
                info = await page.evaluate(FIND, CARD)
                if info:
                    break
                if attempt + 1 < attempts:
                    await asyncio.sleep(retry_delay)  # свежий пост появляется в вебе с задержкой
            else:
                return None
            await page.add_style_tag(content=STYLE)
            await page.evaluate(RUSSIAN)
            await page.evaluate("document.fonts.ready.then(() => true)")
            png = await page.locator(CARD).first.screenshot(omit_background=True, animations="disabled", timeout=timeout * 1000)
            box = tuple(round(x * SCALE) for x in info["box"]) if info["box"] else None
            return Shot(png, box, info["video_url"], info["gif"])
        finally:
            await browser.close()
