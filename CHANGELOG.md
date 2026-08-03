# Changelog

## v0.19.0 (2026-08-03)
- `python -m saipet.monitor` -- one command to leave running. `--interval`, `--cycles`, `--min-score`, `--notify-file`, `--heartbeat-every`, `--quiet`, plus the scout's own flags. Reuses the existing config, credential and report plumbing rather than growing a second copy of it.
- Findings go to a file feed and the console by default, because they answer different questions: the console is what a supervising agent reads live, the file is what anyone asks afterwards.
- An empty subreddit allowlist is called out at start-up. A monitor that finds nothing because it is watching nothing looks exactly like a quiet night otherwise.
- `--interval 0` is refused: a monitor with no gap between cycles is a rate-limit ban, not a fast monitor.

## v0.18.0 (2026-08-03)
- A failed cycle no longer ends the monitor. Reddit goes down, a token expires, a disk fills -- a loop that exits on the first of those is one you discover is dead a week later. Failures are reported as notifications, counted on `MonitorState`, and backed off 2x per consecutive failure to a cap of 8x, resetting on the first good cycle.
- Both failure shapes are caught: `dispatch` swallows its own exceptions and returns `ok=False`, while anything outside it still raises. Catching only the second would leave a monitor running happily against a dead API, reporting nothing, looking healthy.
- `heartbeat_every=N` sends proof of life every Nth cycle -- off by default in the library, on in the daemon. A caller driving the loop knows it is alive; for an unattended run, "alive and finding nothing" and "died at 03:00" are the same silence.
- A notification sink that throws can no longer kill the loop that reports on it.

## v0.17.0 (2026-08-03)
- Notify bar (`config.NOTIFY_MIN_SCORE`, default = the priority gate, overridable as `notify_min_score`): the monitor only reports findings that clear it. The queue gate decides what a human may look at when they sit down; this decides what is worth interrupting them for, and a monitor that pages on every borderline thread teaches its owner to ignore it.
- The bar is read each cycle, not frozen at start-up, so editing the config because the monitor was too noisy actually quiets the monitor that is already running.
- `MonitorState.queued` tracks what was queued as well as what was reported, so "found things, none worth waking you" stays visible.

## v0.16.0 (2026-08-03)
- Monitor loop (`saipet/monitor.py`): `run_monitor()` scouts on an interval and notifies on whatever is new. Everything else here is something you run; this is the thing you leave running.
- It drives the bridge rather than reaching into `cli.scout`, so the background loop and an external agent go through exactly one code path instead of two that drift.
- `cycles=None` runs forever and any integer bounds it, which makes a one-shot run, a test and the daemon the same code. No sleep after the final bounded cycle.
- Deduplication needs nothing new: the seen-store already means a later scout never re-surfaces a thread, so the queue after a cycle is that cycle's findings.

## v0.15.0 (2026-08-03)
- Notification sinks (`saipet/notify.py`): `ConsoleNotifier` for whoever reads this process's output, `FileNotifier` appending both `notifications.jsonl` and a human-readable `inbox.md`, plus `Null` and `Multi`. Groundwork for the unattended monitor -- "print it and hope" is not a delivery mechanism for a process nobody is watching.
- The inbox appends and never rewrites. An inbox that overwrites itself is worse than none: it looks full while losing everything before the last write.
- No shell call and no HTTP in this module, deliberately. A notification body carries a Reddit title, so handing it to another program is the one delivery path this package must not have.
- `MultiNotifier` collects a failing sink's error rather than raising it. One dead sink must not cost the others their message, and nothing in delivery may stop the monitor.

## v0.14.0 (2026-08-03)
- Command engine (`python -m saipet.bridge`): a line-oriented loop over the T-013 dispatch. One line in (`scout subreddit=LocalLLaMA limit=50`), one JSON line out. A local agent, or SAIPEN, can now drive the scout over a pipe.
- Deliberately not a shell. An unrecognised line is refused and passed nowhere -- there is no fallback that tries to run it. This process holds text fetched off the public internet, and a Reddit title reaching an execution path is a stranger's code running on your machine.
- No prompt is printed: the caller is normally another program, and `saipet> ` in front of every JSON line is one more thing to strip.

## v0.13.0 (2026-08-03)
- Control-plane seam (`saipet/bridge/`): `Bridge.dispatch(verb, **args)` gives an external agent a stable way to drive SAIPET -- `scout`, `report`, `status`, `queue`, `approve` -- returning structured results instead of printed text. `cli.main()` remains a script; nothing has to scrape its stdout any more.
- The verb set is closed and resolved through a fixed table, never `getattr`. Everything this bridge acts on originates on the public internet, so dynamic resolution would turn "fetched some posts" into "ran what a stranger wrote". An unknown verb is refused with nothing executed.
- `approve` requires a human-written solution, returns the draft and `posted: False`. As everywhere else in this codebase, posting stays a manual human action.
- A second static scan now fails the build on any execution primitive anywhere in `saipet/`, alongside the existing no-write-endpoint scan.

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
