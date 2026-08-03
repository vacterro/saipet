# SAIPET

v0.7.0 -- see [CHANGELOG.md](CHANGELOG.md)

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
posts fetched per subreddit. Note that `config.SUBREDDIT_ALLOWLIST` is
empty by default and default-deny: until you add a subreddit there (after
reading its self-promo rules), every candidate is dropped before scoring.

## Test

```
pytest
```
