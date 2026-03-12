from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


# 终止状态集合：处于这些状态的 autofix run 不会进行重试
# - success: 已成功完成
# - cancelled: 已被用户或系统取消
TERMINAL_STATUSES = {"success", "cancelled"}


@dataclass(frozen=True)
class RetryPlan:
    run_id: int
    scheduled: bool
    retry_after: str | None
    delay_seconds: int | None
    next_attempt_count: int


@dataclass
class RetryConfig:
    """重试配置参数

    Attributes:
        base_delay_seconds: 基础延迟秒数，首次重试的等待时间
        max_delay_seconds: 最大延迟秒数，指数退避的上限
        non_retryable_error_codes: 不可重试的错误代码集合，遇到这些错误将终止重试
    """

    base_delay_seconds: int = 30
    max_delay_seconds: int = 1800
    non_retryable_error_codes: set[str] | None = None


def should_retry(
    *,
    status: str,
    attempt_count: int,
    max_attempts: int,
    retryable: bool = True,
    error_code: str | None = None,
    non_retryable_error_codes: set[str] | None = None,
) -> bool:
    if not retryable:
        return False
    if max_attempts <= 0:
        return False
    if status in TERMINAL_STATUSES:
        return False
    if attempt_count >= max_attempts:
        return False
    if (
        error_code
        and non_retryable_error_codes
        and error_code in non_retryable_error_codes
    ):
        return False
    return True


def compute_backoff_seconds(
    retry_number: int,
    base_seconds: int = 30,
    max_seconds: int = 1800,
) -> int:
    if retry_number <= 0:
        raise ValueError("retry_number must be positive")
    if base_seconds <= 0:
        raise ValueError("base_seconds must be positive")
    if max_seconds <= 0:
        raise ValueError("max_seconds must be positive")

    delay = base_seconds * (2 ** (retry_number - 1))
    return min(delay, max_seconds)


def schedule_retry(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    error_code: str | None = None,
    error_summary: str | None = None,
    now: datetime | None = None,
    config: RetryConfig | None = None,
    base_delay_seconds: int = 30,
    max_delay_seconds: int = 1800,
    non_retryable_error_codes: set[str] | None = None,
) -> RetryPlan:
    if config is None:
        config = RetryConfig(
            base_delay_seconds=base_delay_seconds,
            max_delay_seconds=max_delay_seconds,
            non_retryable_error_codes=non_retryable_error_codes,
        )

    row = conn.execute(
        """
        SELECT id, status, attempt_count, max_attempts, retryable
        FROM autofix_runs
        WHERE id = ?
        LIMIT 1
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Unknown run_id: {run_id}")

    status = str(row["status"])
    attempt_count = int(row["attempt_count"])
    max_attempts = int(row["max_attempts"])
    retryable = bool(int(row["retryable"]))
    current_time = _normalize_now(now)
    error_time = _to_timestamp(current_time)

    if not should_retry(
        status=status,
        attempt_count=attempt_count,
        max_attempts=max_attempts,
        retryable=retryable,
        error_code=error_code,
        non_retryable_error_codes=config.non_retryable_error_codes,
    ):
        conn.execute(
            """
            UPDATE autofix_runs
            SET status = 'failed',
                retryable = ?,
                last_error_code = ?,
                last_error_at = ?,
                error_summary = COALESCE(?, error_summary),
                updated_at = CURRENT_TIMESTAMP,
                finished_at = COALESCE(finished_at, CURRENT_TIMESTAMP)
            WHERE id = ?
            """,
            (
                0
                if error_code
                and config.non_retryable_error_codes
                and error_code in config.non_retryable_error_codes
                else int(retryable),
                error_code,
                error_time,
                error_summary,
                run_id,
            ),
        )
        conn.commit()
        return RetryPlan(
            run_id=run_id,
            scheduled=False,
            retry_after=None,
            delay_seconds=None,
            next_attempt_count=attempt_count,
        )

    retry_number = max(1, attempt_count)
    delay_seconds = compute_backoff_seconds(
        retry_number=retry_number,
        base_seconds=config.base_delay_seconds,
        max_seconds=config.max_delay_seconds,
    )
    retry_after = _to_timestamp(current_time + timedelta(seconds=delay_seconds))
    next_attempt_count = attempt_count + 1

    conn.execute(
        """
        UPDATE autofix_runs
        SET status = 'retry_scheduled',
            retry_after = ?,
            attempt_count = ?,
            last_error_code = ?,
            last_error_at = ?,
            error_summary = COALESCE(?, error_summary),
            updated_at = CURRENT_TIMESTAMP,
            finished_at = NULL
        WHERE id = ?
        """,
        (
            retry_after,
            next_attempt_count,
            error_code,
            error_time,
            error_summary,
            run_id,
        ),
    )
    conn.commit()
    return RetryPlan(
        run_id=run_id,
        scheduled=True,
        retry_after=retry_after,
        delay_seconds=delay_seconds,
        next_attempt_count=next_attempt_count,
    )


def _normalize_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _to_timestamp(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")
