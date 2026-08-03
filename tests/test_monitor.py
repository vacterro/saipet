import pytest

from saipet.bridge import Bridge
from saipet.monitor import MonitorState, run_monitor
from saipet.notify import ConsoleNotifier, Notification, Notifier
from saipet.sources.base import Candidate, FixtureSource

NOW = 1_800_000_000.0
_RELEVANT_BODY = "Lost context, session state, handoff, resume, checkpoint, deterministic protocol."


class _Collector(Notifier):
    def __init__(self):
        self.sent: list[Notification] = []

    def send(self, notification):
        self.sent.append(notification)


def _candidate(candidate_id):
    return Candidate(
        source="fixture",
        id=candidate_id,
        title=f"agent keeps losing context {candidate_id}",
        body=_RELEVANT_BODY,
        permalink=f"https://reddit.com/r/testsub/{candidate_id}",
        subreddit="testsub",
        created_utc=NOW - 3600,
    )


@pytest.fixture
def make_bridge(tmp_path, monkeypatch):
    monkeypatch.setattr("saipet.policy.SUBREDDIT_ALLOWLIST", {"testsub"})
    monkeypatch.setattr("saipet.config.SUBREDDIT_ALLOWLIST", {"testsub"})

    def _make(batches):
        """`batches` is a list of candidate lists, one per fetch call."""
        remaining = list(batches)

        def _factory(*_args):
            return FixtureSource(remaining.pop(0) if remaining else [])

        return Bridge(
            report_dir=tmp_path / "runs",
            seen_path=tmp_path / "seen.json",
            source_factory=_factory,
            now_fn=lambda: NOW,
        )

    return _make


def test_three_bounded_cycles_notify_and_sleep(make_bridge):
    bridge = make_bridge([[_candidate("a")], [_candidate("b")], [_candidate("c")]])
    sink = _Collector()
    sleeps: list[float] = []

    state = run_monitor(
        bridge,
        notifier=sink,
        interval_seconds=900,
        cycles=3,
        sleep_fn=sleeps.append,
        now_fn=lambda: NOW,
    )

    assert state.cycles == 3
    assert state.notified == 3
    assert [n.data["id"] for n in sink.sent] == ["a", "b", "c"]
    assert sleeps == [900, 900]  # no nap after the final cycle


def test_a_single_cycle_does_not_sleep_at_all(make_bridge):
    """`--cycles 1` that naps first feels broken."""
    sleeps: list[float] = []

    run_monitor(
        make_bridge([[_candidate("a")]]),
        cycles=1,
        sleep_fn=sleeps.append,
        now_fn=lambda: NOW,
    )

    assert sleeps == []


def test_an_already_seen_thread_is_not_notified_twice(make_bridge):
    """The seen-gate is the dedup: the same candidate returned on cycle 2
    never reaches the queue, so the monitor needs no store of its own."""
    bridge = make_bridge([[_candidate("a")], [_candidate("a")]])
    sink = _Collector()

    state = run_monitor(
        bridge, notifier=sink, cycles=2, sleep_fn=lambda _s: None, now_fn=lambda: NOW
    )

    assert state.notified == 1
    assert len(sink.sent) == 1


def test_a_quiet_cycle_notifies_nothing(make_bridge):
    sink = _Collector()

    state = run_monitor(
        make_bridge([[]]), notifier=sink, cycles=1, sleep_fn=lambda _s: None, now_fn=lambda: NOW
    )

    assert state.cycles == 1
    assert state.notified == 0
    assert sink.sent == []


def test_every_finding_carries_the_permalink_a_human_needs(make_bridge):
    sink = _Collector()

    run_monitor(
        make_bridge([[_candidate("a")]]),
        notifier=sink,
        cycles=1,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )

    assert sink.sent[0].data["permalink"] == "https://reddit.com/r/testsub/a"
    assert sink.sent[0].kind == "finding"


def test_the_state_object_is_the_callers_and_gets_updated_in_place(make_bridge):
    """A driving agent holds this while the loop is still running."""
    state = MonitorState()

    returned = run_monitor(
        make_bridge([[_candidate("a")]]),
        cycles=1,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
        state=state,
    )

    assert returned is state
    assert state.last_cycle_at == NOW
    assert state.as_dict()["cycles"] == 1


def test_scout_args_reach_the_bridge(make_bridge):
    bridge = make_bridge([[_candidate("a")]])
    seen_args = {}
    original = bridge.dispatch

    def _record(verb, **args):
        if verb == "scout":
            seen_args.update(args)
        return original(verb, **args)

    bridge.dispatch = _record

    run_monitor(
        bridge,
        cycles=1,
        scout_args={"limit": 7, "subreddits": ["testsub"]},
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )

    assert seen_args == {"limit": 7, "subreddits": ["testsub"]}


def test_no_notifier_is_a_legal_run(make_bridge):
    state = run_monitor(
        make_bridge([[_candidate("a")]]), cycles=1, sleep_fn=lambda _s: None, now_fn=lambda: NOW
    )
    assert state.notified == 1


def test_the_console_sink_works_end_to_end(make_bridge):
    lines: list[str] = []

    run_monitor(
        make_bridge([[_candidate("a")]]),
        notifier=ConsoleNotifier(print_fn=lines.append),
        cycles=1,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )

    assert len(lines) == 1
    assert "agent keeps losing context a" in lines[0]
