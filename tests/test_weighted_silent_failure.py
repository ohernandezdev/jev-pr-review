"""silent_failure is only meaningful scaled by the same file's risk_level."""

import os

import pytest

import jev_pr_review as J


def _answers(risk: float, silent: float) -> dict:
    return {
        "risk_level": {"type": "score", "score": risk},
        "silent_failure": {"type": "noul", "noul": silent},
    }


def test_weighted_value_scales_by_risk():
    derived = J.derive_dimensions({"risk_level": 3.0, "silent_failure": 0.9})
    assert derived["silent_failure_weighted"] == pytest.approx(0.9)

    derived = J.derive_dimensions({"risk_level": 0.0, "silent_failure": 0.9})
    assert derived["silent_failure_weighted"] == pytest.approx(0.0)


def test_weighted_is_absent_when_either_input_is_missing():
    assert "silent_failure_weighted" not in J.derive_dimensions({"risk_level": 2.0})
    assert "silent_failure_weighted" not in J.derive_dimensions({"silent_failure": 0.8})


def test_pairing_happens_per_file_not_across_files():
    """The README's silence must not pair with the auth file's risk.

    Cross-pairing would yield 0.57 * (3.0/3) = 0.57 and escalate a PR whose
    files are each individually harmless.
    """
    readme = _answers(risk=0.0, silent=0.57)
    auth = _answers(risk=3.0, silent=0.20)

    aggregated = J.aggregate_max([readme, auth])

    assert aggregated["silent_failure"] == pytest.approx(0.57)  # raw max, misleading
    assert aggregated["risk_level"] == pytest.approx(3.0)
    assert aggregated["silent_failure_weighted"] == pytest.approx(0.20)


def test_raw_silent_failure_no_longer_gates_the_verdict():
    """A high raw silent_failure on a zero-risk change must not block."""
    verdict, reasons = J.decide_verdict(
        aggregated={
            "risk_level": 0.0,
            "hidden_scope": 0.02,
            "silent_failure": 0.57,
            "silent_failure_weighted": 0.0,
            "diff_matches_title": 0.95,
        },
        files_changed=["README.md"],
        total_lines_changed=2,
        ci_status="success",
        config={
            "automerge_when": {
                "max_risk_level": "< 1.5",
                "hidden_scope": "< 0.25",
                "silent_failure_weighted": "< 0.30",
                "diff_matches_title": "> 0.80",
                "max_lines": 400,
            },
            "blocked_paths": [],
        },
        network_failure=False,
    )
    assert verdict == "automerge", reasons


@pytest.mark.e2e
@pytest.mark.skipif(not os.environ.get("TYPESAFE_API_KEY"), reason="needs TYPESAFE_API_KEY")
def test_weighting_orders_real_diffs_that_raw_scores_invert():
    """Against the live model: raw inverts README vs auth, weighted does not."""
    questions = {k: J.QUESTIONS[k] for k in ("risk_level", "silent_failure")}
    base = {"lines_added": 1, "lines_deleted": 1, "has_test_changes": False, "ci_status": "all checks passing"}

    readme = dict(base, pr_title="docs: fix typo in README", file_path="README.md",
                  diff="--- a/README.md\n+++ b/README.md\n@@ -12,1 +12,1 @@\n"
                       "-A moderaton layer for chat.\n+A moderation layer for chat.\n")
    swallow = dict(base, pr_title="chore: tidy up error handling", file_path="src/billing/charge.py",
                   diff="--- a/src/billing/charge.py\n+++ b/src/billing/charge.py\n@@ -30,4 +30,6 @@\n"
                        "-    resp = stripe.Charge.create(**kw)\n-    return resp\n"
                        "+    try:\n+        return stripe.Charge.create(**kw)\n"
                        "+    except Exception:\n+        return None\n")

    scored = {}
    for name, state in (("readme", readme), ("swallow", swallow)):
        answers = J.call_jev(state, questions)["answers"]
        scored[name] = J.derive_dimensions({
            "risk_level": answers["risk_level"]["score"],
            "silent_failure": answers["silent_failure"]["noul"],
        })

    # The swallowed-exception charge must dominate on the weighted dimension,
    # regardless of how the raw silence happens to land on prose.
    assert scored["swallow"]["silent_failure_weighted"] > scored["readme"]["silent_failure_weighted"] + 0.3
    assert scored["readme"]["silent_failure_weighted"] < 0.30
    assert scored["swallow"]["silent_failure_weighted"] >= 0.30
