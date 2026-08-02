from saipet.config import GATE_IGNORE_BELOW, GATE_PRIORITIZE_AT, WEIGHTS


def score(signals: dict) -> float:
    """Combine weighted signals into one 0-100 relevance score.

    `signals` carries a float per key in config.WEIGHTS; missing keys count
    as 0. Each value is clamped to its own weight's range before summing, so
    one runaway signal can't blow past what the others allow.
    """
    total = 0.0
    for key, weight in WEIGHTS.items():
        value = signals.get(key, 0.0)
        if weight < 0:
            magnitude = max(0.0, min(-weight, value))
            total -= magnitude
        else:
            total += max(0.0, min(weight, value))
    return max(0.0, min(100.0, total))


def gate(relevance_score: float) -> str:
    """Map a score to the three-band decision: ignore / review / priority."""
    if relevance_score < GATE_IGNORE_BELOW:
        return "ignore"
    if relevance_score < GATE_PRIORITIZE_AT:
        return "review"
    return "priority"
