# SAIPET

v0.12.0 -- see [CHANGELOG.md](CHANGELOG.md)

Read-only internet scout. Finds threads where people describe a real
problem (symptom search, not brand search), scores relevance, drafts a
solve-first reply, and stops for a human to approve before anything gets
posted. Nothing in this codebase calls a submit/comment/post endpoint --
posting, if it happens, is a manual action by the human outside this
program.

Reddit is the first source (`saipet/sources/reddit.py`, read-only OAuth
script app, 100 req/min free tier, no auto-post). The core (`scorer.py`,
`draft.py`, `review.py`) is source-agnostic so other platforms can plug in
behind the same `Source` interface later.

## Setup

```
pip install -r requirements.txt
```

Reddit live fetch needs a script-type app registered at
reddit.com/prefs/apps (your own account), then:

```
set REDDIT_CLIENT_ID=...
set REDDIT_CLIENT_SECRET=...
```

Without those, `saipet/cli.py` runs against an empty local fixture.

## Run

```
python -m saipet.cli
```

With credentials present this searches the configured allowlist; without
them it runs against the empty fixture and says so. Flags:

```
python -m saipet.cli --subreddit LocalLLaMA --subreddit AI_Agents --limit 50
```

`--subreddit` is repeatable and defaults to the allowlist; `--limit` caps
posts fetched per subreddit. Every run writes `runs/<UTC stamp>.jsonl`
plus a matching `.md` digest (`--report-dir` moves them).

Unattended runs use `--report-only`: fetch, score, gate, write the report,
stop. No prompts, nothing to block on.

## Tuning

Drop a `saipet.config.json` next to where you run (or point `--config` at
one) to retune without editing the package:

```json
{
  "subreddit_allowlist": ["LocalLLaMA", "AI_Agents"],
  "symptoms": ["lost context", "handoff", "resume"],
  "weights": { "problem_match": 45 },
  "gate_ignore_below": 60,
  "gate_prioritize_at": 80,
  "max_age_hours": 168
}
```

The allowlist is empty by default and default-deny: until a subreddit is
listed (after you have read its self-promo rules), every candidate from it
is dropped before scoring. Unknown keys are refused, not ignored.

## Test

```
pytest
```
