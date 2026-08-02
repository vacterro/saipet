from saipet.scorer import gate, score
from saipet.signals import extract_signals
from saipet.sources.base import Candidate


def _candidate(title, body):
    return Candidate(source="fixture", id="x", title=title, body=body, permalink="https://x")


def test_three_plus_symptom_hits_no_solved_marker_lands_priority():
    c = _candidate(
        title="Lost context and session state after every agent handoff",
        body=(
            "Every time I resume my automation workflow after a checkpoint, "
            "my LLM agent loses all deterministic state from the prior session. "
            "I've tried hand-rolling my own recovery framework but it's fragile."
        ),
    )
    signals = extract_signals(c)
    assert gate(score(signals)) == "priority"


def test_solved_marker_scores_lower_than_same_post_without_it():
    unsolved = _candidate(
        title="Lost context and session state after every agent handoff",
        body="My automation workflow protocol loses deterministic checkpoint state on resume.",
    )
    solved = _candidate(
        title="Lost context and session state after every agent handoff",
        body=(
            "My automation workflow protocol loses deterministic checkpoint state on resume. "
            "EDIT: fixed, see comment below."
        ),
    )
    score_unsolved = score(extract_signals(unsolved))
    score_solved = score(extract_signals(solved))
    assert score_solved < score_unsolved


def test_irrelevant_post_scores_zero():
    c = _candidate(title="Best pizza toppings?", body="Pineapple, discuss.")
    assert score(extract_signals(c)) == 0
    assert gate(0) == "ignore"
