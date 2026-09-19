import pytest

from jev_pr_review import (
    DEFAULT_CONFIG,
    check_threshold,
    decide_verdict,
    evaluate_hard_gates,
    parse_threshold,
)


def test_blocked_path_gate_fires():
    reasons = evaluate_hard_gates(
        files_changed=["src/auth/login.py", "README.md"],
        total_lines_changed=10,
        ci_status="all checks passing",
        blocked_paths=["**/auth/**"],
        max_lines=400,
    )
    assert any(r["code"] == "blocked_path" for r in reasons)


def test_max_lines_gate_fires():
    reasons = evaluate_hard_gates(
        files_changed=["README.md"],
        total_lines_changed=500,
        ci_status="all checks passing",
        blocked_paths=[],
        max_lines=400,
    )
    assert any(r["code"] == "too_large" for r in reasons)


def test_ci_not_green_gate_fires():
    reasons = evaluate_hard_gates(
        files_changed=["README.md"],
        total_lines_changed=1,
        ci_status="failing",
        blocked_paths=[],
        max_lines=400,
    )
    assert any(r["code"] == "ci_not_green" for r in reasons)


def test_no_gate_fires_on_clean_pr():
    reasons = evaluate_hard_gates(
        files_changed=["README.md"],
        total_lines_changed=3,
        ci_status="all checks passing",
        blocked_paths=["**/auth/**"],
        max_lines=400,
    )
    assert reasons == []


def test_gates_use_fnmatch_glob_semantics():
    reasons = evaluate_hard_gates(
        files_changed=["Dockerfile"],
        total_lines_changed=1,
        ci_status="all checks passing",
        blocked_paths=["Dockerfile"],
        max_lines=400,
    )
    assert any("Dockerfile" in p for r in reasons for p in r.get("paths", []))


@pytest.mark.parametrize(
    "expr,value,expected",
    [
        ("< 1.5", 1.0, True),
        ("< 1.5", 1.5, False),
        ("> 0.80", 0.9, True),
        ("> 0.80", 0.8, False),
        ("<= 400", 400, True),
        (">= 0.25", 0.25, True),
    ],
)
def test_check_threshold(expr, value, expected):
    assert check_threshold(value, expr) is expected


def test_parse_threshold_rejects_garbage():
    with pytest.raises(ValueError):
        parse_threshold("banana 1.5")


def test_decide_verdict_network_failure_is_always_escalate_never_automerge():
    verdict, reasons = decide_verdict(
        aggregated={"risk_level": 0.0, "hidden_scope": 0.0, "silent_failure": 0.0, "diff_matches_title": 1.0},
        files_changed=["README.md"],
        total_lines_changed=1,
        ci_status="all checks passing",
        config=DEFAULT_CONFIG,
        network_failure=True,
    )
    assert verdict == "escalate"
    assert any(r["code"] == "review_unreachable" for r in reasons)


def test_decide_verdict_hard_gate_beats_perfect_scores():
    verdict, reasons = decide_verdict(
        aggregated={"risk_level": 0.0, "hidden_scope": 0.0, "silent_failure": 0.0, "diff_matches_title": 1.0},
        files_changed=["src/auth/login.py"],
        total_lines_changed=5,
        ci_status="all checks passing",
        config=DEFAULT_CONFIG,
    )
    assert verdict == "escalate"
    assert any(r["code"] == "blocked_path" for r in reasons)


def test_decide_verdict_automerge_when_everything_clean():
    verdict, reasons = decide_verdict(
        aggregated={"risk_level": 0.2, "hidden_scope": 0.0, "silent_failure": 0.0, "diff_matches_title": 0.95},
        files_changed=["README.md"],
        total_lines_changed=3,
        ci_status="all checks passing",
        config=DEFAULT_CONFIG,
    )
    assert verdict == "automerge"
    assert reasons


def test_decide_verdict_escalates_on_high_risk_even_without_gates():
    verdict, reasons = decide_verdict(
        aggregated={"risk_level": 2.9, "hidden_scope": 0.0, "silent_failure": 0.0, "diff_matches_title": 0.95},
        files_changed=["src/payments/charge.py"],
        total_lines_changed=3,
        ci_status="all checks passing",
        config=DEFAULT_CONFIG,
    )
    assert verdict == "escalate"
    assert any(r.get("dimension") == "risk_level" for r in reasons)
