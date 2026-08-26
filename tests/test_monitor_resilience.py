import pytest

from saipet.bridge import Bridge, Result
from saipet.monitor import MAX_BACKOFF_MULTIPLIER, run_monitor
from saipet.notify import Notification, Notifier
from saipet.sources.base import Candidate, FixtureSource

NOW = 1_800_000_000.0
_RELEVANT_BODY = "Lost context, session state, handoff, resume, checkpoint, deterministic protocol."


class _Collector(Notifier):
    def __init__(self):
        self.sent: list[Notification] = []

    def kinds(self):
        return [n.kind for n in self.sent]

    def send(self, notification):
        self.sent.append(notification)


@pytest.fixture(autouse=True)
def _notify_everything(monkeypatch):
    monkeypatch.setattr("saipet.config.NOTIFY_MIN_SCORE", 0)
    monkeypatch.setattr("saipet.policy.SUBREDDIT_ALLOWLIST", {"testsub"})
    monkeypatch.setattr("saipet.config.SUBREDDIT_ALLOWLIST", {"testsub"})


def _candidate(candidate_id):
    return Candidate(
        source="fixture",
        id=candidate_id,
        title="agent keeps losing context",
        body=_RELEVANT_BODY,
        permalink=f"https://reddit.com/r/testsub/{candidate_id}",
        subreddit="testsub",
        created_utc=NOW - 3600,
    )


@pytest.fixture
def bridge(tmp_path):
    return Bridge(
        report_dir=tmp_path / "runs",
        seen_path=tmp_path / "seen.json",
        source_factory=lambda *_: FixtureSource([]),
        now_fn=lambda: NOW,
    )


def _fail_on(bridge, failing_cycles: set[int]):
    """Make `scout` fail on the given 1-based cycle numbers."""
    original = bridge.dispatch
    calls = {"scout": 0}

    def _dispatch(verb, **args):
        if verb == "scout":
            calls["scout"] += 1
            if calls["scout"] in failing_cycles:
                return Result(ok=False, verb="scout", error="ConnectionError: reddit unreachable")
        return original(verb, **args)

    bridge.dispatch = _dispatch
    return calls


def test_a_failed_cycle_does_not_end_the_run(bridge):
    calls = _fail_on(bridge, {1})
    sink = _Collector()

    state = run_monitor(
        bridge,
        notifier=sink,
        cycles=3,
        heartbeat_every=1,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )

    assert state.cycles == 3
    assert calls["scout"] == 3
    assert state.errors == 1
    assert state.last_error == "ConnectionError: reddit unreachable"
    assert sink.kinds() == ["error", "heartbeat", "heartbeat"]


def test_an_exception_outside_dispatch_is_caught_too(bridge):
    """`dispatch` swallows its own exceptions; anything else still raises,
    and only catching one of the two leaves the loop killable."""

    def _boom(_verb, **_args):
        raise OSError("disk on fire")

    bridge.dispatch = _boom
    sink = _Collector()

    state = run_monitor(
        bridge, notifier=sink, cycles=2, sleep_fn=lambda _s: None, now_fn=lambda: NOW
    )

    assert state.cycles == 2
    assert state.errors == 2
    assert state.last_error == "OSError: disk on fire"


def test_backoff_doubles_while_failing_and_is_capped(bridge):
    _fail_on(bridge, {1, 2, 3, 4, 5})
    sleeps: list[float] = []

    run_monitor(bridge, cycles=6, interval_seconds=100, sleep_fn=sleeps.append, now_fn=lambda: NOW)

    assert sleeps == [200, 400, 800, 800, 800]
    assert MAX_BACKOFF_MULTIPLIER == 8


def test_backoff_resets_after_a_good_cycle(bridge):
    _fail_on(bridge, {1, 2})
    sleeps: list[float] = []

    run_monitor(bridge, cycles=4, interval_seconds=100, sleep_fn=sleeps.append, now_fn=lambda: NOW)

    assert sleeps == [200, 400, 100]


def test_a_healthy_run_sleeps_exactly_the_interval(bridge):
    sleeps: list[float] = []

    run_monitor(bridge, cycles=3, interval_seconds=100, sleep_fn=sleeps.append, now_fn=lambda: NOW)

    assert sleeps == [100, 100]


def test_a_heartbeat_proves_the_loop_is_alive_when_nothing_is_found(bridge):
    """'Alive and finding nothing' and 'died at 03:00' are identical silence
    otherwise, and only one of them is fine."""
    sink = _Collector()

    run_monitor(
        bridge,
        notifier=sink,
        cycles=2,
        heartbeat_every=1,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )

    assert sink.kinds() == ["heartbeat", "heartbeat"]
    assert "0 new candidate(s)" in sink.sent[0].text


def test_the_heartbeat_can_be_thinned_out(bridge):
    sink = _Collector()

    run_monitor(
        bridge,
        notifier=sink,
        cycles=4,
        heartbeat_every=2,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )

    assert sink.kinds() == ["heartbeat", "heartbeat"]
    assert [n.data["cycle"] for n in sink.sent] == [2, 4]


def test_the_heartbeat_can_be_switched_off(bridge):
    sink = _Collector()

    run_monitor(
        bridge,
        notifier=sink,
        cycles=3,
        heartbeat_every=0,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )

    assert sink.sent == []


def test_a_failing_cycle_reports_an_error_instead_of_a_heartbeat(bridge):
    _fail_on(bridge, {1})
    sink = _Collector()

    run_monitor(
        bridge,
        notifier=sink,
        cycles=1,
        heartbeat_every=1,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )

    assert sink.kinds() == ["error"]
    assert "cycle 1 failed" in sink.sent[0].text


def test_a_throwing_sink_cannot_kill_the_loop(tmp_path):
    class _Broken(Notifier):
        def send(self, notification):
            raise RuntimeError("sink is gone")

    live = Bridge(
        report_dir=tmp_path / "runs",
        seen_path=tmp_path / "seen.json",
        source_factory=lambda *_: FixtureSource([_candidate("a")]),
        now_fn=lambda: NOW,
    )

    state = run_monitor(
        live, notifier=_Broken(), cycles=2, sleep_fn=lambda _s: None, now_fn=lambda: NOW
    )

    assert state.cycles == 2
    # CORE-007: a failed delivery is not counted as a notification. The
    # failure is surfaced instead of being swallowed.
    assert state.notified_total == 0
    assert state.notify_failed_total == 1
    assert "sink is gone" in state.last_notify_error
