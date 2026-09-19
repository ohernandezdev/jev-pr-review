"""CI status must ignore this reviewer's own check run."""

import jev_pr_review as J


def _run(name, status="completed", conclusion="success", run_id="999"):
    return {
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "html_url": f"https://github.com/o/r/actions/runs/{run_id}/job/1",
    }


def test_own_run_is_excluded():
    """The reviewer is itself a check; counting it makes green unreachable.

    This is the bug the first live run caught: `mergeable_state` read
    'unstable' because the review job was still in progress, so the verdict
    escalated on the reviewer's own existence.
    """
    runs = [_run("review", status="in_progress", conclusion=None, run_id="42"), _run("tests", run_id="7")]
    assert J.compute_ci_status(runs, own_run_id="42") == "success"


def test_without_the_exclusion_the_same_input_is_pending():
    runs = [_run("review", status="in_progress", conclusion=None, run_id="42"), _run("tests", run_id="7")]
    assert J.compute_ci_status(runs, own_run_id="") == "pending"


def test_a_repo_with_no_other_checks_is_not_green():
    assert J.compute_ci_status([_run("review", run_id="42")], own_run_id="42") == "no checks"
    assert J.compute_ci_status([]) == "no checks"


def test_failure_and_pending_are_distinguished():
    assert J.compute_ci_status([_run("tests", conclusion="failure")]) == "failure"
    assert J.compute_ci_status([_run("tests", status="queued", conclusion=None)]) == "pending"


def test_neutral_and_skipped_count_as_passing():
    assert J.compute_ci_status([_run("a", conclusion="neutral"), _run("b", conclusion="skipped")]) == "success"


def test_only_success_passes_the_hard_gate():
    for status, green in (("success", True), ("pending", False), ("failure", False), ("no checks", False)):
        reasons = J.evaluate_hard_gates(
            files_changed=["README.md"],
            total_lines_changed=2,
            ci_status=status,
            blocked_paths=[],
            max_lines=400,
        )
        assert (reasons == []) is green, (status, reasons)


def test_await_polls_until_other_checks_settle():
    """A single snapshot races the other workflows; the wait is what fixes it."""
    snapshots = [
        [_run("review", status="in_progress", conclusion=None, run_id="42")],          # only us
        [_run("review", status="in_progress", conclusion=None, run_id="42"),
         _run("tests", status="queued", conclusion=None, run_id="7")],                 # tests appear
        [_run("review", status="in_progress", conclusion=None, run_id="42"),
         _run("tests", run_id="7")],                                                   # tests pass
    ]
    calls = {"n": 0}

    def fake_fetch(repo, sha, token):
        i = min(calls["n"], len(snapshots) - 1)
        calls["n"] += 1
        return snapshots[i]

    original, J.fetch_check_runs = J.fetch_check_runs, fake_fetch
    try:
        status = J.await_ci_status("o/r", "abc", None, own_run_id="42",
                                   sleep_fn=lambda _: None)
    finally:
        J.fetch_check_runs = original
    assert status == "success"
    assert calls["n"] == 3


def test_await_gives_up_at_the_deadline():
    clock = {"t": 0.0}

    def fake_fetch(repo, sha, token):
        return [_run("tests", status="queued", conclusion=None, run_id="7")]

    def tick(seconds):
        clock["t"] += seconds

    original, J.fetch_check_runs = J.fetch_check_runs, fake_fetch
    try:
        status = J.await_ci_status("o/r", "abc", None, timeout_s=30.0, poll_s=15.0,
                                   sleep_fn=tick, now_fn=lambda: clock["t"])
    finally:
        J.fetch_check_runs = original
    assert status == "pending"


def test_await_without_a_sha_reports_no_checks():
    assert J.await_ci_status("o/r", "", None) == "no checks"


def test_unreadable_check_runs_are_not_silently_empty():
    """A 403 must not look like a repository with no CI."""

    def boom(repo, sha, token):
        raise J.CheckRunsUnreadable("403 Forbidden -- missing `checks: read`")

    original, J.fetch_check_runs = J.fetch_check_runs, boom
    try:
        assert J.await_ci_status("o/r", "abc", None, sleep_fn=lambda _: None) == "unreadable"
    finally:
        J.fetch_check_runs = original


def test_unreadable_blocks_with_an_actionable_reason():
    reasons = J.evaluate_hard_gates(
        files_changed=["README.md"],
        total_lines_changed=2,
        ci_status="unreadable",
        blocked_paths=[],
        max_lines=400,
    )
    assert any("checks: read" in r for r in reasons), reasons


def test_fetch_check_runs_calls_github_correctly():
    """Regression: the call passed a relative path and a positional token.

    That raised TypeError, a broad except turned it into 'unreadable', and the
    reviewer confidently blamed a missing permission for an hour.
    """
    seen = {}

    def fake_request(url, *, token, method="GET", body=None):
        seen["url"] = url
        seen["token"] = token
        return {"check_runs": [_run("tests")]}

    original, J._github_request = J._github_request, fake_request
    try:
        runs = J.fetch_check_runs("o/r", "abc123", "tok")
    finally:
        J._github_request = original

    assert seen["url"].startswith("https://api.github.com/repos/o/r/commits/abc123/check-runs")
    assert seen["token"] == "tok"
    assert len(runs) == 1


def test_fetch_check_runs_does_not_swallow_programming_errors():
    def boom(url, *, token, method="GET", body=None):
        raise TypeError("wrong arguments")

    original, J._github_request = J._github_request, boom
    try:
        import pytest as _pytest
        with _pytest.raises(TypeError):
            J.fetch_check_runs("o/r", "abc", "tok")
    finally:
        J._github_request = original
