from jev_pr_review import CHARS_PER_TOKEN, build_file_state, estimate_tokens, truncate_diff


def test_short_diff_is_not_truncated():
    diff = "a" * 100
    result, was_truncated = truncate_diff(diff, max_tokens=1000)
    assert result == diff
    assert was_truncated is False


def test_long_diff_is_truncated_by_the_middle():
    head_marker = "HEAD" * 100
    tail_marker = "TAIL" * 100
    middle = "x" * 200_000
    diff = head_marker + middle + tail_marker

    result, was_truncated = truncate_diff(diff, max_tokens=1000)

    assert was_truncated is True
    assert result.startswith(head_marker[:50])
    assert result.endswith(tail_marker[-50:])
    assert "truncated" in result
    assert len(result) < len(diff)


def test_truncated_diff_respects_token_budget_roughly():
    diff = "y" * 500_000
    max_tokens = 24_000
    result, was_truncated = truncate_diff(diff, max_tokens=max_tokens)
    assert was_truncated is True
    assert len(result) <= max_tokens * CHARS_PER_TOKEN + 1


def test_estimate_tokens_uses_four_chars_per_token():
    assert estimate_tokens("a" * 400) == 100


def test_build_file_state_marks_diff_truncated_flag():
    state = build_file_state(
        pr_title="Fix bug",
        pr_body="",
        file_path="src/foo.py",
        diff="z" * 200_000,
        lines_added=10,
        lines_deleted=5,
        has_test_changes=False,
        ci_status="all checks passing",
    )
    assert state["diff_truncated"] is True
    assert state["file_path"] == "src/foo.py"


def test_build_file_state_omits_truncated_flag_when_not_truncated():
    state = build_file_state(
        pr_title="Fix typo",
        pr_body="",
        file_path="README.md",
        diff="small diff",
        lines_added=1,
        lines_deleted=1,
        has_test_changes=False,
        ci_status="all checks passing",
    )
    assert "diff_truncated" not in state
