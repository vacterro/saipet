# Board
## DOING
- [/] T-005 [P2] Fixture-recorded Reddit integration test: `sources/reddit.py`'s query construction and Candidate mapping are untested (needs live creds today). Record a canned praw-shaped response fixture and assert the query string / Candidate fields it produces, so a regression here is caught without network or credentials. | owner: claude-opus | claim_time: 2026-08-02T19:58:00Z | verify: test fails if the search query stops OR-joining `config.SYMPTOMS`, or if a field mapping (permalink/id/subreddit) is dropped

## TODO
- [ ] T-006 [P2] Per-subreddit policy gate: user's spec says always check the target sub's own rules (self-promo/bot rules vary a lot) before a draft is ever surfaced for approve. Minimal version: a local allow/deny list per subreddit (`config.py` or a small YAML) checked before a candidate is added to the review queue, defaulting to deny for unlisted subs. | needs: T-001 | verify: a candidate from a denied/unlisted subreddit never reaches ReviewQueue; one from an allowed sub does

## DONE
- [x] T-004 [P2] No-link mode wired end-to-end: `run_interactive` (injectable `input_fn`/`print_fn`) asks per-item whether to mention SAIPEN, passes the choice into `build_draft`. | review_passes: 1 | verify: PASS -- 18/18 tests. Shipped v0.4.0, local-only
- [x] T-003 [P1] Seen-thread persistence: `saipet/store.py`'s `SeenStore` (local JSON) wired into `cli.scout()`, checked/marked for every fetched candidate regardless of gate band. | review_passes: 1 | verify: PASS -- 16/16 tests, both ticket criteria covered directly (`tests/test_scout_dedup.py`). Shipped v0.3.0, local-only
- [x] T-002 [P0] Real signal extraction: `saipet/signals.py` maps title/body against symptom vocabulary + audience/workflow/protocol cue words + solved-marker detector, wired into `cli.main`. | review_passes: 1 | verify: PASS -- 13/13 tests, both ticket criteria covered directly (`tests/test_signals.py`). Shipped v0.2.0, local-only (no origin)
- [x] T-001 [P1] SAIPET MVP: scorer (3 gate bands), solve-first draft generator, human-approve review queue, source-agnostic `Source` interface + read-only Reddit implementation, `FixtureSource` for dev/tests. review_passes: 1 (found+fixed a P0: `ReviewQueue.approve/reject` indexed into the wrong list). | review_passes: 1 | verify: PASS -- 10/10 tests; static scan confirms no submit/comment/post/delete call anywhere in `saipet/`; symptom list has no "saipen" literal. Shipped v0.1.0, commit a627016 local-only (user deferred GitHub publish)

## BLOCKED
