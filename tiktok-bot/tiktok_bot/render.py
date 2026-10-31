"""Вертикальный ролик 1080×1920 из поста: слайд с заголовком и картинкой, видео или GIF поста, слайды с текстом,
финальный слайд с каналом. Кадры рисует Pillow (кириллица, переносы), склеивает встроенный ffmpeg (imageio-ffmpeg).
Текст держится в безопасной зоне: снизу и справа TikTok закрывает кадр подписью и кнопками."""
import asyncio
import io
import math
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from .text import Script

W, H = 1080, 1920
FPS = 30
SAFE_LEFT, SAFE_RIGHT, SAFE_TOP, SAFE_BOTTOM = 80, W - 150, 230, H - 480
BG_TOP, BG_BOTTOM = (15, 23, 42), (30, 41, 59)
ACCENT = (56, 189, 248)
FONT_DIRS = [os.environ.get("TIKTOK_BOT_FONT_DIR", ""), "/usr/share/fonts/truetype/dejavu", "C:/Windows/Fonts"]


@dataclass
class Media:
    images: list[bytes] = field(default_factory=list)
    video: bytes | None = None  # видео или GIF поста (Telegram отдаёт GIF как MP4)
    animation: bool = False  # GIF короткий — проигрывается трижды, обычное видео — один раз


def _font(bold: bool, size: int) -> ImageFont.FreeTypeFont:
    names = ["DejaVuSans-Bold.ttf", "arialbd.ttf"] if bold else ["DejaVuSans.ttf", "arial.ttf"]
    for d in filter(None, FONT_DIRS):
        for name in names:
            path = Path(d) / name
            if path.exists():
                return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> list[str]:
    lines: list[str] = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split():
            trial = f"{cur} {word}".strip()
            if draw.textlength(trial, font=font) <= width or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        lines.append(cur)
    return lines


def _fit_text(draw, text: str, bold: bool, size: int, width: int, max_height: int, min_size: int = 34) -> tuple:
    """Самый крупный кегль, при котором текст помещается в блок."""
    while True:
        font = _font(bold, size)
        lines = _wrap(draw, text, font, width)
        step = int(size * 1.32)
        if len(lines) * step <= max_height or size <= min_size:
            return font, lines, step
        size -= 4


def _background(image: bytes | None) -> Image.Image:
    if image:
        try:
            src = Image.open(io.BytesIO(image)).convert("RGB")
            bg = ImageOps.fit(src, (W, H), method=Image.LANCZOS).filter(ImageFilter.GaussianBlur(36))
            return Image.blend(bg, Image.new("RGB", (W, H), BG_TOP), 0.62)
        except Exception:
            pass
    bg = Image.new("RGB", (W, H), BG_TOP)
    draw = ImageDraw.Draw(bg)
    for y in range(H):  # вертикальный градиент
        t = y / H
        draw.line([(0, y), (W, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM)))
    return bg


def _picture(canvas: Image.Image, image: bytes | None, box: tuple[int, int, int, int]) -> int:
    """Картинка поста целиком (без обрезки) в рамке; возвращает нижнюю границу или верх рамки, если картинки нет."""
    if not image:
        return box[1]
    try:
        pic = Image.open(io.BytesIO(image)).convert("RGB")
    except Exception:
        return box[1]
    pic.thumbnail((box[2] - box[0], box[3] - box[1]), Image.LANCZOS)
    x = box[0] + (box[2] - box[0] - pic.width) // 2
    mask = Image.new("L", pic.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, pic.width, pic.height], radius=28, fill=255)
    canvas.paste(pic, (x, box[1]), mask)
    return box[1] + pic.height


def _footer(draw, handle: str) -> None:
    if handle:
        draw.text((SAFE_LEFT, SAFE_BOTTOM + 40), handle, font=_font(True, 40), fill=ACCENT)


def title_slide(script: Script, image: bytes | None, handle: str) -> Image.Image:
    canvas = _background(image)
    draw = ImageDraw.Draw(canvas)
    top = _picture(canvas, image, (SAFE_LEFT, SAFE_TOP, W - SAFE_LEFT, SAFE_TOP + 760))
    font, lines, step = _fit_text(draw, script.title, True, 78, SAFE_RIGHT - SAFE_LEFT, SAFE_BOTTOM - top - 60)
    y = top + 60
    for line in lines:
        draw.text((SAFE_LEFT, y), line, font=font, fill="white")
        y += step
    _footer(draw, handle)
    return canvas


def text_slide(text: str, image: bytes | None, handle: str, page: str, show_picture: bool = False) -> Image.Image:
    """Слайд с абзацем поста. show_picture — ещё одна иллюстрация поста над текстом, чтобы ролик не был сплошным текстом."""
    canvas = _background(image)
    draw = ImageDraw.Draw(canvas)
    top = _picture(canvas, image, (SAFE_LEFT, SAFE_TOP, W - SAFE_LEFT, SAFE_TOP + 560)) + 50 if show_picture else SAFE_TOP
    font, lines, step = _fit_text(draw, text, False, 58, SAFE_RIGHT - SAFE_LEFT, SAFE_BOTTOM - top - 80)
    y = top + (max(0, (SAFE_BOTTOM - top - len(lines) * step) // 3) if not show_picture else 0)
    for line in lines:
        draw.text((SAFE_LEFT, y), line, font=font, fill="white")
        y += step
    draw.text((SAFE_LEFT, SAFE_TOP - 90), page, font=_font(True, 38), fill=ACCENT)
    _footer(draw, handle)
    return canvas


def final_slide(script: Script, image: bytes | None, handle: str, channel_title: str) -> Image.Image:
    canvas = _background(image)
    draw = ImageDraw.Draw(canvas)
    lines = ["Ссылки и подробности —", "в Telegram"]
    if script.links:
        lines = [*script.links[:2], "", *lines]
    y = SAFE_TOP + 300
    for line in lines:
        font, wrapped, step = _fit_text(draw, line, False, 56, SAFE_RIGHT - SAFE_LEFT, 300)
        for part in wrapped:
            draw.text((SAFE_LEFT, y), part, font=font, fill="white")
            y += step
    y += 40
    for part in filter(None, [channel_title, handle]):
        font, wrapped, step = _fit_text(draw, part, True, 84, SAFE_RIGHT - SAFE_LEFT, 400)
        for line in wrapped:
            draw.text((SAFE_LEFT, y), line, font=font, fill=ACCENT)
            y += step
    return canvas


def video_backdrop(script: Script, image: bytes | None, handle: str) -> Image.Image:
    """Фон под видео поста: заголовок сверху, видео ставится ниже (VIDEO_BOX)."""
    canvas = _background(image)
    draw = ImageDraw.Draw(canvas)
    font, lines, step = _fit_text(draw, script.title, True, 56, SAFE_RIGHT - SAFE_LEFT, 300, min_size=36)
    y = SAFE_TOP - 80
    for line in lines:
        draw.text((SAFE_LEFT, y), line, font=font, fill="white")
        y += step
    _footer(draw, handle)
    return canvas


VIDEO_BOX = (W - 2 * 40, 1150, 560)  # ширина, высота, отступ сверху


def duration_for(text: str) -> float:
    """Сколько держать слайд: ~16 знаков в секунду плюс время заметить кадр."""
    return max(3.0, min(9.0, 1.5 + len(text) / 16))


def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        path = shutil.which("ffmpeg")
        if not path:
            raise RuntimeError("ffmpeg не найден: установите imageio-ffmpeg") from None
        return path


async def _run(*args: str, timeout: float = 300) -> None:
    proc = await asyncio.create_subprocess_exec(ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", *args,
                                                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("ffmpeg не уложился во время") from None
    if proc.returncode != 0:
        # первая строка с ошибкой бывает задолго до конца вывода
        raise RuntimeError(f"ffmpeg: {(err or b'').decode(errors='replace')[-1500:]}")


# Одинаковые параметры у всех кусков — тогда склейка обходится без перекодирования.
ENCODE = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(FPS), "-an"]


async def probe(path: Path) -> tuple[float | None, bool, float | None]:
    """Длительность в секундах, есть ли звуковая дорожка и частота кадров — из заголовка, который печатает ffmpeg -i."""
    proc = await asyncio.create_subprocess_exec(ffmpeg_exe(), "-hide_banner", "-i", str(path),
                                                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await asyncio.wait_for(proc.communicate(), timeout=60)
    info = (err or b"").decode(errors="replace")
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", info)
    seconds = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else None
    fps = re.search(r"Stream #\S+.*: Video:.*?([\d.]+) fps", info)
    return seconds, bool(re.search(r"Stream #\S+.*: Audio:", info)), float(fps[1]) if fps else None


# Карточка поста: слева отступ, справа место под кнопки TikTok; сверху вкладки, снизу подпись — туда текст не ставим.
CARD_X, CARD_W = 50, 900
CARD_TOP, CARD_BOTTOM = 170, H - 350
MIN_FIT_SCALE = 0.75  # пост, которому до безопасной зоны не хватает до четверти, уменьшаем и показываем целиком
HOLD_START, HOLD_END = 2.0, 2.5  # высокий пост: столько стоим наверху и внизу, между ними прокрутка
SCROLL_FPS = 60  # частота экрана телефона: при другой кадры показываются неравномерно
# Шаг прокрутки — целое число пикселей за кадр или за два: при дробной скорости (1,3 px за кадр) шаги чередуются
# 1–2 px и текст едет рывками. Так же, без разгона, ровно катятся титры в кино. Скорости: 30, 60, 120 и 180 px/с.
SCROLL_STEPS = ((1, 2), (1, 1), (2, 1), (3, 1))  # (пикселей, за столько кадров)
# Движущиеся слои накладываются с overlay=…:format=rgb: по умолчанию overlay работает в yuv420 и округляет позицию
# до чётного пикселя, и шаг в 1 px превращается в рывок на 2 px через кадр.


def shot_encode(fps: float) -> list[str]:
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-maxrate", "8M", "-bufsize", "16M", "-pix_fmt", "yuv420p",
            "-r", f"{fps:g}", "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-movflags", "+faststart"]


def clip_fps(fps: float | None) -> float:
    """Частота кадров ролика с видео — как у самого видео: перевод 24 кадров в 30 даёт рывки от повторённых кадров.
    TikTok принимает 23–60 кадров в секунду."""
    return round(fps, 3) if fps and 23 <= fps <= 60 else float(FPS)


def reading_seconds(text: str) -> float:
    """Сколько показывать пост без видео: беглое чтение ~18 знаков в секунду, но не меньше 8 и не больше 45 секунд."""
    return max(8.0, min(45.0, 3 + len(text) / 18))


def scroll_seconds(text: str, hidden: float) -> float:
    """Сколько прокручивать карточку после видео: время чтения той доли текста, что не поместилась (hidden — доля
    высоты карточки за кадром), но не меньше 4 и не больше 30 секунд."""
    return max(4.0, min(30.0, 2 + len(text) * hidden / 18))


def scroll_step(distance: int, want_seconds: float) -> tuple[int, int]:
    """Ровный шаг прокрутки, скорость которого ближе всего к нужной: distance пикселей за want_seconds."""
    want = distance / max(want_seconds, 0.1) / SCROLL_FPS  # пикселей за кадр
    return min(SCROLL_STEPS, key=lambda s: abs(math.log(s[0] / s[1] / want)))


def scroll_y(y0: int, y1: int, start: float, step: tuple[int, int], offset: int = 0) -> tuple[str, float]:
    """Выражение ffmpeg для верхнего края карточки по номеру кадра n: до start стоит в y0, потом едет вверх ровно на
    step[0] пикселей каждые step[1] кадров до y1 и стоит. offset — для того, что едет вместе с карточкой (видео в
    рамке плеера). Возвращает выражение и сколько секунд длится движение."""
    px, every = step
    n0, frames = round(start * SCROLL_FPS), math.ceil((y0 - y1) * every / px)
    return f"'{offset}+{y0}-min({y0 - y1},floor(clip(n-{n0},0,{frames})*{px}/{every}))'", frames / SCROLL_FPS


async def render_shot(shot, clip: bytes | None, text: str, max_video_seconds: int = 600) -> tuple[bytes, float, bool]:
    """Ролик из скриншота поста (screenshot.Shot) и, если есть, его видео в рамке плеера — как запись экрана Telegram.
    Видео идёт целиком (до max_video_seconds) со своим звуком, GIF крутится, пока читается пост. Пост без видео
    стоит на экране время чтения. Не влезающий по высоте пост прокручивается до конца текста — у поста с видео
    после того, как видео доиграет. Возвращает MP4, его длительность и есть ли в нём звук."""
    card = Image.open(io.BytesIO(shot.png)).convert("RGBA")
    box = shot.video_box if clip else None
    area = CARD_BOTTOM - CARD_TOP
    k = CARD_W / card.width
    if box and (box[1] + box[3]) * k > area:
        k = area / (box[1] + box[3])  # плеер целиком в безопасной зоне, даже если ради этого карточка станет уже
    if area < card.height * k <= area / MIN_FIT_SCALE:
        k = area / card.height  # почти влезает — чуть уменьшаем вместо прокрутки
    card = card.resize((round(card.width * k), round(card.height * k)), Image.LANCZOS)
    x = CARD_X + (CARD_W - card.width) // 2
    canvas = _background(shot.png)
    tall = card.height > area
    y0, y1 = (CARD_TOP, CARD_BOTTOM - card.height) if tall else (CARD_TOP + (area - card.height) // 2,) * 2

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        out = tmpdir / "tiktok.mp4"
        silence = ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
        back, front = tmpdir / "back.png", tmpdir / "card.png"
        canvas.save(back)
        card.save(front)
        if box:
            src = tmpdir / "clip"
            src.write_bytes(clip)
            clip_seconds, has_audio, src_fps = await probe(src)
            fps = clip_fps(src_fps)
            clip_seconds = clip_seconds or 10.0
            if shot.gif:  # GIF без звука и короткий: крутим, пока читается пост, но не меньше трёх повторов
                seconds, has_audio = min(max(reading_seconds(text), clip_seconds * 3), float(max_video_seconds)), False
            else:
                seconds = min(clip_seconds, float(max_video_seconds))
            bx, by, bw, bh = (round(v * k) for v in box)
            bw, bh = bw - bw % 2, bh - bh % 2  # фильтры ffmpeg в yuv требуют чётных размеров, иначе маска разойдётся с кадром
            if tall:  # видео играет наверху, потом карточка едет к концу текста; видео замирает на последнем кадре
                start = max(seconds, HOLD_START)
                step = scroll_step(y0 - y1, scroll_seconds(text, (card.height - area) / card.height))
                y_card, move = scroll_y(y0, y1, start, step)
                y_video, _ = scroll_y(y0, y1, start, step, offset=by)
                seconds, fps = start + move + HOLD_END, SCROLL_FPS
            else:
                y_card, y_video = str(y0), str(y0 + by)
            mask = tmpdir / "mask.png"
            # Скругления плеера повторяем по прозрачности самого скриншота, чтобы видео не вылезало за углы карточки.
            card.getchannel("A").crop((bx, by, bx + bw, by + bh)).save(mask)
            # Видео целиком в рамке плеера; поля, если пропорции другие, — размытый кадр того же видео, как в Telegram.
            fit = (f"[1:v]fps={fps:g},setsar=1,split[a][b];"
                   f"[a]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},gblur=sigma=24,eq=brightness=-0.1[blur];"
                   f"[b]scale={bw}:{bh}:force_original_aspect_ratio=decrease[sharp];"
                   f"[blur][sharp]overlay=(W-w)/2:(H-h)/2,format=rgba[v]")
            # звук видео кончается раньше прокрутки — дальше тишина
            audio_filter, audio = (";[1:a:0]apad[a]", ["-map", "[a]"]) if has_audio else ("", ["-map", "3:a"])
            await _run(
                "-loop", "1", "-framerate", f"{fps:g}", "-i", str(back),
                "-stream_loop", "-1" if shot.gif else "0", "-i", str(src),
                "-loop", "1", "-framerate", f"{fps:g}", "-i", str(mask), *silence,
                "-loop", "1", "-framerate", f"{fps:g}", "-i", str(front),
                "-filter_complex",
                f"{fit};[2:v]format=gray[m];[v][m]alphamerge[vm];[0:v][4:v]overlay={x}:{y_card}:eval=frame:format=rgb[bc];"
                f"[bc][vm]overlay={x + bx}:{y_video}:eval=frame:format=rgb,format=yuv420p[out]{audio_filter}",
                "-map", "[out]", *audio, "-t", f"{seconds:.3f}", *shot_encode(fps), str(out),
                timeout=120 + seconds * 6,
            )
            return out.read_bytes(), seconds, has_audio

        seconds = reading_seconds(text)
        if not tall:
            await _run("-loop", "1", "-framerate", str(FPS), "-i", str(back), "-loop", "1", "-framerate", str(FPS), "-i", str(front),
                       *silence, "-filter_complex", f"[0:v][1:v]overlay={x}:{y0},format=yuv420p[out]", "-map", "[out]", "-map", "2:a",
                       "-t", f"{seconds:.2f}", *shot_encode(FPS), str(out), timeout=120 + seconds * 4)
            return out.read_bytes(), seconds, False
        # Высокий пост без видео: стоит наверху, ровно едет вверх до конца текста и стоит внизу.
        y_card, move = scroll_y(y0, y1, HOLD_START, scroll_step(y0 - y1, max(seconds - HOLD_START - HOLD_END, 1.0)))
        seconds = HOLD_START + move + HOLD_END
        await _run("-loop", "1", "-framerate", str(SCROLL_FPS), "-i", str(back), "-loop", "1", "-framerate", str(SCROLL_FPS), "-i", str(front),
                   *silence, "-filter_complex", f"[0:v][1:v]overlay={x}:{y_card}:eval=frame:format=rgb,format=yuv420p[out]",
                   "-map", "[out]", "-map", "2:a", "-t", f"{seconds:.3f}", *shot_encode(SCROLL_FPS), str(out), timeout=120 + seconds * 6)
        return out.read_bytes(), seconds, False


async def render(script: Script, media: Media, handle: str, channel_title: str = "", max_video_seconds: int = 60) -> bytes:
    """MP4 ролика. Порядок: заголовок → видео поста (если есть) → текст → канал."""
    first = media.images[0] if media.images else None
    frames: list[tuple[Image.Image, float]] = [(title_slide(script, first, handle), 3.5)]
    for i, text in enumerate(script.slides):
        # фон слайдов по очереди берётся из картинок поста, чтобы ролик не был одним кадром
        index = (i + 1) % len(media.images) if media.images else 0
        bg = media.images[index] if media.images else None
        # картинку над текстом показываем, только если она не та же, что на заголовке
        frames.append((text_slide(text, bg, handle, f"{i + 1}/{len(script.slides)}", show_picture=index > 0), duration_for(text)))
    frames.append((final_slide(script, first, handle, channel_title), 3.0))

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        parts: list[Path] = []
        for n, (frame, seconds) in enumerate(frames):
            png, seg = tmpdir / f"f{n}.png", tmpdir / f"s{n:02d}.mp4"
            frame.save(png)
            await _run("-loop", "1", "-framerate", str(FPS), "-t", f"{seconds:.2f}", "-i", str(png), *ENCODE, str(seg))
            parts.append(seg)
        if media.video:
            src, back, seg = tmpdir / "post_video", tmpdir / "back.png", tmpdir / "s00v.mp4"
            src.write_bytes(media.video)
            video_backdrop(script, first, handle).save(back)
            vw, vh, vy = VIDEO_BOX
            try:
                await _run(
                    "-loop", "1", "-framerate", str(FPS), "-i", str(back), "-stream_loop", "2" if media.animation else "0", "-i", str(src),
                    "-filter_complex",
                    f"[1:v]scale={vw}:{vh}:force_original_aspect_ratio=decrease,setsar=1,fps={FPS}[v];"
                    f"[0:v][v]overlay=(W-w)/2:{vy}:shortest=1,format=yuv420p",
                    "-t", str(max_video_seconds), *ENCODE, str(seg),
                )
                parts.insert(1, seg)  # сразу после заголовка: видео — самое интересное в посте
            except RuntimeError:
                pass  # битое видео не мешает собрать ролик из слайдов
        listing = tmpdir / "list.txt"
        listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
        out = tmpdir / "tiktok.mp4"
        # Тихая дорожка: часть клиентов и сам TikTok хуже обрабатывают ролики совсем без звука.
        await _run("-f", "concat", "-safe", "0", "-i", str(listing), "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                   "-c:v", "copy", "-c:a", "aac", "-shortest", "-movflags", "+faststart", str(out))
        return out.read_bytes()
