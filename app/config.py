import logging
import os
from functools import lru_cache
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

_AI_TOKEN_ENV_VARS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "OPENAI_API_KEY",
        "ZHIPU_API_KEY",
        "DEEPSEEK_API_KEY",
    }
)


class Settings(BaseSettings):
    app_env: str = "development"
    host: str = "127.0.0.1"
    port: int = 8000
    db_path: str = "./data/software_factory.db"
    github_webhook_secret: str = ""
    github_token: str = ""
    github_webhook_debounce_seconds: int = 60
    max_autofix_per_pr: int = 3
    max_concurrent_runs: int = 3
    stale_run_timeout_seconds: int = 900
    pr_lock_ttl_seconds: int = 900
    max_retry_attempts: int = 3
    retry_backoff_base_seconds: int = 30
    retry_backoff_max_seconds: int = 1800
    bot_logins: tuple[str, ...] = ()
    noise_comment_patterns: tuple[str, ...] = ()
    managed_repo_prefixes: tuple[str, ...] = ()
    non_retryable_error_codes: tuple[str, ...] = (
        "unsupported_project_type",
        "checks_failed",
        "head_sha_mismatch",
        "ai_not_configured",
        "ai_invalid_response",
        "ai_request_client_error",
        "agent_claude_stalled",
        "patch_apply_failed",
        "rebase_conflict_blocker",
        "rebase_blocker",
        "rebase_sha_read_failed",
        "rebase_fetch_failed",
    )
    autofix_comment_author: str = "software-factory[bot]"
    autofix_baseline_failures: bool = False
    max_baseline_fix_attempts: int = 1
    log_dir: str = "logs"
    log_archive_subdir: str = "archive"
    log_retention_days: int = 7
    worker_id: str = "worker-default"
    ai_provider: str = "anthropic"
    ai_timeout_seconds: int = 120
    ai_max_output_tokens: int = 6000
    ai_temperature: float = 0.0
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-3-7-sonnet-latest"
    anthropic_base_url: str = "https://api.anthropic.com"
    openai_api_key: str = ""
    openai_model: str = "gpt-4.1"
    openai_base_url: str = "https://api.openai.com/v1"
    agent_sdks: tuple[str, ...] = ("claude_agent_sdk", "openhands")
    openhands_command: str = "openhands"
    claude_agent_sdk_command: str = "claude"
    claude_agent_sdk_provider: str = "zhipu"
    claude_agent_sdk_base_url: str = "https://open.bigmodel.cn/api/anthropic"
    claude_agent_sdk_model: str = "glm-5"
    claude_agent_sdk_runtime: str = "host"
    claude_agent_sdk_container_image: str = "software-factory/claude-agent:latest"
    openhands_command_timeout_seconds: int = 600
    claude_agent_sdk_command_timeout_seconds: int = 1800
    openhands_worktree_base_dir: str = ".software-factory-worktrees"
    claude_agent_sdk_worktree_base_dir: str = ".software-factory-worktrees"
    repo_cache_base_dir: str = ".software-factory-repo-cache"
    run_workspace_base_dir: str = ".software-factory-run-workspaces"

    @field_validator(
        "bot_logins",
        "noise_comment_patterns",
        "managed_repo_prefixes",
        "non_retryable_error_codes",
        "agent_sdks",
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


def validate_web_env() -> list[str]:
    """Check that the web process does not have AI tokens in its environment.

    Returns a list of offending env var names found.  Call this at web startup
    and log / abort if the list is non-empty.
    """
    found = [v for v in _AI_TOKEN_ENV_VARS if os.environ.get(v)]
    if found:
        logger.warning(
            "web process has AI tokens in environment: %s — "
            "strip them before starting the web service to avoid token leaks",
            ", ".join(sorted(found)),
        )
    return found
