FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /app
# DejaVu — шрифт с кириллицей для слайдов ролика
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
COPY tiktok-bot/requirements.txt .
# Безголовый Chromium (только headless shell) для скриншотов поста — с системными библиотеками, которые ему нужны
RUN pip install -r requirements.txt && python -m playwright install --with-deps --only-shell chromium && rm -rf /var/lib/apt/lists/*
COPY tiktok-bot/tiktok_bot ./tiktok_bot
RUN useradd --create-home --uid 10002 bot && mkdir -p /data && chown bot /data
USER bot
VOLUME /data
CMD ["python", "-m", "tiktok_bot.main"]
