import pytest

from saipet.bridge import Bridge, Result
from saipet.monitor import run_monitor
from saipet.notify import Notification, Notifier
from saipet.sources.base import Candidate, FixtureSource

NOW = 1_800_000_000.0
_STRONG_BODY = (
    "My AI agent keeps losing context between sessions. The handoff has no checkpoint, "
    "so resume is guesswork. Is there a deterministic protocol or workflow for this?"
)


class _Collector(Notifier):
    def __init__(self):
        self.sent: list[Notification] = []

    def heartbeats(self):
        return [n for n in self.sent if n.kind == "heartbeat"]

    def send(self, notification):
        self.sent.append(notification)


def _candidate(candidate_id):
    return Candidate(
        source="fixture",
        id=candidate_id,
        title="agent keeps losing context",
        body=_STRONG_BODY,
        permalink=f"https://reddit.com/r/testsub/{candidate_id}",
        subreddit="testsub",
        created_utc=NOW - 3600,
    )


@pytest.fixture
def make_bridge(tmp_path, monkeypatch):
    monkeypatch.setattr("saipet.policy.SUBREDDIT_ALLOWLIST", {"testsub"})
    monkeypatch.setattr("saipet.config.SUBREDDIT_ALLOWLIST", {"testsub"})

    def _make(batches):
        remaining = list(batches)
        return Bridge(
            report_dir=tmp_path / "runs",
            seen_path=tmp_path / "seen.json",
            source_factory=lambda *_: FixtureSource(remaining.pop(0) if remaining else []),
            now_fn=lambda: NOW,
        )

    return _make


def test_each_heartbeat_reports_its_own_cycle_not_the_running_total(make_bridge):
    """The reported defect: two cycles finding one candidate each printed
    'cycle 1: 1 new candidate(s)' then 'cycle 2: 2 new candidate(s)'."""
    sink = _Collector()

    run_monitor(
        make_bridge([[_candidate("a")], [_candidate("b")]]),
        notifier=sink,
        cycles=2,
        heartbeat_every=1,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )

    assert [n.data["queued"] for n in sink.heartbeats()] == [1, 1]
    assert [n.text for n in sink.heartbeats()] == [
        "cycle 1: 1 new candidate(s)",
        "cycle 2: 1 new candidate(s)",
    ]


def test_totals_still_accumulate(make_bridge):
    state = run_monitor(
        make_bridge([[_candidate("a")], [_candidate("b")]]),
        cycles=2,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )

    assert state.queued_total == 2
    assert state.notified_total == 2
    assert state.queued_this_cycle == 1
    assert state.notified_this_cycle == 1


def test_a_quiet_cycle_after_a_busy_one_reports_zero(make_bridge):
    sink = _Collector()

    state = run_monitor(
        make_bridge([[_candidate("a")], []]),
        notifier=sink,
        cycles=2,
        heartbeat_every=1,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )

    assert [n.data["queued"] for n in sink.heartbeats()] == [1, 0]
    assert state.queued_this_cycle == 0
    assert state.queued_total == 1


def test_a_failed_cycle_does_not_republish_the_previous_cycles_numbers(make_bridge):
    """Reset happens before the work, not after: a cycle that dies partway
    would otherwise report the last good cycle's counts as its own."""
    bridge = make_bridge([[_candidate("a")], []])
    original = bridge.dispatch
    calls = {"n": 0}

    def _dispatch(verb, **args):
        if verb == "scout":
            calls["n"] += 1
            if calls["n"] == 2:
                return Result(ok=False, verb="scout", error="ConnectionError: down")
        return original(verb, **args)

    bridge.dispatch = _dispatch

    state = run_monitor(
        bridge, cycles=2, heartbeat_every=1, sleep_fn=lambda _s: None, now_fn=lambda: NOW
    )

    assert state.queued_this_cycle == 0
    assert state.notified_this_cycle == 0
    assert state.queued_total == 1


def test_the_state_dict_names_both_scopes(make_bridge):
    state = run_monitor(
        make_bridge([[_candidate("a")]]), cycles=1, sleep_fn=lambda _s: None, now_fn=lambda: NOW
    )

    keys = state.as_dict()
    assert keys["queued_this_cycle"] == 1
    assert keys["queued_total"] == 1
    assert keys["notified_this_cycle"] == 1
    assert keys["notified_total"] == 1
    assert "queued" not in keys  # the ambiguous name is gone, not aliased
    assert "notified" not in keys
