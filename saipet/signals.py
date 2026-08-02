from saipet.config import (
    AUDIENCE_CUES,
    PROTOCOL_CUES,
    SOLVED_MARKERS,
    SYMPTOMS,
    WEIGHTS,
    WORKFLOW_CUES,
)
from saipet.sources.base import Candidate


def _cue_score(text: str, cues: list[str], weight: float) -> float:
    return weight if any(cue in text for cue in cues) else 0.0


def extract_signals(candidate: Candidate) -> dict:
    """Turn one candidate's text into the signal dict scorer.score() expects.

    Keyword-based on purpose (see config.py) -- cheap and explainable, and
    swappable for a real classifier later without scorer.py noticing.
    """
    text = f"{candidate.title}\n{candidate.body}".lower()

    hits = sum(1 for symptom in SYMPTOMS if symptom in text)
    # 3+ distinct symptom hits already earns the full problem_match weight;
    # this is the ticket's own bar for "clearly the right kind of problem."
    problem_match = min(WEIGHTS["problem_match"], hits * (WEIGHTS["problem_match"] / 3))

    solved = any(marker in text for marker in SOLVED_MARKERS)

    return {
        "problem_match": problem_match,
        "audience_fit": _cue_score(text, AUDIENCE_CUES, WEIGHTS["audience_fit"]),
        "workflow_fit": _cue_score(text, WORKFLOW_CUES, WEIGHTS["workflow_fit"]),
        "protocol_fit": _cue_score(text, PROTOCOL_CUES, WEIGHTS["protocol_fit"]),
        "already_solved": abs(WEIGHTS["already_solved"]) if solved else 0.0,
    }
