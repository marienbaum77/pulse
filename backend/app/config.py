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

    # stub — без внешних моделей (тесты / экстрактивный режим); openai — любой OpenAI-совместимый сервер (Ollama, vLLM, облако)
    llm_provider: str = "stub"
    llm_base_url: str = "http://localhost:11434/v1"
    llm_api_key: str = "ollama"
    llm_model: str = "qwen2.5:7b-instruct"
    embed_model: str = "bge-m3"
    # Эмбеддинги можно брать с другого сервера, чем текст (например, embeddings — локальный Ollama, chat — облачный API). Пусто — как у LLM_*.
    embed_base_url: str = ""
    embed_api_key: str = ""
    llm_timeout: float = 180.0

    allow_private_urls: bool = False
    telegram_bot_token: str = ""

    worker_concurrency: int = 2
    tick_seconds: int = 15
    retention_days: int = 30
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
