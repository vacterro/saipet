# Changelog

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
