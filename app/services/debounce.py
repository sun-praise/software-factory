from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import Protocol


@dataclass(frozen=True, slots=True)
class DebounceKey:
    repo: str
    pr_number: int


class DebounceBackend(Protocol):
    """Storage contract for debounce state."""

    window_seconds: float

    def record_event(
        self,
        repo: str,
        pr_number: int,
        arrived_at: float | None = None,
    ) -> DebounceKey: ...

    def is_ready(
        self,
        repo: str,
        pr_number: int,
        now: float | None = None,
    ) -> bool: ...

    def pull_ready(self, now: float | None = None) -> set[DebounceKey]: ...


class InMemoryDebounceBackend:
    """Single-process debounce backend keyed by (repo, pr_number)."""

    def __init__(self, window_seconds: float = 60) -> None:
        if window_seconds <= 0:
            raise ValueError("window_seconds must be greater than 0")

        self.window_seconds = float(window_seconds)
        self._latest_event_at: dict[DebounceKey, float] = {}

    def record_event(
        self,
        repo: str,
        pr_number: int,
        arrived_at: float | None = None,
    ) -> DebounceKey:
        key = DebounceKey(repo=repo, pr_number=pr_number)
        self._latest_event_at[key] = monotonic() if arrived_at is None else arrived_at
        return key

    def is_ready(
        self,
        repo: str,
        pr_number: int,
        now: float | None = None,
    ) -> bool:
        key = DebounceKey(repo=repo, pr_number=pr_number)
        last_event_at = self._latest_event_at.get(key)
        if last_event_at is None:
            return False

        current = monotonic() if now is None else now
        return current - last_event_at >= self.window_seconds

    def pull_ready(self, now: float | None = None) -> set[DebounceKey]:
        current = monotonic() if now is None else now
        ready: set[DebounceKey] = {
            key
            for key, last_event_at in self._latest_event_at.items()
            if current - last_event_at >= self.window_seconds
        }

        for key in ready:
            self._latest_event_at.pop(key, None)

        return ready
