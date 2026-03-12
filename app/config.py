from functools import lru_cache
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    host: str = "127.0.0.1"
    port: int = 8000
    db_path: str = "./data/software_factory.db"
    github_webhook_secret: str = ""
    github_webhook_debounce_seconds: int = 60
    max_autofix_per_pr: int = 3
    max_concurrent_runs: int = 3
    pr_lock_ttl_seconds: int = 900
    max_retry_attempts: int = 3
    retry_backoff_base_seconds: int = 30
    retry_backoff_max_seconds: int = 1800
    bot_logins: tuple[str, ...] = ()
    noise_comment_patterns: tuple[str, ...] = ()
    managed_repo_prefixes: tuple[str, ...] = ()
    autofix_comment_author: str = "software-factory[bot]"
    log_dir: str = "logs"
    log_archive_subdir: str = "archive"
    log_retention_days: int = 7
    worker_id: str = "worker-default"

    @field_validator(
        "bot_logins",
        "noise_comment_patterns",
        "managed_repo_prefixes",
        mode="before",
    )
    @classmethod
    def _parse_list_value(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            items = [item.strip() for item in value.split(",")]
            return tuple(item for item in items if item)
        if isinstance(value, (list, tuple, set)):
            parsed_items: list[str] = []
            for item in value:
                if item is None:
                    continue
                text = str(item).strip()
                if text:
                    parsed_items.append(text)
            return tuple(parsed_items)
        return (str(value).strip(),) if str(value).strip() else ()

    @field_validator("autofix_comment_author", mode="before")
    @classmethod
    def _normalize_author(cls, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        enable_decoding=False,
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
