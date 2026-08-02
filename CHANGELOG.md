# Changelog

## v0.2.0 (2026-08-02)
- Real relevance signals (`saipet/signals.py`): title/body matched against the symptom vocabulary plus audience/workflow/protocol cue words, and an already-solved detector that penalizes threads that look already answered. Wired into `cli.main`, replacing the placeholder that scored everything 0.

## v0.1.0 (2026-08-02)
- Initial MVP: relevance scorer with three gate bands (ignore/review/priority), solve-first draft generator, human-approve review queue, source-agnostic `Source` interface with a read-only Reddit implementation (no OAuth creds wired in yet) and a local `FixtureSource` for dev/tests.
- Hard invariant, enforced by a static-scan test: no code path in `saipet/` calls a submit/comment/post/delete endpoint. Posting is always a manual human action outside this program.
