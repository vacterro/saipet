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

# Cue words for the three secondary fit signals (T-002). Keyword-based on
# purpose: cheap, explainable, and replaceable by a real classifier later
# without changing scorer.py's contract.
AUDIENCE_CUES = ["ai agent", "llm", "gpt", "claude", "chatgpt", "copilot", "autonomous agent"]
WORKFLOW_CUES = ["workflow", "pipeline", "automation", "ci/cd", "script", "orchestrat"]
PROTOCOL_CUES = ["protocol", "framework", "spec", "standard", "architecture"]

# Phrases suggesting the thread already has a fix -- penalizes already_solved.
SOLVED_MARKERS = [
    "solved:",
    "resolved:",
    "already fixed",
    "found a solution",
    "found the fix",
    "closing this thread",
    "edit: fixed",
    "update: fixed",
]
