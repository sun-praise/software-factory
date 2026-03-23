from app.config import Settings
from app.services.filter import (
    get_filter_reason,
    is_bot_actor,
    is_managed_repo,
    is_noise_actor,
    is_noise_comment,
    should_filter_event,
)


def test_settings_parse_m6_list_values_from_csv() -> None:
    settings = Settings.model_validate(
        {
            "bot_logins": "github-actions[bot], dependabot[bot]",
            "noise_comment_patterns": "^/retest\\b, ^/resolve\\b",
            "managed_repo_prefixes": "acme/, widgets/",
        }
    )

    assert settings.bot_logins == ("github-actions[bot]", "dependabot[bot]")
    assert settings.noise_comment_patterns == (r"^/retest\b", r"^/resolve\b")
    assert settings.managed_repo_prefixes == ("acme/", "widgets/")


def test_is_bot_actor_matches_configured_login_and_github_bot_suffix() -> None:
    assert is_bot_actor("github-actions[bot]") is True
    assert is_bot_actor("ci-helper", bot_logins=("ci-helper",)) is True
    assert is_bot_actor("reviewer-1", bot_logins=("ci-helper",)) is False


def test_is_noise_actor_matches_autofix_comment_author() -> None:
    assert (
        is_noise_actor(
            "software-factory[bot]",
            autofix_comment_author="software-factory[bot]",
        )
        is True
    )
    assert (
        is_noise_actor(
            "reviewer-1",
            autofix_comment_author="software-factory[bot]",
        )
        is False
    )


def test_is_noise_comment_matches_regex_case_insensitively() -> None:
    assert is_noise_comment("/Retest please", noise_comment_patterns=(r"^/retest\b",))
    assert not is_noise_comment(
        "Please take another look", noise_comment_patterns=(r"^/retest\b",)
    )


def test_is_managed_repo_allows_all_when_prefixes_empty() -> None:
    assert is_managed_repo("acme/widgets", managed_repo_prefixes=()) is True


def test_is_managed_repo_matches_prefixes() -> None:
    assert is_managed_repo("acme/widgets", managed_repo_prefixes=("acme/",)) is True
    assert is_managed_repo("other/widgets", managed_repo_prefixes=("acme/",)) is False


def test_get_filter_reason_prioritizes_repo_actor_then_comment() -> None:
    assert (
        get_filter_reason(
            "other/widgets",
            actor="reviewer-1",
            body="/retest",
            managed_repo_prefixes=("acme/",),
            noise_comment_patterns=(r"^/retest\b",),
        )
        == "unmanaged_repo"
    )
    assert (
        get_filter_reason(
            "acme/widgets",
            actor="ci-helper",
            body="Looks good",
            managed_repo_prefixes=("acme/",),
            bot_logins=("ci-helper",),
        )
        == "noise_actor"
    )
    assert (
        get_filter_reason(
            "acme/widgets",
            actor="reviewer-1",
            body="/retest",
            managed_repo_prefixes=("acme/",),
            noise_comment_patterns=(r"^/retest\b",),
        )
        == "noise_comment"
    )


def test_should_filter_event_false_for_managed_human_signal() -> None:
    assert (
        should_filter_event(
            "acme/widgets",
            actor="reviewer-1",
            body="Please fix the failing test",
            managed_repo_prefixes=("acme/",),
            noise_comment_patterns=(r"^/retest\b",),
        )
        is False
    )
