# Changelog

## v0.28.0 (2026-08-26)
- **Second audit wave: 22 tickets -- durability, cross-process visibility,
  error boundaries and hot-path scaling.**
  First publish to GitHub (`github.com/vacterro/saipet`).

### Core
- **CORE-001** (P0): terminal review transitions now fsync the cold journal
  record before the hot file removes the item from pending -- an OS/power
  crash can no longer make the hot snapshot durable while the only full
  terminal record (approved human draft included) is still volatile.
- **CORE-002** (P1): a legacy v0.24 `seen.json` (JSON array of `"source:id"`)
  is detected and migrated to the SQLite format on open, preserving the
  original bytes on any failure, instead of being rejected as "file is not a
  database".
- **CORE-003** (P1): outbox draining moved inside the per-cycle failure
  boundary -- a transient outbox read/write problem now counts as one failed
  cycle (backed off, reported, status-published) instead of terminating the
  unattended daemon.
- **CORE-004** (P1): the agent-facing terminal (`python -m saipet.bridge`)
  applies the persisted runtime config before building the default Bridge and
  reports a malformed config as a clear startup failure.
- **CORE-005** (P1): weight-sign validation anchored to an immutable shipped
  baseline -- a penalty saved as 0 can no longer later be converted into a
  reward by the same running process.
- **CORE-006** (P1): the GUI applies persisted runtime config before
  constructing any config-dependent store, so a configured `seen_ttl_days`
  actually reaches the dedup store for the whole session.
- **CORE-007** (P1): saving settings through the GUI no longer deletes the
  hidden `report_retention_days`/`seen_ttl_days` keys from the config file.
- **CORE-008** (P1): ReviewStore v2 metadata is validated strictly --
  malformed or contradictory `journal_checkpoint` / pending-vs-terminal state
  raises StateFileError instead of crashing GUI startup or creating an
  unresolvable item.
- **CORE-009** (P2): SeenStore claims carry a per-claim owner token; a stale
  claimant can no longer delete or prematurely finalize a successor's row.
- **CORE-010** (P2): all default review/outbox/delivery databases, journals,
  WAL/SHM and lock artifacts are now Git-ignored.

### Second wave
- **W2-001** (P1): the default terminal engine uses the canonical durable
  review store -- a finding survives a process restart and stays inspectable/
  approvable instead of being stranded by seen-state.
- **W2-002** (P1): ReviewStore gains an explicit `refresh()`; queue listing,
  item resolution and queue rehydration re-read durable state, so a
  long-running process sees findings another process committed.
- **W2-003** (P1): a torn trailing journal append (interrupted write) is
  recovered on read and truncated under the lock on the next write, instead
  of poisoning the whole review store; a malformed interior line is still
  hard corruption.
- **W2-004** (P2): `run_monitor` saves/restores the Bridge's monitor outbox
  context, so a later invocation cannot leak delivery ownership or duplicate
  notifications.
- **W2-005** (P2): `load_overrides` normalises invalid-UTF-8 and read errors
  into `ConfigError` -- CLI/monitor take their config-error exit path instead
  of propagating a raw traceback.
- **W2-006** (P2): durable `notifier_health` now tracks current delivery
  state and recovers after a successful send, independent of the lifetime
  failure counter.

### Performance
- **PERF-001** (P1): the review journal checkpoint is a durable byte offset,
  so startup and hot mutations read only the tail since the last checkpoint
  rather than the whole lifetime journal.
- **PERF-002** (P1): terminal identity/status lives in a point-addressable
  SQLite index with journal byte spans -- one inspect/approve of an old item
  is a point query plus a single journal seek, not a whole-history read.
- **PERF-003** (P1): DeliveryOutbox point operations query exactly one
  identity; the monitor drains only due obligations through an indexed range
  query, so a large future backlog no longer inflates a retry drain.
- **PERF-004** (P2): authentication-shaped Reddit failures are no longer
  retried within a fetch -- a 401/invalid token skips useless backoff sleeps
  while transient failures keep their bounded retries.
- **PERF-005** (P2): SeenStore prunes expired rows at most daily on write
  paths, keeping physical table cardinality near the TTL window for a
  long-lived daemon.
- **PERF-006** (P2): GUI startup constructs each durable store exactly once
  and passes the validated instances into the Bridge, halving persistence
  startup work on large state.

## v0.27.0 (2026-08-24)
- **Audit wave: 27 tickets from Core, Second Wave and Performance audits.**
  Same architecture, same screens, corrected substrate.

### Core
- **CORE-001** (P0): corrupt review-state.json no longer silently degrades to
  a non-durable session that marks things seen without ever making them
  durable. The GUI blocks review mutations (scout/watch/approve/reject) until
  the corrupt file is removed or fixed, and displays a persistent
  Cause/Effect/Fix error.
- **CORE-002** (P1): report preview now obeys the single-operation ownership
  model. A preview completion can no longer terminate another operation's
  busy state or permit overlapping Bridge dispatches.
- **CORE-003** (P1): all state mutations (seen, review approve/reject) follow
  a stage-persist-publish ordering. In-memory state is updated only after the
  durable write succeeds. A write failure leaves both runtime and disk exactly
  as before.
- **CORE-004** (P1): scout response counts and report output use the run
  delta, never the rebuilt durable backlog. Historical pending items are no
  longer re-reported as new findings every cycle.
- **CORE-005** (P1): only candidates admitted to the review queue are marked
  seen. Gate-dropped candidates (freshness, allowlist, score) stay
  reconsiderable, so retuning thresholds can discover them.
- **CORE-006** (P1): report filenames are collision-safe: two runs in the
  same second get distinct stems (`<stamp>-2`, `<stamp>-3`, ...) instead of
  silently overwriting one another.
- **CORE-007** (P1): notification delivery has an explicit success/failure
  contract. Failed sends are not counted as notified; failures are surfaced
  in the cycle result and lifetime counters.
- **CORE-008** (P1): the daemon startup guard validates that explicit
  `--subreddit` targets are also in the policy allowlist. A real-source
  daemon cannot run forever structurally unable to admit any content.
- **CORE-009** (P1): every bridge verb validates its arguments against a
  per-verb schema before the handler runs. `limit=-5`, `subreddits="abc"`,
  `since_hours=NaN` and `cycles=True` are now rejected with a stable error.
- **CORE-010** (P1): `read_json` normalises all read/decoding failures
  (JSONDecodeError, UnicodeDecodeError, OSError) into `StateFileError`.
  ReviewStore validates structure before iteration; malformed items produce
  `StateFileError` rather than raw TypeError.
- **CORE-011** (P1): `watch` returns only the findings produced by the
  current call, not the cumulative lifetime history. The `last_run_findings`
  field separates per-cycle from cumulative totals.

### Second Wave
- **W2-001** (P0): non-ReviewStore callers (CLI, terminal, default Bridge)
  now write the run report as their durable pre-seen record. A report-write
  failure leaves the candidate reconsiderable on the next run.
- **W2-002** (P0): every state file write (SeenStore, ReviewStore, runtime
  config) runs under an inter-process file lock (`InterProcessLock`) and
  reloads the durable file before merging. Two writers cannot silently lose
  each other's commits.
- **W2-003** (P1): `shutdown()` joins the in-flight worker with a bounded
  timeout, so its local side effects (seen, review, report, config) complete
  atomically rather than being cut at an arbitrary point.
- **W2-004** (P1): `select_finding` refuses while a mutating worker owns the
  bridge, preventing overlapping Bridge dispatches. Approve/reject completion
  clears the selection only when the user has not moved to a different finding
  in the meantime.
- **W2-005** (P1): untouched Scout/Monitor fields send `None` (defer to
  current config) so a settings save applies without restart. Edited fields
  are tracked and re-synced only when the user has not typed into them.
- **W2-006** (P1): `scout` deduplicates by `(source,id)` within the same
  batch, and explicit `--subreddit` targets are deduplicated before fetching.
  A source returning the same candidate twice yields exactly one queue item,
  one report record and one notification.
- **W2-007** (P1): every monitor numeric argument is validated for domain AND
  finiteness before startup. `NaN`, `Infinity`, negative `--since-hours` and
  out-of-range `--min-score` are all rejected.
- **W2-008** (P1): the daemon writes an atomic durable status record
  (`monitor-status.json`) after every cycle. A fresh Bridge can read it and
  answer "is the daemon alive" from disk, with staleness detection.
- **W2-009** (P1): `validate_overrides` canonicalises and validates scoring
  configuration at the boundary: symptoms are trimmed, must be non-empty,
  lowercase and unique after normalisation; weights must be finite. Persistent
  config is the canonical form.
- **W2-010** (P2): candidate identity is consistently `(source,id)` across
  SeenStore, ReviewStore, ReviewQueue and Bridge verbs. Cross-source id
  collisions are distinguished; ambiguous id-only lookups raise a clear error.
- **W2-011** (P1): both halves of a report pair are staged to temp files,
  fsynced, then renamed. Discovery lists only stems where both halves exist,
  so a crash mid-commit never advertises a partial run as complete.

### Performance
- **PERF-001** (P1): `SeenStore.mark_many` commits all new keys in one atomic
  write. A batch of already-seen keys performs no write at all.
- **PERF-002** (P1): `ReviewStore` maintains a `(source,id)` index for O(1)
  contains/find/upsert. `Bridge._merge_scouted_into_queue` replaces the full
  queue rebuild, avoiding a complete history walk per scout.
- **PERF-003** (P1): `MultiNotifier` diagnostics are bounded: a monotonic
  failure counter plus a bounded deque of the most recent failures replaces
  an unbounded lifetime string list.
- **PERF-004** (P2): the GUI worker-result poller backs off while the queue
  stays empty (15 → 30 → 60 → 100 ms), reducing main-loop wakeups during
  long operations.
- **PERF-005** (P2): `GoldenListBase` draws only the visible viewport rows
  (plus overscan). Keyboard navigation, focus and selection update only the
  affected rows, not the entire list.

## v0.26.0 (2026-08-24)
- **GUI hardening wave: real Tk runtime correctness.** Same screens, same
  bridge, fixed substrate.
- **Geometry:** `GoldenScrollbar` now requests only its intrinsic width/1px
  height, so it no longer inflates SunkenText/List/StatusRegion. Fixed-size
  widgets (buttons, entries) freeze their outer geometry via
  `grid_propagate(False)`, so a busy label text ("Scout running...") cannot
  push neighbours; the 1px press shift stays entirely internal. Settings
  lives in an in-view vertical scroller so its form is reachable at 640x480.
- **State ownership:** the Controller owns the full operation lifecycle
  (`Operation`: IDLE/RUNNING/SUCCEEDED/FAILED). A view renders busy only
  after the Controller accepts the operation, and every accepted operation
  reaches a terminal state -- no control can be left permanently disabled.
- **Races:** `inspect` is now a synchronous in-memory bridge read, so rapid
  A->B selection can never show a stale detail panel. Report preview is
  guarded by a monotonic generation counter; stale results are discarded.
- **Failure hygiene:** a failed approve/reject/dismiss preserves the typed
  solution, the selected finding and the detail panel; only success resets
  what should reset.
- **Config lifecycle:** the GUI loads and validates persisted overrides at
  startup (before screens are built), honours `config_path` everywhere, and
  surfaces invalid config as a persistent Cause/Effect/Fix error with no
  partial apply.
- **Durable review queue:** new `saipet/review_store.py` persists the full
  review workflow (`review-state.json`) -- candidate, score, band, signals,
  draft, status, discovered time. `scout` writes the durable queue BEFORE it
  marks anything seen, so a crash between the two can never lose a finding.
  `approve`/`reject` persist immediately. A corrupt review/seen file is
  detected and reported, never silently converted to empty.
- **One atomic JSON writer:** `saipet/jsonio.py` (`atomic_write_json` /
  `read_json`) is now the single helper for config, seen and review state.
- **Stale data is labelled:** failed refreshes keep the previous snapshot but
  say so ("Refresh failed: showing previous data.") instead of pretending it
  is fresh.
- **URL hardening:** Reddit permalinks are validated against an approved-host
  set via `parsed.hostname` (no substring netloc checks); lookalike and
  userinfo-swizzled hosts, non-https and malformed URLs are refused; browser
  launch failures become normal GUI errors.
- **Monitor simplified:** one explicit "Run one monitor cycle"; no
  multi-cycle loops, no fake daemon liveness.
- **Shutdown:** closing during any async operation is deterministic: the
  poller stops, late worker results are discarded, and no Tk-after-destroy
  errors occur.
- **Real GUI tests:** `tests/test_gui_integration.py` drives actual widgets
  and real geometry at 640x480 (no horizontal overflow, primary actions in
  bounds, scrollbar does not inflate, button press is geometry-invariant,
  cross-tab busy never sticks, rapid selection matches, failure preserves
  input, config survives restart, review state survives restart, close
  during every async operation).
- `cli.scout` gained a `persist` hook and now marks seen only after the
  durable queue write.

## v0.25.0 (2026-08-24)
- **Desktop GUI:** `python -m saipet.gui` — a local Win95-style review desk
  over the existing bridge. Golden Default palette, Verdana-only, 2px bevels,
  compact 640x480-compatible layout. Screens: Scout, Queue, Monitor, Reports,
  Settings. Read-only by construction: `Approve & build draft` produces text
  only, there is no post path, and the GUI never executes commands.
- Bridge gains two fixed verbs for the review screen: `inspect id=...`
  (full sanitized view of one candidate, exact-id, read-only) and
  `reject id=...` (mark a pending item dismissed, local state only). Both
  are closed-table verbs with tests.
- `runtime_config` gains `validate_overrides()` (pure validation, shared with
  `apply_overrides`) and `write_overrides()` (atomic temp-file + `os.replace`
  save). Settings can now be edited and saved without a half-written config.
- `ReviewQueue.find(id)` — exact-id lookup across all items, used by the new
  verbs.
- GUI threading: all bridge work runs on one gated worker; results are
  marshalled to the Tk loop through a thread-safe queue drained only while an
  operation is active — no periodic refresh, no background mutation.

## v0.24.0 (2026-08-03)
- **Fixed: the monitor starts silently when it has nothing it can actually reach.** Missing Reddit credentials without `--subreddit` flags or an empty subreddit allowlist without `--subreddit` flags now exits non-zero with a clear message instead of running forever and producing indistinguishable silence from a dead process.
- `--fixture` is the explicit opt-in for a credential-less run: passing it skips the startup checks so the monitor can still be used in tests and controlled environments without real credentials.
- A monitor that starts with `--fixture` and no subreddits prints a warning but still runs (testing the loop shape is a valid use case).

## v0.23.0 (2026-08-03)
- **Fixed: a newline in a Reddit title forges a second NOTIFY line on stdout.** Console and inbox sinks now sanitize untrusted text at the output boundary rather than trusting fetched content.
- Control characters (including tab, CR, LF) are stripped from console output; ANSI escape sequences are removed. Raw text is kept intact in the JSONL feed so an agent can still read the original.
- Markdown metacharacters (`*`, `` ` ``, `_`, `[`, `]`, etc.) are escaped in the markdown inbox so a title carrying formatting does not shift rendering.
- Permalinks are validated as https URLs before being rendered in the inbox; non-https or malformed URLs are dropped silently.

## v0.22.0 (2026-08-03)
- **Fixed: heartbeat printed cumulative counts instead of per-cycle counts.** `MonitorState` now carries separate `this_cycle` and `total` counters for both `queued` and `notified`. The heartbeat reports the number from the current cycle only, and a failed cycle cannot republish the last good cycle's numbers because the per-cycle fields are reset before the work starts.
- The ambiguous bare names `queued` and `notified` are removed from `as_dict()` rather than aliased: keeping an alias would preserve exactly the bug that made "cycle 2: 2 new candidate(s)" when each cycle found one.

## v0.21.0 (2026-08-03)
- **Fixed: a total Reddit outage counted as a healthy cycle.** A scout that reached none of its subreddits returned an empty list with `ok: True`, which is byte-identical to a quiet night -- so expired keys or a 403 across the board produced `errors: 0` and a cheerful heartbeat indefinitely.
- Sources now report `FetchHealth`: targets attempted versus failed, resolving to `healthy`, `degraded` or `failed`. The verdict is made where the target count is known rather than inferred downstream from a failure list whose emptiness also means "nothing was configured".
- The monitor acts on it: `failed` is a cycle failure with backoff and an error notification; `degraded` is a warning that does not slow the loop, because the subreddits that answered are still worth polling on schedule.
- Credentials that fail in `_client()` -- before any subreddit is touched -- count as every target failing, not zero targets attempted, which would have read as perfectly healthy.

## v0.20.0 (2026-08-03)
- `watch` verb on the bridge: an agent driving SAIPET over the API can now run the monitor itself for a bounded number of cycles and get the findings back, rather than only being able to fire single scouts.
- Bounded always -- there is no "forever" value. A dispatch that never returns hangs whoever called it, and for the command engine one typed line would eat the process. The unattended forever-run stays `python -m saipet.monitor`, which is something you can actually stop.
- `status` now reports the monitor: cycles run, findings reported, last cycle time, error count and last error. "Is the thing still alive" was previously unanswerable over the API.
- Fixed: cycle counts were per-call, so a second `watch` reset them to 1 and told a driving agent the monitor had just started.

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
