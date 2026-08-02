from saipet.scorer import gate, score


def test_gate_bands():
    assert gate(0) == "ignore"
    assert gate(59.9) == "ignore"
    assert gate(60) == "review"
    assert gate(79.9) == "review"
    assert gate(80) == "priority"
    assert gate(100) == "priority"


def test_score_sums_and_clamps_each_signal():
    full = score(
        {
            "problem_match": 40,
            "audience_fit": 20,
            "workflow_fit": 15,
            "protocol_fit": 20,
        }
    )
    assert full == 95

    # a runaway single signal can't exceed its own weight
    over_claimed = score({"problem_match": 999})
    assert over_claimed == 40


def test_already_solved_pulls_score_down():
    base = score({"problem_match": 40, "audience_fit": 20})
    penalized = score({"problem_match": 40, "audience_fit": 20, "already_solved": 999})
    assert penalized == base - 25


def test_missing_signals_default_to_zero():
    assert score({}) == 0
