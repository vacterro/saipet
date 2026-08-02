"""Tunables: symptom vocabulary, scoring weights, gate thresholds.

Deliberately no "SAIPEN"/brand terms in SYMPTOMS -- the scout hunts for
problems, not mentions of the project (RFC-style constraint from the
parent SAIPEN board's T-429 spec).
"""

SYMPTOMS = [
    "continue",
    "lost context",
    "handoff",
    "session",
    "resume",
    "checkpoint",
    "deterministic",
    "state",
    "memory",
    "multi agent",
    "context window",
    "agent workflow",
]

# Weighted signals feeding the relevance score. Each is a float in [0, weight].
WEIGHTS = {
    "problem_match": 40,
    "audience_fit": 20,
    "workflow_fit": 15,
    "protocol_fit": 20,
    "already_solved": -25,  # negative signal, subtracted
}

GATE_IGNORE_BELOW = 60
GATE_PRIORITIZE_AT = 80
