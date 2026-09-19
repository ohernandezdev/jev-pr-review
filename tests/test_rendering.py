"""The comment has to be readable by whoever decides to merge, not only by its author."""

import re

import jev_pr_review as J

SCORES = {
    "risk_level": 1.76,
    "diff_matches_title": 0.77,
    "hidden_scope": 0.95,
    "silent_failure": 0.55,
    "silent_failure_weighted": 0.32,
    "tests_expected": 0.84,
}


def render(verdict="escalate", reasons=None, scores=None):
    return J.render_comment(
        verdict=verdict,
        reasons=reasons if reasons is not None else [{"code": "blocked_path", "paths": [".github/workflows/ci.yml"]}],
        aggregated=scores if scores is not None else SCORES,
        total_input_tokens=10042,
        files_reviewed=4,
    )


def test_probabilities_are_shown_as_percentages():
    body = render()
    readable = body.split("<details>")[0]
    assert "95%" in readable and "77%" in readable and "32%" in readable
    assert not re.search(r"\b0\.\d\d\b", readable), "no bare decimals above the fold"


def test_raw_scores_stay_available_but_folded_away():
    body = render()
    assert "<details><summary>Raw scores</summary>" in body
    assert "0.95" in body.split("<details>")[1]


def test_headline_says_what_to_do_in_plain_words():
    assert "Someone should look at this" in render(verdict="escalate")
    assert "safe to merge" in render(verdict="automerge", reasons=[{"code": "all_clear"}])


def test_every_question_is_a_sentence_not_a_field_name():
    readable = render().split("<details>")[0]
    for field in ("risk_level", "hidden_scope", "silent_failure_weighted", "diff_matches_title"):
        assert field not in readable, f"{field} leaked into the readable part"
    assert "Does the title describe the whole change?" in readable


def test_risk_is_a_word_not_a_number_out_of_three():
    readable = render().split("<details>")[0]
    assert "Medium" in readable and "shared logic" in readable
    assert "1.76" not in readable


def test_risk_bands_cover_the_whole_range():
    assert J.risk_label(0.0)[1] == "None"
    assert J.risk_label(1.2)[1] == "Low"
    assert J.risk_label(1.76)[1] == "Medium"
    assert J.risk_label(3.0)[1] == "High"


def test_answer_labels_read_in_the_direction_that_matters():
    # A high "does it match its title" is reassuring; a high "hidden scope" is not.
    assert J.answer_label("diff_matches_title", 0.95)[1] == "Yes"
    assert J.answer_label("diff_matches_title", 0.95)[0] == J.answer_label("hidden_scope", 0.05)[0]
    assert J.answer_label("hidden_scope", 0.95)[0] != J.answer_label("diff_matches_title", 0.95)[0]


def test_wording_carries_the_direction_so_the_number_only_confirms_it():
    """"No -- 32% sure" reads as doubt about the No. The wording must say it."""
    assert J.answer_label("hidden_scope", 0.05)[1] == "No"
    assert J.answer_label("hidden_scope", 0.95)[1] == "Yes"
    assert J.answer_label("hidden_scope", 0.32)[1] == J.UNSURE
    table = [l for l in render().splitlines() if l.startswith("| ") and "?" in l]
    assert table and all("sure" not in row for row in table)


def test_file_count_is_not_written_as_file_s():
    assert "1 file reviewed" in render(scores=SCORES).replace("4 files", "1 file")
    body = J.render_comment(verdict="escalate", reasons=[{"code": "all_clear"}],
                            aggregated=SCORES, total_input_tokens=1, files_reviewed=1)
    assert "1 file reviewed" in body and "file(s)" not in body


def test_percentages_round_to_whole_numbers():
    assert J.as_percent(0.777) == "78%"
    assert J.as_percent(0.0) == "0%"
    assert J.as_percent(1.0) == "100%"


def test_small_cost_is_words_not_six_decimals():
    readable = render().split("<details>")[0]
    assert "less than a cent" in render()
    assert "0.000422" not in readable


def test_every_reason_code_renders_a_sentence():
    codes = [
        {"code": "blocked_path", "paths": ["a.yml"]},
        {"code": "too_large", "lines": 812, "limit": 400},
        {"code": "ci_unreadable"},
        {"code": "ci_not_green", "status": "pending"},
        {"code": "ci_not_green", "status": "failure"},
        {"code": "ci_not_green", "status": "no checks"},
        {"code": "review_unreachable"},
        {"code": "no_score", "dimension": "hidden_scope"},
        {"code": "dimension", "dimension": "risk_level", "value": 3.0},
        {"code": "dimension", "dimension": "hidden_scope", "value": 0.95},
        {"code": "all_clear"},
    ]
    for reason in codes:
        text = J.humanize_reason(reason)
        assert text and text[0].isupper(), reason
        assert "{" not in text and "code" not in text, reason
