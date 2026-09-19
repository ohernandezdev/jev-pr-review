"""The mean risk compensates; the tail needs its own condition."""

import pytest

import jev_pr_review as J


def _score(value, probabilities):
    return {"type": "score", "score": value, "probabilities": probabilities}


def test_a_bimodal_file_slips_under_a_mean_threshold():
    """50% cosmetic + 50% catastrophic averages to exactly the launch limit."""
    answer = _score(1.5, {"0": 0.5, "1": 0.0, "2": 0.0, "3": 0.5})
    assert J.check_threshold(answer["score"], "< 1.5") is False  # exactly at the edge
    assert J.top_level_probability(answer) == pytest.approx(0.5)
    # The tail condition is what actually catches it.
    assert J.check_threshold(J.top_level_probability(answer), "< 0.15") is False


def test_a_genuinely_low_risk_file_passes_both():
    answer = _score(0.2, {"0": 0.8, "1": 0.2, "2": 0.0, "3": 0.0})
    assert J.check_threshold(answer["score"], "< 1.5")
    assert J.check_threshold(J.top_level_probability(answer), "< 0.15")


def test_concentrated_mid_risk_has_a_small_tail():
    """The real measurement behind a 1.85: mostly level 2, nothing on level 3."""
    answer = _score(1.85, {"0": 0.0, "1": 0.15, "2": 0.85, "3": 0.0})
    assert J.top_level_probability(answer) == pytest.approx(0.0)  # measured, not missing


def test_missing_distribution_is_absent_not_zero():
    """A missing distribution must not read as a reassuring 0%."""
    assert J.top_level_probability({"type": "score", "score": 2.0}) is None
    aggregated = J.aggregate_max([{"risk_level": {"type": "score", "score": 2.0}}])
    assert "worst_case_risk" not in aggregated


def test_aggregation_exposes_the_tail_per_file():
    quiet = {"risk_level": _score(0.1, {"0": 0.9, "1": 0.1, "2": 0.0, "3": 0.0})}
    bimodal = {"risk_level": _score(1.5, {"0": 0.5, "1": 0.0, "2": 0.0, "3": 0.5})}
    aggregated = J.aggregate_max([quiet, bimodal])
    assert aggregated["risk_level"] == pytest.approx(1.5)
    assert aggregated["worst_case_risk"] == pytest.approx(0.5)


def test_the_tail_blocks_a_verdict_the_mean_would_have_allowed():
    verdict, reasons = J.decide_verdict(
        aggregated={
            "risk_level": 1.4,
            "worst_case_risk": 0.45,
            "hidden_scope": 0.02,
            "silent_failure_weighted": 0.10,
            "diff_matches_title": 0.95,
        },
        files_changed=["src/util.py"],
        total_lines_changed=10,
        ci_status="success",
        config={
            "automerge_when": {
                "max_risk_level": "< 1.5",
                "worst_case_risk": "< 0.15",
                "hidden_scope": "< 0.25",
                "silent_failure_weighted": "< 0.30",
                "diff_matches_title": "> 0.80",
                "max_lines": 400,
            },
            "blocked_paths": [],
        },
        network_failure=False,
    )
    assert verdict == "escalate"
    assert any(r.get("dimension") == "worst_case_risk" for r in reasons), reasons
