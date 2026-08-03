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
import time
from dataclasses import dataclass, field

from saipet import config
from saipet.bridge import Bridge
from saipet.cli import DEFAULT_LIMIT, build_source
from saipet.notify import (
    DEFAULT_NOTIFICATION_FILE,
    ConsoleNotifier,
    FileNotifier,
    MultiNotifier,
    Notifier,
    NullNotifier,
    degraded,
    error,
    finding,
    heartbeat,
)
from saipet.report import DEFAULT_REPORT_DIR
from saipet.sources.base import DEGRADED, FAILED
from saipet.runtime_config import (
    DEFAULT_CONFIG_PATH,
    ConfigError,
    apply_overrides,
    load_overrides,
)

DEFAULT_INTERVAL_SECONDS = 900  # 15 minutes: well under any sane rate limit
MAX_BACKOFF_MULTIPLIER = 8  # a broken API is not worth hammering every 15 min


@dataclass
class MonitorState:
    """What the loop has done so far.

    Kept as an object the caller owns rather than counters inside the loop,
    so a driving agent can read progress while the loop is still running
    (T-020 puts this on the bridge's `status`).
    """

    cycles: int = 0
    notified: int = 0
    queued: int = 0
    last_cycle_at: float | None = None
    last_error: str = ""
    errors: int = 0
    health: str = "healthy"
    degraded_cycles: int = 0
    last_degradation: str = ""
    findings: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "cycles": self.cycles,
            "notified": self.notified,
            "queued": self.queued,
            "last_cycle_at": self.last_cycle_at,
            "last_error": self.last_error,
            "errors": self.errors,
            "health": self.health,
            "degraded_cycles": self.degraded_cycles,
            "last_degradation": self.last_degradation,
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

    Returns the state it accumulated, so a bounded run is directly
    assertable and an unbounded one leaves the same object behind for
    whoever is holding it.
    """
    sink = notifier if notifier is not None else NullNotifier()
    tracker = state if state is not None else MonitorState()
    args = dict(scout_args or {})

    ran = 0
    consecutive_failures = 0
    while cycles is None or ran < cycles:
        ran += 1
        # Cumulative, not per-call: `watch` on the bridge calls this
        # repeatedly with the same state, and a counter that restarted at 1
        # would tell a driving agent the monitor had just started every time.
        tracker.cycles += 1
        cycle = tracker.cycles
        now = now_fn()
        tracker.last_cycle_at = now

        failure = _run_one_cycle(bridge, sink, tracker, args, min_score, now)
        if failure:
            consecutive_failures += 1
            tracker.errors += 1
            tracker.last_error = failure
            tracker.health = FAILED
            _send_quietly(sink, error(failure, cycle, now))
        else:
            consecutive_failures = 0
            if tracker.health == DEGRADED:
                # Said out loud, but no backoff: the targets that answered
                # are still worth polling on schedule.
                tracker.degraded_cycles += 1
                _send_quietly(sink, degraded(tracker.last_degradation, cycle, now))
            if heartbeat_every and cycle % heartbeat_every == 0:
                _send_quietly(sink, heartbeat(cycle, tracker.queued, now))

        last_cycle = cycles is not None and ran >= cycles
        if not last_cycle:
            # No sleep after the final bounded cycle: a one-shot run that
            # naps first would make `--cycles 1` feel broken.
            sleep_fn(interval_seconds * _backoff(consecutive_failures))

    return tracker


def _backoff(consecutive_failures: int) -> int:
    """1x while healthy, doubling per consecutive failure up to the cap."""
    if consecutive_failures <= 0:
        return 1
    return min(2**consecutive_failures, MAX_BACKOFF_MULTIPLIER)


def _send_quietly(sink: Notifier, notification) -> None:
    """A sink that throws must not be able to kill the loop it reports on."""
    try:
        sink.send(notification)
    except Exception:  # noqa: BLE001 -- delivery is best-effort, by design
        return


def _run_one_cycle(bridge, sink, tracker, args, min_score, now) -> str:
    """One scout + notify pass. Returns "" on success, the failure otherwise.

    A failed cycle arrives two ways and both are caught here: `dispatch`
    already swallows exceptions and hands back `ok=False`, while anything
    outside it still raises. Handling only the second would leave a monitor
    running happily against a dead API, reporting nothing, looking healthy.
    """
    try:
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

        listed = bridge.dispatch("queue")
        if not listed.ok:
            return listed.error

        items = listed.data.get("items", [])
        tracker.queued += len(items)
        bar = config.NOTIFY_MIN_SCORE if min_score is None else min_score
        for item in items:
            if not is_notify_worthy(item, bar):
                continue
            _send_quietly(sink, finding(item, now))
            tracker.notified += 1
            tracker.findings.append(item)
        return ""
    except Exception as exc:  # noqa: BLE001 -- see the docstring above
        return f"{type(exc).__name__}: {exc}"


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
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, metavar="PATH")
    parser.add_argument("--quiet", action="store_true", help="file sink only, nothing on stdout")

    args = parser.parse_args(argv)
    if args.interval <= 0:
        parser.error("--interval must be positive (a monitor with no gap is a rate-limit ban)")
    if args.cycles is not None and args.cycles < 1:
        parser.error("--cycles must be at least 1")
    if args.heartbeat_every < 0:
        parser.error("--heartbeat-every must not be negative (0 disables it)")
    if args.limit < 1:
        parser.error("--limit must be at least 1")
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
    bridge = Bridge(report_dir=args.report_dir, source_factory=build_source)
    notifier = build_notifier(args.notify_file, args.quiet, print_fn=print_fn)

    print_fn(
        f"monitor: every {args.interval:g}s, "
        f"subreddits {', '.join(subreddits) if subreddits else '(none)'}, "
        f"reporting at or above {config.NOTIFY_MIN_SCORE if args.min_score is None else args.min_score}"
    )
    if not subreddits:
        print_fn(
            "WARNING: no subreddits to watch -- the allowlist is empty, so every "
            "cycle will find nothing. Add some to the config file first."
        )

    scout_args: dict = {"limit": args.limit}
    if subreddits:
        scout_args["subreddits"] = subreddits
    if args.since_hours is not None:
        scout_args["since_hours"] = args.since_hours

    return run_monitor(
        bridge,
        notifier=notifier,
        interval_seconds=args.interval,
        cycles=args.cycles,
        scout_args=scout_args,
        min_score=args.min_score,
        heartbeat_every=args.heartbeat_every,
        **run_kwargs,
    )


if __name__ == "__main__":
    main()
