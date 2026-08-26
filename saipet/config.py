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

# Freshness window (T-009). A week: old threads are read, answered, and
# dead, and replying to one is how a scout looks like a bot. A candidate
# with no timestamp at all (created_utc == 0.0, e.g. a fixture) is kept --
# unknown age is not evidence of age.
MAX_AGE_HOURS = 168

# The bar a finding must clear before the unattended monitor tells anyone
# (T-017). Deliberately higher than GATE_IGNORE_BELOW: the queue bar decides
# what a human may look at when they sit down, this one decides what is worth
# interrupting them for. A monitor that pages on every borderline thread
# teaches its owner to ignore it, which is the same as not running it.
NOTIFY_MIN_SCORE = GATE_PRIORITIZE_AT

# Report retention (T-025): report pairs older than this many days are pruned
# by the unattended monitor so a long-lived daemon does not accumulate ~192
# files per day forever.
REPORT_RETENTION_DAYS = 14

# Seen-state TTL (T-027): a candidate stays deduplicated for this long, then
# becomes reconsiderable. Prevents the seen store from growing without bound
# on a daemon meant to run for months.
SEEN_TTL_DAYS = 90

# Cue words for the three secondary fit signals (T-002). Keyword-based on
# purpose: cheap, explainable, and replaceable by a real classifier later
# without changing scorer.py's contract.
AUDIENCE_CUES = ["ai agent", "llm", "gpt", "claude", "chatgpt", "copilot", "autonomous agent"]
WORKFLOW_CUES = ["workflow", "pipeline", "automation", "ci/cd", "script", "orchestrat"]
PROTOCOL_CUES = ["protocol", "framework", "spec", "standard", "architecture"]

# Per-subreddit self-promo/bot-rule policy (T-006). Default-deny: a subreddit
# not listed here never reaches the review queue. Add a subreddit only after
# actually reading its rules, per the user's own spec.
SUBREDDIT_ALLOWLIST: set[str] = set()

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
