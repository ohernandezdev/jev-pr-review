"""Changes driven by a red-team read of the comment by a non-technical reader.

Each test here pins one finding: the table has to affect the verdict, a shrug
has to look like a shrug, the title question is one question, tests are a fact,
and "sensitive" is three separate things.
"""

import pytest

import jev_pr_review as J

CONFIG = {
    "automerge_when": {
        "max_risk_level": "< 1.5",
        "hidden_scope": "< 0.25",
        "silent_failure_weighted": "< 0.30",
        "worst_case_risk": "< 0.15",
        "diff_matches_title": "> 0.80",
        "sensitive_area": "< 0.10",
        "max_lines": 400,
    },
    "blocked_paths": ["**/auth/**"],
}

CLEAN = {
    "risk_level": 0.2,
    "worst_case_risk": 0.0,
    "hidden_scope": 0.02,
    "silent_failure_weighted": 0.05,
    "diff_matches_title": 0.97,
    "sensitive_area": 0.01,
    "touches_money": 0.01,
    "touches_accounts": 0.0,
    "touches_personal_data": 0.01,
    "tests_expected": 0.2,
}


def decide(aggregated, **kwargs):
    kwargs.setdefault("files_changed", ["src/util.py"])
    kwargs.setdefault("total_lines_changed", 10)
    kwargs.setdefault("ci_status", "success")
    kwargs.setdefault("config", CONFIG)
    return J.decide_verdict(aggregated=aggregated, **kwargs)


# --- 1. the table has to participate in the verdict ---------------------------


def test_a_hard_gate_does_not_hide_the_dimensions_that_also_failed():
    """A blocked path used to short-circuit, leaving the table decorative."""
    aggregated = {**CLEAN, "hidden_scope": 0.95, "risk_level": 2.8}
    verdict, reasons = decide(aggregated, files_changed=["src/auth/login.py", "src/util.py"])
    assert verdict == "escalate"
    codes = [r["code"] for r in reasons]
    assert "blocked_path" in codes, reasons
    assert "changes_more_than_title" in codes, reasons
    assert any(r.get("dimension") == "risk_level" for r in reasons), reasons


def test_a_red_verdict_can_be_driven_by_the_table_alone():
    """No gate fires; the reasons come only from the scored dimensions."""
    verdict, reasons = decide({**CLEAN, "silent_failure_weighted": 0.80})
    assert verdict == "escalate"
    codes = {r["code"] for r in reasons}
    assert codes == {"dimension"}, reasons
    assert any(r["dimension"] == "silent_failure_weighted" for r in reasons)


def test_clean_scores_and_clean_gates_still_automerge():
    verdict, reasons = decide(CLEAN)
    assert verdict == "automerge", reasons


# --- 2. no green tick in the middle of the range ------------------------------


@pytest.mark.parametrize("value", [0.10, 0.38, 0.5, 0.62, 0.90])
def test_nothing_between_10_and_90_percent_is_ever_ticked(value):
    for dim in ("hidden_scope", "diff_matches_title", "worst_case_risk"):
        icon, word = J.answer_label(dim, value)
        assert icon == "⚠️", (dim, value)
        assert word == "Not confident either way"


def test_only_a_confident_reassuring_answer_gets_a_tick():
    assert J.answer_label("worst_case_risk", 0.02) == ("✅", "No")
    assert J.answer_label("diff_matches_title", 0.99) == ("✅", "Yes")
    # Confident, but in the alarming direction.
    assert J.answer_label("worst_case_risk", 0.99) == ("⚠️", "Yes")
    assert J.answer_label("diff_matches_title", 0.01) == ("⚠️", "No")


def test_the_shrug_is_labelled_as_a_shrug_not_as_probably():
    body = J.render_comment(
        verdict="escalate",
        reasons=[{"code": "all_clear"}],
        aggregated={**CLEAN, "silent_failure_weighted": 0.38},
        total_input_tokens=1,
    )
    readable = body.split("<details>")[0]
    assert "Not confident either way** (38%)" in readable
    assert "Probably" not in readable


# --- 3. the two title rows are one question -----------------------------------


def test_the_title_question_is_asked_once():
    readable = J.render_comment(
        verdict="escalate", reasons=[{"code": "all_clear"}],
        aggregated=CLEAN, total_input_tokens=1,
    ).split("<details>")[0]
    assert readable.count("Does the title describe the whole change?") == 1
    assert "Does it also change things its title" not in readable


def test_title_row_needs_both_signals_to_say_yes():
    assert J.title_scope_label(0.97, 0.02) == ("✅", "Yes")
    assert J.title_scope_label(0.97, 0.95)[1] == "No -- it changes more than it says"
    assert J.title_scope_label(0.61, 0.95)[1] == "No -- it changes more than it says"
    assert J.title_scope_label(0.61, 0.30)[1] == "Not confident either way"
    # Matching the title well is not enough while hidden scope is uncertain.
    assert J.title_scope_label(0.97, 0.30)[1] == "Not confident either way"


def test_both_dimensions_are_still_asked_of_jev_and_still_gate():
    assert "diff_matches_title" in J.QUESTIONS and "hidden_scope" in J.QUESTIONS
    _, reasons = decide({**CLEAN, "diff_matches_title": 0.40})
    assert any(r.get("dimension") == "diff_matches_title" for r in reasons)


def test_changing_more_than_the_title_says_leads_the_why_block():
    aggregated = {**CLEAN, "hidden_scope": 0.95, "risk_level": 2.8}
    _, reasons = decide(aggregated, files_changed=["src/auth/login.py"])
    assert reasons[0]["code"] == "changes_more_than_title", reasons
    body = J.render_comment(
        verdict="escalate", reasons=reasons, aggregated=aggregated, total_input_tokens=1
    )
    why = body.split("**Why**")[1].split("**What")[0].strip().splitlines()
    assert why[0].startswith("- It changes more than its title says")


# --- 4. tests are a fact we already have --------------------------------------


def test_the_tests_row_reports_the_fact_not_an_expectation():
    assert J.tests_label(True, 0.95) == ("✅", "Yes")
    assert J.tests_label(False, 0.10) == ("⚠️", "No")
    assert J.tests_label(False, 0.95)[1] == "No -- and a reviewer would expect them"


def test_missing_expected_tests_become_a_reason():
    _, reasons = decide({**CLEAN, "tests_expected": 0.95}, has_test_changes=False)
    assert any(r["code"] == "tests_missing_but_expected" for r in reasons)
    verdict, _ = decide({**CLEAN, "tests_expected": 0.95}, has_test_changes=True)
    assert verdict == "automerge"


def test_the_tests_row_is_only_rendered_when_the_fact_is_known():
    base = dict(verdict="escalate", reasons=[{"code": "all_clear"}],
                aggregated=CLEAN, total_input_tokens=1)
    assert "Does it include tests?" not in J.render_comment(**base)
    assert "Does it include tests?" in J.render_comment(**base, has_test_changes=True)
    assert "Would a reviewer expect tests" not in J.render_comment(**base, has_test_changes=True)


# --- 5. money, accounts and personal data are three questions -----------------


def test_the_three_sensitive_areas_are_separate_questions():
    for dim in ("touches_money", "touches_accounts", "touches_personal_data"):
        assert J.QUESTIONS[dim]["type"] == "noul"
        text = J.QUESTIONS[dim]["instructions"].lower()
        for forbidden in ("merge", "approve", "review"):
            assert forbidden not in text, (dim, forbidden)


def test_sensitive_area_is_the_max_of_the_three():
    aggregated = J.aggregate_max([
        {
            "touches_money": {"noul": 0.02},
            "touches_accounts": {"noul": 0.93},
            "touches_personal_data": {"noul": 0.11},
        }
    ])
    assert aggregated["sensitive_area"] == pytest.approx(0.93)


def test_the_row_names_which_area_it_is():
    assert J.sensitive_label(CLEAN) == ("✅", "No")
    hit = {**CLEAN, "touches_money": 0.95, "sensitive_area": 0.95}
    assert J.sensitive_label(hit) == ("⚠️", "Yes -- money")
    both = {**CLEAN, "touches_money": 0.95, "touches_personal_data": 0.97, "sensitive_area": 0.97}
    assert J.sensitive_label(both)[1] == "Yes -- money, personal data"
    assert J.sensitive_label({**CLEAN, "touches_accounts": 0.45})[1] == "Not confident either way"


def test_a_sensitive_area_over_the_gate_becomes_a_named_reason():
    aggregated = {**CLEAN, "touches_personal_data": 0.88, "sensitive_area": 0.88}
    verdict, reasons = decide(aggregated)
    assert verdict == "escalate"
    reason = next(r for r in reasons if r["code"] == "sensitive_area")
    assert reason["areas"] == ["personal data"]
    assert "personal data" in J.humanize_reason(reason)


# --- 6. the comment says which PR it is ---------------------------------------


def test_both_comments_carry_the_pr_facts():
    for verdict in ("escalate", "automerge"):
        body = J.render_comment(
            verdict=verdict,
            reasons=[{"code": "all_clear"}],
            aggregated=CLEAN,
            total_input_tokens=1,
            pr_title="Bump the retry budget",
            pr_author="@ohernandezdev",
            files_changed=3,
            lines_changed=42,
        )
        head = body.split("**Why**")[0]
        assert "Bump the retry budget" in head
        assert "@ohernandezdev" in head
        assert "3 files changed" in head and "42 lines changed" in head


def test_pr_facts_are_omitted_rather_than_invented():
    body = J.render_comment(verdict="automerge", reasons=[{"code": "all_clear"}],
                            aggregated=CLEAN, total_input_tokens=1)
    assert ">" not in body.split("**Why**")[0]


# --- 7. an escalation with no addressee ---------------------------------------


def test_escalate_headline_names_the_assignee_when_configured():
    body = J.render_comment(verdict="escalate", reasons=[{"code": "all_clear"}],
                            aggregated=CLEAN, total_input_tokens=1,
                            escalate_to="@platform-team")
    assert "assigned to: @platform-team" in body.splitlines()[2]


def test_no_assignee_is_invented_when_unconfigured():
    body = J.render_comment(verdict="escalate", reasons=[{"code": "all_clear"}],
                            aggregated=CLEAN, total_input_tokens=1)
    assert "assigned to" not in body


def test_the_green_headline_is_never_assigned_to_anyone():
    body = J.render_comment(verdict="automerge", reasons=[{"code": "all_clear"}],
                            aggregated=CLEAN, total_input_tokens=1,
                            escalate_to="@platform-team")
    assert "assigned to" not in body


# --- 8. the cost belongs with the technical detail ----------------------------


def test_the_cost_is_folded_away_with_the_raw_scores():
    body = J.render_comment(verdict="escalate", reasons=[{"code": "all_clear"}],
                            aggregated=CLEAN, total_input_tokens=10042, files_reviewed=4)
    readable, folded = body.split("<details>")
    assert "cost" not in readable.lower()
    assert "cost of this run" in folded
    assert "4 files reviewed." in body


def test_the_file_count_stays_outside_the_fold():
    body = J.render_comment(verdict="escalate", reasons=[{"code": "all_clear"}],
                            aggregated=CLEAN, total_input_tokens=1, files_reviewed=2)
    assert "2 files reviewed." in body.split("</details>")[1]


def _codes_generated_in_module() -> set:
    """Every reason code the module can emit, read from its own source."""
    import pathlib
    import re

    source = pathlib.Path(J.__file__).read_text(encoding="utf-8")
    return set(re.findall(r'"code": "([a-z_]+)"', source))


def test_every_generated_reason_code_has_a_sentence():
    """A code with no branch used to print a raw dict into the comment."""
    for code in _codes_generated_in_module():
        reason = {
            "code": code,
            "paths": ["a.yml"],
            "lines": 1,
            "limit": 400,
            "status": "pending",
            "dimension": "hidden_scope",
            "value": 0.5,
            "areas": ["accounts"],
        }
        text = J.humanize_reason(reason)
        assert "{" not in text and "'code'" not in text, (code, text)
        assert text[0].isupper(), (code, text)


def test_an_unknown_code_never_leaks_a_dict_to_the_reader():
    text = J.humanize_reason({"code": "something_new_nobody_wrote_yet"})
    assert "{" not in text and "code" not in text
    assert text.startswith("Something the review flagged")


def test_worst_case_risk_is_a_gate_but_not_a_second_risk_row():
    body = J.render_comment(
        verdict="escalate",
        reasons=[{"code": "all_clear"}],
        aggregated={"risk_level": 1.8, "worst_case_risk": 0.4, "diff_matches_title": 0.9,
                    "hidden_scope": 0.1, "silent_failure_weighted": 0.1, "sensitive_area": 0.01},
        total_input_tokens=1,
        files_reviewed=1,
    )
    readable, technical = body.split("<details>")
    assert readable.count("how bad is it") == 1
    assert "worst case" not in readable
    assert "worst_case_risk" in technical


def test_a_reason_never_claims_more_confidence_than_its_row():
    """The Why block said "It touches accounts" while the table said unsure."""
    unsure = J.humanize_reason({"code": "sensitive_area", "areas": ["accounts"],
                                "value": 0.72, "confident": False})
    certain = J.humanize_reason({"code": "sensitive_area", "areas": ["accounts"],
                                 "value": 0.95, "confident": True})
    assert unsure == "It may touch accounts"
    assert certain == "It touches accounts"


def test_reasons_are_statements_not_questions():
    for dim in ("silent_failure_weighted", "worst_case_risk", "hidden_scope", "diff_matches_title"):
        text = J.humanize_reason({"code": "dimension", "dimension": dim, "value": 0.42})
        assert "?" not in text, (dim, text)
        assert text[0].isupper()


def test_the_why_block_and_the_table_agree_on_sensitive_areas():
    body = J.render_comment(
        verdict="escalate",
        reasons=[{"code": "sensitive_area", "areas": ["accounts"], "value": 0.72, "confident": False}],
        aggregated={"risk_level": 1.0, "sensitive_area": 0.72, "touches_accounts": 0.72,
                    "diff_matches_title": 0.95, "hidden_scope": 0.02, "silent_failure_weighted": 0.05},
        total_input_tokens=1,
        files_reviewed=1,
    )
    readable = body.split("<details>")[0]
    assert "It may touch accounts" in readable
    assert "Not confident either way" in readable


def test_only_areas_actually_driving_the_signal_are_named():
    """Measured on a real PR: personal data peaked at 0.10 and was still named."""
    _, reasons = J.decide_verdict(
        aggregated={"risk_level": 1.0, "sensitive_area": 0.71, "touches_accounts": 0.71,
                    "touches_money": 0.05, "touches_personal_data": 0.10,
                    "diff_matches_title": 0.95, "hidden_scope": 0.02,
                    "silent_failure_weighted": 0.05, "worst_case_risk": 0.01},
        files_changed=["src/a.py"], total_lines_changed=10, ci_status="success",
        config={"automerge_when": {"sensitive_area": "< 0.10"}, "blocked_paths": []},
        network_failure=False,
    )
    named = [r for r in reasons if r["code"] == "sensitive_area"]
    assert named and named[0]["areas"] == ["accounts"], named
