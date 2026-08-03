# Changelog

## v0.12.0 (2026-08-03)
- `--report-only`: fetch, score, gate and write the report, then stop. No prompts, so a scheduled or agent-driven run has nothing to block on.
- `main()` now takes an injectable `input_fn`, which is what makes the no-stdin promise testable rather than merely stated.

## v0.11.0 (2026-08-03)
- Persisted run reports (`saipet/report.py`): every run writes `runs/<UTC stamp>.jsonl` and a matching `.md` digest, so findings survive the process instead of dying with the review queue. `--report-dir` moves them.
- Each record carries the signal breakdown behind its score, not just the number, plus any subreddit that could not be fetched. A run that reached nine subreddits out of ten is not the same run as one that reached all ten.
- An empty run still writes both files: "ran and found nothing" and "never ran" are different facts, and a missing file cannot tell them apart.

## v0.10.0 (2026-08-03)
- Resilient fetch: subreddits now fail independently. A private, banned, misspelled or rate-limited one is retried (2 retries, doubling backoff) and then recorded, instead of aborting the whole run and losing every subreddit after it.
- Give-ups land in `RedditSource.last_failures` and the CLI prints a WARNING for each. A subreddit that answered nothing and one that could not be reached are different facts and no longer look identical.

## v0.9.0 (2026-08-03)
- Freshness window: `scout()` now drops threads older than `config.MAX_AGE_HOURS` (default 168 = one week) before scoring them. Old threads are already read and answered, and replying to one is how a scout starts looking like a bot. `created_utc` had been stored on every candidate and never used.
- `--since-hours H` overrides the window per run; `0` disables it. A candidate whose source gave no timestamp is kept -- unknown age is not evidence of age.
- `--since-hours` below zero and `--limit` below one are refused at parse time. A negative window inverts the freshness test, which would make a broken run look like a quiet one.

## v0.8.0 (2026-08-03)
- Runtime config file (`saipet/runtime_config.py`): an optional `saipet.config.json` overrides the symptom vocabulary, the subreddit allowlist, the signal weights and both gate thresholds without editing the installed package. No file means the shipped defaults, unchanged.
- Overrides are validated in full before any of them is applied, so one bad entry can never leave a half-applied configuration; unknown keys and unknown weight names are refused rather than silently ignored.
- `python -m saipet.cli --config PATH` selects a different file; a broken one prints one line and exits 2 instead of a traceback.

## v0.7.0 (2026-08-03)
- Live run path: `python -m saipet.cli` now builds a real `RedditSource` when `REDDIT_CLIENT_ID`/`REDDIT_CLIENT_SECRET` are in the environment, and falls back to the empty fixture otherwise. Until now `main()` always used the fixture, so no live scout run was possible at all.
- CLI flags: `--subreddit NAME` (repeatable, defaults to the configured allowlist) and `--limit N` (posts fetched per subreddit). `scout()` now honours the caller's limit instead of the source's default.
- An empty subreddit allowlist is reported at startup, since default-deny otherwise makes a correct run look like a broken one.

## v0.6.0 (2026-08-02)
- Per-subreddit policy gate (`saipet/policy.py`, `config.SUBREDDIT_ALLOWLIST`): default-deny, so a candidate from a subreddit nobody has explicitly allowed (after reading its rules) never reaches the review queue.

## v0.5.0 (2026-08-02)
- Fixture-recorded test coverage for `sources/reddit.py` (`tests/test_reddit_source.py`): query construction (every symptom OR-joined) and Candidate field mapping are now regression-tested without praw, network, or credentials.

## v0.4.0 (2026-08-02)
- No-link mode wired end-to-end: the interactive review step now asks whether to mention SAIPEN at all, so a relevant-but-not-appropriate-for-promo thread can still get a genuinely helpful, link-free reply.

## v0.3.0 (2026-08-02)
- Seen-thread persistence (`saipet/store.py`): every fetched candidate is checked/marked against a local JSON store, so a second `scout()` run never re-surfaces (or lets a human re-approve into) the same thread.

## v0.2.0 (2026-08-02)
- Real relevance signals (`saipet/signals.py`): title/body matched against the symptom vocabulary plus audience/workflow/protocol cue words, and an already-solved detector that penalizes threads that look already answered. Wired into `cli.main`, replacing the placeholder that scored everything 0.

## v0.1.0 (2026-08-02)
- Initial MVP: relevance scorer with three gate bands (ignore/review/priority), solve-first draft generator, human-approve review queue, source-agnostic `Source` interface with a read-only Reddit implementation (no OAuth creds wired in yet) and a local `FixtureSource` for dev/tests.
- Hard invariant, enforced by a static-scan test: no code path in `saipet/` calls a submit/comment/post/delete endpoint. Posting is always a manual human action outside this program.
