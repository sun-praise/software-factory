from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock
from fastapi.testclient import TestClient

from app.main import app
from app.db import connect_db
from app.services.queue import claim_next_queued_run
from app.services.agent_runner import run_once
from app.services import agent_runner
from tests.fixtures.e2e_fixtures import (
    setup_e2e_env,
    make_pull_request_review_payload,
    make_issue_comment_payload,
    post_webhook,
    make_mock_runner_ops,
    count_runs,
    count_review_events,
    get_run_by_id,
    get_pr_autofix_count,
    set_pr_autofix_count,
)


def _stub_pr_metadata(*, repo: str, pr_number: int) -> dict[str, object]:
    return {}


class TestE2ESuccessPath:
    def test_full_success_flow_from_webhook_to_runner(
        self,
        tmp_path: Path,
        monkeypatch,
    ):
        db_path = setup_e2e_env(tmp_path)
        mock_executor = MagicMock(
            return_value={"returncode": 0, "stdout": "ok", "stderr": ""}
        )
        mock_ops = make_mock_runner_ops()
        payload = make_pull_request_review_payload()
        monkeypatch.setattr(
            agent_runner,
            "_execute_agent_sdks",
            lambda **kwargs: (True, None, None, "claude_agent_sdk"),
        )
        monkeypatch.setattr(
            agent_runner,
            "_collect_pull_request_metadata",
            _stub_pr_metadata,
        )
        with TestClient(app) as client:
            resp = post_webhook(
                client=client,
                event_type="pull_request_review",
                payload=payload,
                secret="test-secret",
            )

            assert resp.status_code == 200
            data = resp.json()
            assert data["insert_status"] == "inserted"
            assert data["queue_status"] == "queued"
            assert data["queued_run_id"] is not None
            run_id = data["queued_run_id"]

            assert count_review_events(db_path, "acme/widgets", 42) == 1
            assert count_runs(db_path, "queued") == 1

            with connect_db() as conn:
                run = claim_next_queued_run(conn)
                assert run is not None
                assert run["id"] == run_id
                assert run["status"] == "running"

                result = run_once(
                    conn=conn,
                    run=run,
                    workspace_dir=str(tmp_path),
                    executor=mock_executor,
                    ops=mock_ops,
                )

                assert result["status"] == "success"
                assert result["commit_sha"] == "deadbeef1234"

            final_run = get_run_by_id(db_path, run_id)
            assert final_run is not None
            assert final_run["status"] == "success"
            assert final_run["commit_sha"] == "deadbeef1234"

            assert get_pr_autofix_count(db_path, "acme/widgets", 42) == 1


class TestE2EFilteredPath:
    def test_bot_comment_is_filtered_and_not_queued(self, tmp_path: Path) -> None:
        db_path = setup_e2e_env(tmp_path)
        payload = make_issue_comment_payload(
            repo="acme/widgets",
            issue_number=42,
            comment_id=3001,
            actor="dependabot[bot]",
            body="please update dependencies",
            is_pr=True,
        )
        with TestClient(app) as client:
            resp = post_webhook(
                client=client,
                event_type="issue_comment",
                payload=payload,
                secret="test-secret",
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("ignored") is True
        assert data.get("reason") == "noise_actor"
        assert count_runs(db_path) == 0
        assert count_review_events(db_path, "acme/widgets", 42) == 0


class TestE2ELimitPath:
    def test_autofix_limit_prevents_queueing(self, tmp_path: Path) -> None:
        db_path = setup_e2e_env(tmp_path)
        set_pr_autofix_count(db_path, "acme/widgets", 42, 3)
        payload = make_pull_request_review_payload(
            repo="acme/widgets",
            pr_number=42,
            review_id=2001,
        )
        with TestClient(app) as client:
            resp = post_webhook(
                client=client,
                event_type="pull_request_review",
                payload=payload,
                secret="test-secret",
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["insert_status"] == "inserted"
        assert data["queue_status"] == "autofix_limit_reached"
        assert data["queued_run_id"] is None
        assert count_runs(db_path) == 0


class TestE2ERetryPath:
    def test_failure_schedules_retry(self, tmp_path: Path, monkeypatch) -> None:
        db_path = setup_e2e_env(tmp_path)
        payload = make_pull_request_review_payload()
        mock_executor = MagicMock(
            return_value={"returncode": 0, "stdout": "ok", "stderr": ""}
        )
        mock_ops = make_mock_runner_ops(
            commit_success=False, commit_error="push_rejected"
        )
        monkeypatch.setattr(
            agent_runner,
            "_collect_pull_request_metadata",
            _stub_pr_metadata,
        )
        with TestClient(app) as client:
            resp = post_webhook(
                client=client,
                event_type="pull_request_review",
                payload=payload,
                secret="test-secret",
            )
        assert resp.status_code == 200
        run_id = resp.json()["queued_run_id"]
        assert run_id is not None

        with connect_db() as conn:
            run = claim_next_queued_run(conn)
            assert run is not None

            result = run_once(
                conn=conn,
                run=run,
                workspace_dir=str(tmp_path),
                executor=mock_executor,
                ops=mock_ops,
            )

        assert result["status"] == "retry_scheduled"

        final_run = get_run_by_id(db_path, run_id)
        assert final_run is not None
        assert final_run["status"] == "retry_scheduled"
        assert final_run["retry_after"] is not None


class TestE2EIdempotencyPath:
    def test_duplicate_webhook_returns_duplicate_status(self, tmp_path: Path) -> None:
        db_path = setup_e2e_env(tmp_path)
        payload = make_pull_request_review_payload()
        with TestClient(app) as client:
            first_resp = post_webhook(
                client=client,
                event_type="pull_request_review",
                payload=payload,
                secret="test-secret",
            )
            second_resp = post_webhook(
                client=client,
                event_type="pull_request_review",
                payload=payload,
                secret="test-secret",
            )

        assert first_resp.status_code == 200
        first_data = first_resp.json()
        assert first_data["insert_status"] == "inserted"
        assert first_data["queue_status"] == "queued"
        assert first_data["queued_run_id"] is not None

        assert second_resp.status_code == 200
        second_data = second_resp.json()
        assert second_data["insert_status"] == "duplicate"
        assert second_data["queue_status"] == "duplicate_event"
        assert second_data["queued_run_id"] is None

        assert count_runs(db_path) == 1
        assert count_review_events(db_path, "acme/widgets", 42) == 1
