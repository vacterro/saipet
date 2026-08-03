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

import time
from dataclasses import dataclass, field

from saipet import config
from saipet.bridge import Bridge
from saipet.notify import Notifier, NullNotifier, error, finding, heartbeat

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
    findings: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "cycles": self.cycles,
            "notified": self.notified,
            "queued": self.queued,
            "last_cycle_at": self.last_cycle_at,
            "last_error": self.last_error,
            "errors": self.errors,
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

    cycle = 0
    consecutive_failures = 0
    while cycles is None or cycle < cycles:
        cycle += 1
        tracker.cycles = cycle
        now = now_fn()
        tracker.last_cycle_at = now

        failure = _run_one_cycle(bridge, sink, tracker, args, min_score, now)
        if failure:
            consecutive_failures += 1
            tracker.errors += 1
            tracker.last_error = failure
            _send_quietly(sink, error(failure, cycle, now))
        else:
            consecutive_failures = 0
            if heartbeat_every and cycle % heartbeat_every == 0:
                _send_quietly(sink, heartbeat(cycle, tracker.queued, now))

        last_cycle = cycles is not None and cycle >= cycles
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
