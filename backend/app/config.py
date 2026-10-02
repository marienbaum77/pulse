from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://pulse:pulse@localhost:5432/pulse"
    secret_key: str = "change-me-in-production"
    admin_email: str = "admin@example.com"
    admin_password: str = "admin12345"
    cookie_secure: bool = False
    session_hours: int = 12

    # openai — любой OpenAI-совместимый сервер (Groq, Ollama, vLLM, облако); stub — без текстовой модели.
    llm_provider: str = "openai"
    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_api_key: str = ""
    llm_model: str = "qwen/qwen3.8-27b"
    llm_chat_enabled: bool = True
    embed_model: str = "bge-m3"
    # Эмбеддинги можно брать с другого сервера, чем текст (например, локальный Ollama + облачный chat API).
    embed_base_url: str = "http://ollama:11434/v1"
    embed_api_key: str = ""
    llm_timeout: float = 180.0

    image_cache_dir: str = ""  # в docker-compose это общий том для api и worker
    image_cache_max_mb: int = 500
    image_cache_max_age_days: int = 30

    allow_private_urls: bool = False
    telegram_bot_token: str = ""

    worker_concurrency: int = 2
    tick_seconds: int = 15
    retention_days: int = 30
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
