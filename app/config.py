from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    host: str = "127.0.0.1"
    port: int = 8000
    db_path: str = "./data/software_factory.db"
    github_webhook_secret: str = ""
    github_webhook_debounce_seconds: int = 60

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
