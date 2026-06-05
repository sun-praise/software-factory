from __future__ import annotations

import logging

from app.services.hooks import _read_metadata


def test_read_metadata_valid_dict() -> None:
    result = _read_metadata('{"key": "val"}')
    assert result == {"key": "val"}


def test_read_metadata_none_or_empty() -> None:
    assert _read_metadata(None) == {}
    assert _read_metadata("") == {}


def test_read_metadata_corrupted_json_warns(caplog) -> None:  # type: ignore[no-untyped-def]
    with caplog.at_level(logging.WARNING, logger="app.services.hooks"):
        result = _read_metadata("{broken")
    assert result == {}
    assert any("Failed to parse metadata JSON" in m for m in caplog.messages)


def test_read_metadata_non_dict_warns(caplog) -> None:  # type: ignore[no-untyped-def]
    with caplog.at_level(logging.WARNING, logger="app.services.hooks"):
        result = _read_metadata("[1, 2, 3]")
    assert result == {}
    assert any("Metadata is not a dict" in m for m in caplog.messages)


def test_read_metadata_valid_does_not_warn(caplog) -> None:  # type: ignore[no-untyped-def]
    with caplog.at_level(logging.WARNING, logger="app.services.hooks"):
        _read_metadata('{"key": "val"}')
    assert len(caplog.messages) == 0
