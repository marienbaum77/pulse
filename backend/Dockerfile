FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install -r requirements.txt
COPY backend/app ./app
COPY backend/scripts ./scripts
COPY sample_data ./sample_data
RUN useradd --create-home --uid 10001 pulse \
    && chown -R pulse /app \
    && mkdir -p /var/cache/pulse/images \
    && chown -R pulse /var/cache/pulse
USER pulse
EXPOSE 8000
# API и воркер — один образ, разные команды (см. docker-compose.yml)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
