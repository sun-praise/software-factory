from app.services.filter import (
    get_filter_reason,
    is_noise_actor,
    is_noise_comment,
    should_filter_event,
)


def test_autofix_comment_author_filtered() -> None:
    assert is_noise_actor(
        "software-factory[bot]",
        autofix_comment_author="software-factory[bot]",
    )
    assert is_noise_actor(
        "Software-Factory[Bot]",
        autofix_comment_author="software-factory[bot]",
    )
    assert not is_noise_actor(
        "reviewer-1",
        autofix_comment_author="software-factory[bot]",
    )


def test_configured_bot_filtered() -> None:
    assert is_noise_actor(
        "dependabot[bot]",
        bot_logins=("dependabot[bot]",),
        autofix_comment_author="software-factory[bot]",
    )
    assert is_noise_actor(
        "renovate",
        bot_logins=("renovate",),
        autofix_comment_author="software-factory[bot]",
    )
    assert not is_noise_actor(
        "human-reviewer",
        bot_logins=("dependabot[bot]", "renovate"),
        autofix_comment_author="software-factory[bot]",
    )


def test_github_bot_suffix_filtered() -> None:
    assert is_noise_actor("github-actions[bot]")
    assert is_noise_actor("codecov[bot]")
    assert is_noise_actor("dependabot[bot]")
    assert not is_noise_actor("human-reviewer")
    assert not is_noise_actor("developer")


def test_noise_comment_filtered() -> None:
    assert is_noise_comment(
        "/retest please",
        noise_comment_patterns=(r"^/retest\b",),
    )
    assert is_noise_comment(
        "/resolve this issue",
        noise_comment_patterns=(r"^/resolve\b",),
    )
    assert not is_noise_comment(
        "Please take another look",
        noise_comment_patterns=(r"^/retest\b", r"^/resolve\b"),
    )
    assert not is_noise_comment(
        "I think we should /retest this",
        noise_comment_patterns=(r"^/retest\b",),
    )


def test_multi_round_no_loop() -> None:
    repo = "acme/widgets"
    autofix_author = "software-factory[bot]"
    bot_logins = ("dependabot[bot]", "renovate")
    noise_patterns = (r"^/retest\b", r"^/resolve\b")

    assert not should_filter_event(
        repo,
        actor="human-reviewer",
        body="Please fix the lint errors",
        managed_repo_prefixes=("acme/",),
        bot_logins=bot_logins,
        noise_comment_patterns=noise_patterns,
        autofix_comment_author=autofix_author,
    )

    assert should_filter_event(
        repo,
        actor=autofix_author,
        body="Auto-fixed lint errors",
        managed_repo_prefixes=("acme/",),
        bot_logins=bot_logins,
        noise_comment_patterns=noise_patterns,
        autofix_comment_author=autofix_author,
    )

    assert should_filter_event(
        repo,
        actor="dependabot[bot]",
        body="Updated dependencies",
        managed_repo_prefixes=("acme/",),
        bot_logins=bot_logins,
        noise_comment_patterns=noise_patterns,
        autofix_comment_author=autofix_author,
    )

    assert should_filter_event(
        repo,
        actor="human-reviewer",
        body="/retest please",
        managed_repo_prefixes=("acme/",),
        bot_logins=bot_logins,
        noise_comment_patterns=noise_patterns,
        autofix_comment_author=autofix_author,
    )

    assert (
        get_filter_reason(
            repo,
            actor=autofix_author,
            body="Fixed",
            managed_repo_prefixes=("acme/",),
            bot_logins=bot_logins,
            noise_comment_patterns=noise_patterns,
            autofix_comment_author=autofix_author,
        )
        == "noise_actor"
    )

    assert (
        get_filter_reason(
            repo,
            actor="human-reviewer",
            body="/retest",
            managed_repo_prefixes=("acme/",),
            bot_logins=bot_logins,
            noise_comment_patterns=noise_patterns,
            autofix_comment_author=autofix_author,
        )
        == "noise_comment"
    )
