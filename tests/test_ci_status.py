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
