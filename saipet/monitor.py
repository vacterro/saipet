"""The unattended half: scout on a schedule, tell someone what turned up.

Everything else in this package is a thing you run. This is the thing you
leave running. It drives the bridge rather than `cli.scout` directly, so
the monitor and an external agent go through exactly one code path -- a
background loop with its own private route into the scout would drift from
the one the agent sees, and only one of them would be tested.

Dedup needs no machinery here: `SeenStore` marks every fetched candidate,
so a later scout never re-surfaces one. The queue after a cycle IS that
cycle's new findings.

`cycles=None` runs forever, which is the point; any integer bounds it, so
a one-shot run and a test are the same code as the daemon.
"""

import argparse
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from saipet import config
from saipet.bridge import Bridge
from saipet.cli import DEFAULT_LIMIT, build_source
from saipet.jsonio import (
    InterProcessLock,
    atomic_write_json,
    pid_alive,
    read_json,
)
from saipet.notify import (
    DEFAULT_NOTIFICATION_FILE,
    ConsoleNotifier,
    Delivery,
    FileNotifier,
    MultiNotifier,
    Notifier,
    NullNotifier,
    degraded,
    error,
    finding,
    heartbeat,
)
from saipet.outbox import DeliveryOutbox
from saipet.report import DEFAULT_REPORT_DIR
from saipet.sources.base import DEGRADED, FAILED, FixtureSource
from saipet.runtime_config import (
    DEFAULT_CONFIG_PATH,
    ConfigError,
    apply_overrides,
    load_overrides,
)

DEFAULT_INTERVAL_SECONDS = 900  # 15 minutes: well under any sane rate limit
MAX_BACKOFF_MULTIPLIER = 8  # a broken API is not worth hammering every 15 min
MAX_KEPT_FINDINGS = 200  # bounded cumulative history for a long-lived daemon
# CORE-010: ONE canonical default durable-status path. The daemon writes it,
# and every status-reading Bridge (terminal, GUI, a reconnecting agent) reads
# it unless explicitly pointed elsewhere.
DEFAULT_STATUS_FILE = "monitor-status.json"
# T-028: one monitor per data directory. A second instance that would share
# seen.json / the notification feed / the inbox must refuse to start. The lock
# is keyed to the working directory by default; point it at the shared state
# via --lock-file when running from elsewhere.
DEFAULT_LOCK_FILE = "saipet-monitor.lock"
# How long a freshly claimed lock is protected before another starter may
# judge it stale. A LIVE holder is never stolen regardless of this value
# (pid_liveness wins); this only bounds the wait-to-refuse on conflict.
LOCK_STALE_SECONDS = 2.0


@dataclass
class MonitorState:
    """What the loop has done so far.

    Kept as an object the caller owns rather than counters inside the loop,
    so a driving agent can read progress while the loop is still running
    (T-020 puts this on the bridge's `status`).
    """

    cycles: int = 0
    # Per-cycle and cumulative are separate fields, not one ambiguous name.
    # The heartbeat used to print the running total under the words "N new
    # candidate(s)", so a second cycle finding one thing reported two.
    notified_this_cycle: int = 0
    notified_total: int = 0
    queued_this_cycle: int = 0
    queued_total: int = 0
    last_cycle_at: float | None = None
    cycle_completed_at: float | None = None
    # W2-002: the last cycle that actually succeeded, maintained
    # independently -- a later failure must not erase success history.
    last_success_at: float | None = None
    last_error: str = ""
    errors: int = 0
    health: str = "healthy"
    degraded_cycles: int = 0
    last_degradation: str = ""
    notify_failed_total: int = 0
    last_notify_error: str = ""
    # W2-006: current/recent notifier health, tracked INDEPENDENTLY of the
    # lifetime `notify_failed_total` counter. A historical failure must not
    # pin durable health to "degraded" forever; a later successful delivery
    # recovers it.
    notifier_health: str = "healthy"
    # CORE-003: a failed durable-status write is observable here instead of
    # being fatal to the loop; cleared on the next successful write.
    status_write_error: str = ""
    # T-031: a mid-run config reload whose file failed validation is observed
    # here instead of silently keeping the stale config.
    config_reload_error: str = ""
    # Cumulative but bounded history of delivered findings (PERF-003 keeps a
    # long-lived daemon's memory flat). Per-run findings are returned on the
    # state as `last_run_findings` so `watch` never reports stale history.
    findings: list = field(default_factory=list)
    last_run_findings: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "cycles": self.cycles,
            "notified_this_cycle": self.notified_this_cycle,
            "notified_total": self.notified_total,
            "queued_this_cycle": self.queued_this_cycle,
            "queued_total": self.queued_total,
        "last_cycle_at": self.last_cycle_at,
        "cycle_completed_at": self.cycle_completed_at,
        "last_success_at": self.last_success_at,
            "last_error": self.last_error,
            "errors": self.errors,
            "health": self.health,
            "degraded_cycles": self.degraded_cycles,
            "last_degradation": self.last_degradation,
        "notify_failed_total": self.notify_failed_total,
        "last_notify_error": self.last_notify_error,
        "notifier_health": self.notifier_health,
        "status_write_error": self.status_write_error,
        "config_reload_error": self.config_reload_error,
    }


def is_notify_worthy(item: dict, min_score: float) -> bool:
    """Does this finding clear the bar for interrupting a human?

    Separate from the queue gate on purpose. The gate decides what a person
    may look at when they sit down; this decides what is worth pulling them
    out of something else for, and the two are not the same number.
    """
    return float(item.get("score", 0.0)) >= min_score


def run_monitor(
    bridge: Bridge,
    notifier: Notifier | None = None,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    cycles: int | None = None,
    scout_args: dict | None = None,
    min_score: float | None = None,
    heartbeat_every: int = 0,
    sleep_fn=time.sleep,
    now_fn=time.time,
    state: MonitorState | None = None,
    status_path: str | None = None,
    outbox: DeliveryOutbox | None = None,
    config_path: str | Path | None = None,
    cancel_fn=None,
) -> MonitorState:
    """Scout every `interval_seconds`, notify on what is new, repeat.

    `min_score` is the notify bar; `None` reads `config.NOTIFY_MIN_SCORE` at
    each cycle, so a config reload mid-run is honoured rather than frozen
    at start-up.

    `heartbeat_every=N` sends proof of life every Nth cycle. It defaults to
    off here and on in the daemon entrypoint, which is the asymmetry that
    matters: a caller driving the loop itself already knows it is alive,
    while for something left running unattended "alive and finding nothing"
    and "died at 03:00" are identical silence, and only one is fine.

    **A failed cycle never ends the run.** Reddit goes down, a token
    expires, a disk fills; a monitor that exits on the first of those is a
    monitor you discover is dead a week later. Failures are reported,
    counted, and backed off exponentially to `MAX_BACKOFF_MULTIPLIER`.

    `status_path`, when given, is an atomic durable monitor status record
    written after every cycle (W2-008): a separate process can then answer
    "is the daemon alive" instead of guessing from this process's private
    memory. This run's own findings are recorded on `state.last_run_findings`
    so a caller (Bridge `watch`) can return only what THIS call produced.

    `outbox`, when given (CORE-004), makes failed notification deliveries
    retriable: each notify-worthy finding is recorded as an obligation
    before its first send attempt, every cycle starts by draining pending
    obligations through the same accounting, and an obligation is cleared
    only when delivery succeeds. A transient sink failure can no longer
    lose an alert permanently.

    Returns the state it accumulated, so a bounded run is directly
    assertable and an unbounded one leaves the same object behind for
    whoever is holding it.
    """
    sink = notifier if notifier is not None else NullNotifier()
    tracker = state if state is not None else MonitorState()
    args = dict(scout_args or {})
    # PERF-001: the per-call return collector exists ONLY for bounded runs,
    # which are the runs whose result someone reads (`Bridge.watch`). In
    # `cycles=None` daemon mode there is no reader to hand it to -- keeping
    # one leaked a full item-view dict per delivered finding for the life of
    # the process beside the already-bounded history.
    run_findings: list | None = [] if cycles is not None else None
    started_at = now_fn()

    # W2-002: wire the outbox and notify bar onto the bridge so the scout
    # persist phase can admit notify-worthy items BEFORE the final seen
    # commit (scout finalizes only after its persist callback returns).
    # W2-004: save any pre-existing monitor context and restore it on every
    # return/error/cancellation path, so a later invocation with a different
    # or no outbox cannot leak delivery ownership into a reusable Bridge.
    saved_outbox = getattr(bridge, "_monitor_outbox", None)
    saved_min = getattr(bridge, "_monitor_min_score", None)
    if outbox is not None:
        bridge._monitor_outbox = outbox
        bridge._monitor_min_score = float(config.NOTIFY_MIN_SCORE if min_score is None else min_score)

    ran = 0
    consecutive_failures = 0
    try:
        while cycles is None or ran < cycles:
            # PERF-006 cooperative stoppoint: checked BETWEEN cycles, never
            # mid-write, so a shutting-down owner can ask the loop to wind down
            # without cutting an atomic commit in half.
            if cancel_fn is not None and cancel_fn():
                break
            ran += 1
            # Cumulative, not per-call: `watch` on the bridge calls this
            # repeatedly with the same state, and a counter that restarted at 1
            # would tell a driving agent the monitor had just started every time.
            tracker.cycles += 1
            cycle = tracker.cycles
            now = now_fn()
            tracker.last_cycle_at = now
            # Reset before the work, not after: a failed cycle that kept the
            # previous cycle's numbers would report them again as its own.
            tracker.queued_this_cycle = 0
            tracker.notified_this_cycle = 0

            # T-031: re-read the override file each cycle so an edit to
            # saipet.config.json takes effect on the next cycle instead of only at
            # start-up. A missing file is "no overrides" (harmless); a file that
            # fails validation is observed and the previous config is kept -- reload
            # must never crash the daemon or half-apply.
            if config_path is not None:
                try:
                    applied = apply_overrides(load_overrides(config_path))
                    tracker.config_reload_error = ""
                    if applied:
                        bridge._monitor_min_score = float(
                            config.NOTIFY_MIN_SCORE if min_score is None else min_score
                        )
                except ConfigError as exc:
                    tracker.config_reload_error = str(exc)

            # CORE-004: undelivered obligations from earlier cycles (or earlier
            # runs) retry before new work; successes count here, not against the
            # current cycle's discovery numbers. (The drain itself now runs inside
            # _run_one_cycle, inside the per-cycle failure boundary -- CORE-003.)
            failure = _run_one_cycle(bridge, sink, tracker, args, min_score, now, run_findings, outbox)
            if failure:
                consecutive_failures += 1
                tracker.errors += 1
                tracker.last_error = failure
                tracker.health = FAILED
                _account_failure(tracker, _send_quietly(sink, error(failure, cycle, now)))
            else:
                consecutive_failures = 0
                # W2-002: success history is its own fact, never overwritten by
                # a later failure.
                tracker.last_success_at = now_fn()
                if tracker.health == DEGRADED:
                    # Said out loud, but no backoff: the targets that answered
                    # are still worth polling on schedule.
                    tracker.degraded_cycles += 1
                    _account_failure(tracker, _send_quietly(sink, degraded(tracker.last_degradation, cycle, now)))
                if heartbeat_every and cycle % heartbeat_every == 0:
                    # W2-004: monitor-level sends account like any other delivery.
                    _account_failure(tracker, _send_quietly(sink, heartbeat(cycle, tracker.queued_this_cycle, now)))

            tracker.cycle_completed_at = now_fn()

            if status_path is not None:
                # CORE-003: durable status is how OTHERS see this daemon; its
                # failure must not be what kills it. A full disk, a permission
                # glitch or a failed atomic replace is recorded on the state and
                # the loop keeps its normal failure/backoff policy -- and the
                # error rides the next successful write instead of being lost.
                try:
                    _write_status(status_path, tracker, started_at, interval_seconds, consecutive_failures, now_fn)
                    tracker.status_write_error = ""
                except Exception as exc:  # noqa: BLE001 -- status is observability, not liveness
                    tracker.status_write_error = f"{type(exc).__name__}: {exc}"

            last_cycle = cycles is not None and ran >= cycles
            if not last_cycle:
                # No sleep after the final bounded cycle: a one-shot run that
                # naps first would make `--cycles 1` feel broken.
                sleep_fn(interval_seconds * _backoff(consecutive_failures))

        tracker.last_run_findings = list(run_findings) if run_findings is not None else []
        if status_path is not None:
            # W2-002: graceful termination publishes a final stopped record, so
            # a bounded run never leaves a "running" marker behind its own corpse.
            try:
                _write_status(status_path, tracker, started_at, interval_seconds, consecutive_failures, now_fn, lifecycle="stopped")
                tracker.status_write_error = ""
            except Exception as exc:  # noqa: BLE001 -- same observability rule as above
                tracker.status_write_error = f"{type(exc).__name__}: {exc}"
        return tracker
    finally:
        # W2-004: always restore the Bridge's prior monitor context so a reusable
        # Bridge is never left owning an outbox from a finished run.
        bridge._monitor_outbox = saved_outbox
        bridge._monitor_min_score = saved_min


def _write_status(path, tracker, started_at, interval_seconds, consecutive_failures, now_fn, lifecycle="running") -> None:
    """Atomically persist the daemon's observable status (T-026 / W2-008).

    A separate process reads this file to answer "is the daemon alive" and
    "is it healthy" without sharing this process's memory. W2-002: the
    record carries an explicit `lifecycle` ("running"/"stopped") so a
    reader never mistakes a future `next_cycle_at` for proof of life.
    """
    next_cycle = None
    if lifecycle == "running" and tracker.cycle_completed_at is not None:
        next_cycle = tracker.cycle_completed_at + interval_seconds * (2 ** min(consecutive_failures, 3))
    data = {
        "pid": os.getpid(),
        "lifecycle": lifecycle,
        "started_at": started_at,
        "cycle_started_at": tracker.last_cycle_at,
        "cycle_completed_at": tracker.cycle_completed_at,
        "last_success_at": tracker.last_success_at,
        "next_cycle_at": next_cycle,
        "consecutive_failures": consecutive_failures,
        "cycles": tracker.cycles,
        "health": tracker.health,
        "last_error": tracker.last_error,
        "errors": tracker.errors,
        "queued_total": tracker.queued_total,
        "notified_total": tracker.notified_total,
        "notify_failed_total": tracker.notify_failed_total,
        "last_notify_error": tracker.last_notify_error,
        "status_write_error": tracker.status_write_error,
        "notifier_health": tracker.notifier_health,
    }
    atomic_write_json(path, data)


def read_monitor_status(path):
    """Read the durable daemon status record. Returns None when absent,
    raises StateFileError on corrupt content."""
    return read_json(path)


def _backoff(consecutive_failures: int) -> int:
    """1x while healthy, doubling per consecutive failure up to the cap."""
    if consecutive_failures <= 0:
        return 1
    return min(2**consecutive_failures, MAX_BACKOFF_MULTIPLIER)


def _send_quietly(sink: Notifier, notification) -> Delivery:
    """A sink that throws must not be able to kill the loop it reports on.
    Returns the Delivery so callers can count only actual deliveries. A
    legacy sink that returns None (no complaint) counts as delivered."""
    try:
        result = sink.send(notification)
    except Exception as exc:  # noqa: BLE001 -- delivery is best-effort, by design
        return Delivery(delivered=False, failures=(("sink", str(exc)),))
    if result is None:
        return Delivery(delivered=True)
    return result


def _account_failure(tracker: MonitorState, delivery: Delivery) -> None:
    """W2-004: one accounting path for EVERY send (finding, heartbeat,
    degraded, error) -- a failing sink is visible in the same counters no
    matter which kind of notification it dropped."""
    if delivery.delivered or not delivery.failures:
        return
    tracker.notify_failed_total += len(delivery.failures)
    tracker.last_notify_error = "; ".join(f"{n}: {r}" for n, r in delivery.failures)


def _sink_name(sink: Notifier) -> str:
    return type(sink).__name__


def _sink_failed(failed_names: set[str], sink_name: str) -> bool:
    """W2-003: a sink may report per-destination failures ("File.inbox");
    any sub-failure counts as that sink not having fully confirmed."""
    return any(
        name == sink_name or name.startswith(sink_name + ".") for name in failed_names
    )


def _update_outbox_after_send(outbox, sink, delivery: Delivery, item: dict) -> None:
    """Record per-sink outcome so a retry never duplicates a healthy sink.

    With a MultiNotifier, sinks absent from `delivery.failures` confirmed
    delivery; they are recorded as confirmed. An opaque notifier is treated
    atomically -- its failures carry the generic "sink" name and there is
    nothing finer-grained to remember.
    """
    source = item.get("source")
    candidate_id = item.get("id")
    failed_names = {name for name, _reason in delivery.failures}
    if isinstance(sink, MultiNotifier):
        confirmed = [
            _sink_name(s) for s in sink.sinks if not _sink_failed(failed_names, _sink_name(s))
        ]
        for name in confirmed:
            outbox.record_confirmed(source, candidate_id, [name])
    for name, reason in delivery.failures:
        outbox.record_failure(source, candidate_id, name, reason)


def _retry_targets(sink: Notifier, entry: dict) -> list[Notifier] | None:
    """The sinks an obligation still owes a delivery to. None means "the
    notifier as a whole" (atomic notifier, or nothing left to filter)."""
    if not isinstance(sink, MultiNotifier):
        return None
    confirmed = set(entry.get("confirmed", []))
    targets = [s for s in sink.sinks if _sink_name(s) not in confirmed]
    return targets or None


def _drain_outbox(outbox, sink: Notifier, tracker: MonitorState, now: float) -> None:
    """Retry every PENDING obligation whose backoff window has expired,
    through the normal accounting. Success clears the obligation; failure
    leaves it queued with an updated `next_attempt_at` (PERF-004).

    W2-008: each obligation carries its original `notification_at`. On retry
    we reconstruct the notification using THAT timestamp (not the current
    wall clock), so FileNotifier's identity key stays stable and a sink
    that already committed is never duplicated.
    """
    for entry in outbox.due_obligations(now):
        # PERF-003: only due obligations are returned by the range query, so
        # a large future backlog never inflates this drain.
        targets = _retry_targets(sink, entry)
        sender = MultiNotifier(*targets) if targets is not None else sink
        item = entry["item"]
        # W2-008: reuse the original notification creation time; fall back to
        # `now` only for legacy entries that predate this field.
        notification_at = entry.get("notification_at", now)
        delivery = _send_quietly(sender, finding(item, notification_at))
        if delivery.delivered:
            tracker.notified_total += 1
            tracker.findings.append(item)
            if len(tracker.findings) > MAX_KEPT_FINDINGS:
                tracker.findings = tracker.findings[-MAX_KEPT_FINDINGS:]
            outbox.remove(entry["source"], entry["id"])
            # W2-006: a successful retry recovers current notifier health.
            tracker.notifier_health = "healthy"
        else:
            _account_failure(tracker, delivery)
            # W2-006: a failed retry degrades current notifier health.
            tracker.notifier_health = "degraded"
            # Subset-wrapper failures carry the same class names as the
            # original sink's children, so they attribute directly.
            failed_names = {name for name, _reason in delivery.failures}
            for name, reason in delivery.failures:
                outbox.record_failure(entry["source"], entry["id"], name, reason)
            if isinstance(sender, MultiNotifier):
                # Only sinks actually attempted this round may confirm, and
                # only when none of their (possibly per-destination)
                # failures appear.
                attempted = {_sink_name(s) for s in targets} if targets is not None else {}
                confirmed = [
                    name
                    for name in (_sink_name(s) for s in getattr(sink, "sinks", []))
                    if name in attempted and not _sink_failed(failed_names, name)
                ]
                if confirmed:
                    outbox.record_confirmed(entry["source"], entry["id"], confirmed)


def _run_one_cycle(bridge, sink, tracker, args, min_score, now, run_findings, outbox=None) -> str:
    """One scout + notify pass. Returns "" on success, the failure otherwise.

    A failed cycle arrives two ways and both are caught here: `dispatch`
    already swallows exceptions and hands back `ok=False`, while anything
    outside it still raises. Handling only the second would leave a monitor
    running happily against a dead API, reporting nothing, looking healthy.
    """
    try:
        # CORE-003: drain undelivered obligations inside this cycle's failure
        # boundary (not before it), so a transient outbox read/write problem
        # counts as a failed cycle -- backed off and reported -- instead of
        # terminating the unattended monitor. A drain failure also means no
        # new discovery work starts that cycle.
        if outbox is not None:
            _drain_outbox(outbox, sink, tracker, now)

        scouted = bridge.dispatch("scout", **args)
        if not scouted.ok:
            return scouted.error

        # `ok` only says the dispatch itself worked. A scout that reached
        # none of its subreddits -- expired keys, 403, Reddit down -- comes
        # back ok with an empty list, which is byte-identical to a quiet
        # night. That is the bug this ticket exists for.
        reported = scouted.data.get("health", "healthy")
        failed = scouted.data.get("fetch", {}).get("failed", [])
        detail = "; ".join(f"{f['target']}: {f['error']}" for f in failed)
        if reported == FAILED:
            return f"every target failed -- {detail}" if detail else "every target failed"

        tracker.health = reported
        tracker.last_degradation = detail if reported == DEGRADED else ""

        # CORE-004/011: use the scout delta items, not the durable worklist,
        # so historical pending items are never re-reported as new findings.
        items = scouted.data.get("items", [])
        tracker.queued_this_cycle = len(items)
        tracker.queued_total += len(items)
        bar = config.NOTIFY_MIN_SCORE if min_score is None else min_score
        for item in items:
            if not is_notify_worthy(item, bar):
                continue
            # W2-004: a qualifying finding belongs to the structured cycle
            # result whether or not its notification got through -- delivery
            # outcome lives in the notification counters, never in discovery.
            if run_findings is not None:
                run_findings.append(item)
            tracker.findings.append(item)
            if len(tracker.findings) > MAX_KEPT_FINDINGS:
                tracker.findings = tracker.findings[-MAX_KEPT_FINDINGS:]
            # W2-002: the outbox admission already happened inside the
            # bridge's scout persist phase (before the final seen commit),
            # so we only handle the delivery and cleanup accounting here.
            delivery = _send_quietly(sink, finding(item, now))
            if delivery.delivered:
                # CORE-007: only actual deliveries count as notified.
                tracker.notified_this_cycle += 1
                tracker.notified_total += 1
                # W2-006: a successful delivery recovers current notifier health.
                tracker.notifier_health = "healthy"
                if outbox is not None:
                    outbox.remove(item["source"], item["id"])
            else:
                _account_failure(tracker, delivery)
                # W2-006: a failed delivery degrades current notifier health,
                # independent of the lifetime failure counter.
                tracker.notifier_health = "degraded"
                if outbox is not None:
                    _update_outbox_after_send(outbox, sink, delivery, item)

        # T-025: one findings-YYYY-MM-DD.jsonl per day instead of a report
        # pair per cycle, and prune report pairs past the retention window.
        # Both are best-effort tidy: a failure must never kill the loop.
        try:
            from saipet.report import append_daily_findings, prune_reports

            append_daily_findings(bridge.report_dir, items, now)
            prune_reports(bridge.report_dir, config.REPORT_RETENTION_DAYS, lambda: now)
        except Exception:  # noqa: BLE001 -- reporting/tidying must never kill the loop
            pass
        return ""
    except Exception as exc:  # noqa: BLE001 -- see the docstring above
        return f"{type(exc).__name__}: {exc}"


def _has_credentials() -> bool:
    return bool(os.environ.get("REDDIT_CLIENT_ID")) and bool(os.environ.get("REDDIT_CLIENT_SECRET"))


def _refuse_to_run_forever(args, subreddits: list[str], fixture: bool, print_fn=print) -> None:
    """Exit non-zero when the monitor has nothing it can actually reach.

    A daemon that starts silently with no credentials is a process that
    sleeps forever fetching nothing real, writing one heartbeat per
    interval, and cannot be distinguished from a dead one. It should not
    get that far.

    CORE-002: source mode is decided ONCE. Explicit `--subreddit` flags
    choose fetch targets for a LIVE run -- they never downgrade it to the
    credential-less fixture, and they never bypass the policy allowlist.
    A target the allowlist denies is a daemon that runs forever structurally
    unable to admit any content, so a real-source run refuses it up front.
    """
    has_creds = _has_credentials()
    explicit_subreddits = bool(args.subreddit)

    if fixture:
        return

    if not has_creds:
        msg = (
            "no Reddit credentials in the environment"
            + (" (explicit --subreddit targets do not change this)" if explicit_subreddits else "")
            + " -- the daemon would silently run against the empty fixture "
            "instead of Reddit. Set REDDIT_CLIENT_ID/SECRET or use --fixture "
            "to opt into a credential-less run."
        )
        print_fn(msg)
        raise SystemExit(1)

    if not subreddits:
        msg = (
            "the subreddit allowlist is empty and no --subreddit flags were "
            "given -- add subreddits to config or pass them on the command line"
        )
        print_fn(msg)
        raise SystemExit(1)

    if explicit_subreddits:
        denied = [s for s in subreddits if s not in config.SUBREDDIT_ALLOWLIST]
        if denied:
            msg = (
                "explicit --subreddit target(s) are denied by the policy "
                f"allowlist: {', '.join(denied)}. The daemon would run forever "
                "unable to admit them. Add them to the allowlist first or remove "
                "them from the command line."
            )
            print_fn(msg)
            raise SystemExit(1)


def _acquire_monitor_lock(lock_path: str | Path, print_fn=print) -> InterProcessLock:
    """Refuse to start if another live monitor already holds the singleton.

    T-028: two monitors sharing seen.json, one notification feed and one
    inbox corrupt every one of those files, with no coordination. We claim a
    per-data-directory lock: a lock held by a LIVE process is refused
    immediately; a lock left behind by a dead or crashed process (a PID that
    is no longer alive) is reclaimed. The atomic claim itself is delegated to
    InterProcessLock, which arbitrates the narrow race between the check and
    the claim.
    """
    lp = Path(lock_path)
    # InterProcessLock stores its claim at `.<name>.lock` (jsonio._lock_path_for);
    # read that actual file for the fast refusal, not the bare lock path.
    actual = lp.with_name(f".{lp.name}.lock")
    # Fast refusal: a lock whose recorded PID is still alive means a running
    # peer, not a leftover. No point waiting for it to die.
    if actual.exists():
        try:
            head = actual.read_text(encoding="utf-8", errors="replace").split(None, 1)
            if head and head[0].isdigit() and pid_alive(int(head[0])):
                print_fn(f"monitor already running (pid {head[0]})")
                raise SystemExit(1)
        except (OSError, ValueError):
            pass
    lock = InterProcessLock(lp, stale_seconds=LOCK_STALE_SECONDS)
    try:
        lock.__enter__()
    except TimeoutError:
        print_fn("monitor already running")
        raise SystemExit(1) from None
    return lock


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="saipet-monitor",
        description=(
            "Leave this running. It scouts on an interval, reports anything worth "
            "your attention, and never posts."
        ),
    )
    parser.add_argument("--subreddit", action="append", metavar="NAME", help="repeatable")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--since-hours", type=float, default=None)
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_SECONDS,
        metavar="SECONDS",
        help=f"seconds between cycles (default: {DEFAULT_INTERVAL_SECONDS})",
    )
    parser.add_argument(
        "--cycles",
        type=int,
        default=None,
        metavar="N",
        help="stop after N cycles (default: run until stopped)",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=None,
        metavar="S",
        help="report findings at or above S (default: config.NOTIFY_MIN_SCORE)",
    )
    parser.add_argument(
        "--notify-file",
        default=DEFAULT_NOTIFICATION_FILE,
        metavar="PATH",
        help=f"JSONL feed; a sibling inbox.md is written too (default: {DEFAULT_NOTIFICATION_FILE})",
    )
    parser.add_argument(
        "--heartbeat-every",
        type=int,
        default=1,
        metavar="N",
        help="proof of life every Nth cycle, 0 to disable (default: 1)",
    )
    parser.add_argument("--report-dir", default=DEFAULT_REPORT_DIR, metavar="DIR")
    parser.add_argument(
        "--status-file",
        default=DEFAULT_STATUS_FILE,
        metavar="PATH",
        help=f"atomic durable status record written after every cycle (default: {DEFAULT_STATUS_FILE})",
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, metavar="PATH")
    parser.add_argument(
        "--lock-file",
        default=DEFAULT_LOCK_FILE,
        metavar="PATH",
        help=f"single-instance lock; a second monitor holding it refuses to start (default: {DEFAULT_LOCK_FILE})",
    )
    parser.add_argument("--quiet", action="store_true", help="file sink only, nothing on stdout")
    parser.add_argument(
        "--fixture",
        action="store_true",
        help=(
            "explicitly opt into a credential-less run against the empty fixture; "
            "without this flag, missing credentials or an empty allowlist is a startup error"
        ),
    )

    args = parser.parse_args(argv)
    # W2-007: every numeric argument is validated for domain AND finiteness
    # before anything is built, written or run -- NaN defeats <= checks.
    if not math.isfinite(args.interval) or args.interval <= 0:
        parser.error("--interval must be a finite positive number")
    if args.cycles is not None and args.cycles < 1:
        parser.error("--cycles must be at least 1")
    if args.heartbeat_every < 0:
        parser.error("--heartbeat-every must not be negative (0 disables it)")
    if args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.since_hours is not None and (not math.isfinite(args.since_hours) or args.since_hours < 0):
        parser.error("--since-hours must be a finite number >= 0 (0 disables the window)")
    if args.min_score is not None and (
        not math.isfinite(args.min_score) or not 0 <= args.min_score <= 100
    ):
        parser.error("--min-score must be a finite number between 0 and 100")
    return args


def build_notifier(notify_file: str, quiet: bool, print_fn=print) -> Notifier:
    """File always, console unless silenced.

    Both, by default, because they answer different questions: the console
    is what a supervising agent reads live, the file is what anyone asks
    afterwards.
    """
    sinks: list[Notifier] = [FileNotifier(notify_file)]
    if not quiet:
        sinks.append(ConsoleNotifier(print_fn=print_fn))
    return MultiNotifier(*sinks)


def main(argv: list[str] | None = None, print_fn=print, **run_kwargs) -> MonitorState:
    """One command to leave running. Returns the state, for tests and callers."""
    args = parse_args(argv)

    try:
        applied = apply_overrides(load_overrides(args.config))
    except ConfigError as exc:
        print_fn(f"config error: {exc}")
        raise SystemExit(2) from exc
    if applied:
        print_fn(f"config: {args.config} overrides {', '.join(applied)}")

    subreddits = args.subreddit or sorted(config.SUBREDDIT_ALLOWLIST)
    fixture = args.fixture

    _refuse_to_run_forever(args, subreddits, fixture, print_fn=print_fn)

    # T-028: claim the singleton lock BEFORE any shared state is opened, so
    # two monitors can never both touch seen.json / the feed / the inbox.
    lock = _acquire_monitor_lock(args.lock_file, print_fn=print_fn)

    def resolved_source_factory(targets: list[str], symptoms: list[str]):
        # CORE-002: one source-mode decision for the whole run. `--fixture`
        # forces the empty fixture even when credentials exist; a live run
        # only reaches build_source after the guard confirmed credentials.
        if fixture:
            return FixtureSource([])
        return build_source(targets, symptoms)

    bridge = Bridge(report_dir=args.report_dir, source_factory=resolved_source_factory)
    bridge.skip_empty_reports = True
    notifier = build_notifier(args.notify_file, args.quiet, print_fn=print_fn)

    print_fn(f"source: {'fixture' if fixture else 'reddit'}")
    print_fn(
        f"monitor: every {args.interval:g}s, "
        f"subreddits {', '.join(subreddits) if subreddits else '(none)'}, "
        f"reporting at or above {config.NOTIFY_MIN_SCORE if args.min_score is None else args.min_score}"
    )
    if fixture and not subreddits:
        print_fn("WARNING: --fixture with no subreddits -- the monitor will find nothing")

    scout_args: dict = {"limit": args.limit}
    if subreddits:
        scout_args["subreddits"] = subreddits
    if args.since_hours is not None:
        scout_args["since_hours"] = args.since_hours

    # CORE-004: failed deliveries retry across restarts, so the outbox lives
    # next to the notification feed it serves.
    outbox = DeliveryOutbox(Path(args.notify_file).with_suffix(".outbox.json"))

    try:
        return run_monitor(
        bridge,
        notifier=notifier,
        interval_seconds=args.interval,
        cycles=args.cycles,
        scout_args=scout_args,
        min_score=args.min_score,
        heartbeat_every=args.heartbeat_every,
        status_path=args.status_file,
        outbox=outbox,
        config_path=args.config,
        **run_kwargs,
    )
    finally:
        # Release the singleton only after the daemon has fully stopped.
        lock.__exit__(None, None, None)


if __name__ == "__main__":
    main()
