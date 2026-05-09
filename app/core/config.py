from functools import lru_cache
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
    )

    PROJECT_NAME: str = "Smart Plan API"
    API_V1_PREFIX: str = "/api/v1"

    # Database
    DATABASE_URL: str = "sqlite+aiosqlite:///./smartplan.db"

    # Auth
    SECRET_KEY: str
    API_ACCESS_KEY: str
    API_ACCESS_KEY_HEADER: str = "X-API-Key"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # CORS
    BACKEND_CORS_ORIGINS: List[str] = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]
    BACKEND_CORS_ORIGIN_REGEX: str = (
        r"^https?://(localhost|127\.0\.0\.1|0\.0\.0\.0)(:\d+)?$"
    )
    BACKEND_CORS_ALLOW_ALL: bool = False

    # OpenAI forecasting
    OPENAI_API_KEY: str = ""
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"
    OPENAI_FORECAST_MODEL: str = "gpt-4o-mini"
    OPENAI_FORECAST_MAX_CONCURRENCY: int = 8
    OPENAI_FORECAST_TIMEOUT_SECONDS: float = 15.0
    FORECAST_CACHE_TTL_HOURS: int = 168
    FORECAST_CACHE_SCHEMA_VERSION: str = "v3"
    FORECAST_GPT_REFINEMENT_ENABLED: bool = False
    PERSISTENT_FORECAST_CACHE_ENABLED: bool = True
    INCREMENTAL_REFRESH_ENABLED: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[arg-type]


settings = get_settings()

