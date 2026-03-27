from app.services.debounce import DebounceKey, InMemoryDebounceBackend


def test_not_ready_within_window() -> None:
    backend = InMemoryDebounceBackend(window_seconds=60)

    backend.record_event("acme/repo", 101, arrived_at=100.0)

    assert backend.is_ready("acme/repo", 101, now=159.9) is False
    assert backend.pull_ready(now=159.9) == set()


def test_ready_after_window_timeout() -> None:
    backend = InMemoryDebounceBackend(window_seconds=60)

    backend.record_event("acme/repo", 101, arrived_at=100.0)

    assert backend.is_ready("acme/repo", 101, now=160.0) is True
    assert backend.pull_ready(now=160.0) == {DebounceKey("acme/repo", 101)}


def test_isolated_between_multiple_pull_requests() -> None:
    backend = InMemoryDebounceBackend(window_seconds=60)

    backend.record_event("acme/repo", 101, arrived_at=100.0)
    backend.record_event("acme/repo", 102, arrived_at=120.0)

    assert backend.pull_ready(now=160.0) == {DebounceKey("acme/repo", 101)}
    assert backend.is_ready("acme/repo", 102, now=160.0) is False


def test_trigger_consumes_and_new_event_resets_window() -> None:
    backend = InMemoryDebounceBackend(window_seconds=60)

    backend.record_event("acme/repo", 101, arrived_at=100.0)
    assert backend.pull_ready(now=160.0) == {DebounceKey("acme/repo", 101)}
    assert backend.is_ready("acme/repo", 101, now=160.0) is False

    backend.record_event("acme/repo", 101, arrived_at=170.0)
    assert backend.is_ready("acme/repo", 101, now=220.0) is False
    assert backend.is_ready("acme/repo", 101, now=230.0) is True
    assert backend.pull_ready(now=230.0) == {DebounceKey("acme/repo", 101)}


def test_window_seconds_can_be_updated_safely() -> None:
    backend = InMemoryDebounceBackend(window_seconds=60)

    backend.record_event("acme/repo", 101, arrived_at=100.0)
    backend.set_window_seconds(30)

    assert backend.is_ready("acme/repo", 101, now=130.0) is True
