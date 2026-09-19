from jev_pr_review import aggregate_max


def test_aggregate_max_picks_the_highest_score_per_dimension():
    per_file_answers = [
        {"risk_level": {"type": "score", "score": 0.4}, "hidden_scope": {"type": "noul", "noul": 0.1}},
        {"risk_level": {"type": "score", "score": 2.9}, "hidden_scope": {"type": "noul", "noul": 0.05}},
        {"risk_level": {"type": "score", "score": 1.2}, "hidden_scope": {"type": "noul", "noul": 0.6}},
    ]
    result = aggregate_max(per_file_answers)
    assert result["risk_level"] == 2.9
    assert result["hidden_scope"] == 0.6


def test_aggregate_max_never_averages():
    # One dangerous file among many trivial ones must not get diluted.
    per_file_answers = [{"risk_level": {"score": 0.0}} for _ in range(39)]
    per_file_answers.append({"risk_level": {"score": 3.0}})
    result = aggregate_max(per_file_answers)
    assert result["risk_level"] == 3.0


def test_aggregate_max_handles_missing_dimensions_per_file():
    per_file_answers = [
        {"risk_level": {"score": 1.0}},
        {"hidden_scope": {"noul": 0.9}},
    ]
    result = aggregate_max(per_file_answers)
    assert result == {"risk_level": 1.0, "hidden_scope": 0.9}


def test_aggregate_max_empty_input_returns_empty_dict():
    assert aggregate_max([]) == {}
