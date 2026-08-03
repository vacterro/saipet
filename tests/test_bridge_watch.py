import json

import pytest

from saipet.bridge import VERBS, Bridge, handle_line
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

    def _make(batches, notifier=None):
        remaining = list(batches)
        return Bridge(
            report_dir=tmp_path / "runs",
            seen_path=tmp_path / "seen.json",
            source_factory=lambda *_: FixtureSource(remaining.pop(0) if remaining else []),
            now_fn=lambda: NOW,
            notifier=notifier,
        )

    return _make


def test_watch_is_a_known_verb():
    assert "watch" in VERBS


def test_watch_runs_the_cycles_and_hands_back_what_it_found(make_bridge):
    bridge = make_bridge([[_candidate("a")], [_candidate("b")]])

    result = bridge.dispatch("watch", cycles=2)

    assert result.ok
    assert result.data["monitor"]["cycles"] == 2
    assert result.data["monitor"]["notified"] == 2
    assert [f["id"] for f in result.data["findings"]] == ["a", "b"]


def test_status_then_reports_the_monitor(make_bridge):
    bridge = make_bridge([[_candidate("a")]])

    assert bridge.dispatch("status").data["monitor"] is None

    bridge.dispatch("watch", cycles=1)
    monitor = bridge.dispatch("status").data["monitor"]

    assert monitor["cycles"] == 1
    assert monitor["notified"] == 1
    assert monitor["last_cycle_at"] == NOW
    assert monitor["errors"] == 0


def test_state_accumulates_across_separate_watch_calls(make_bridge):
    bridge = make_bridge([[_candidate("a")], [_candidate("b")]])

    bridge.dispatch("watch", cycles=1)
    bridge.dispatch("watch", cycles=1)

    assert bridge.dispatch("status").data["monitor"]["cycles"] == 2


@pytest.mark.parametrize("cycles", [0, -1, None, "forever", 1.5, True])
def test_an_unbounded_or_nonsense_cycle_count_is_refused(make_bridge, cycles):
    """A dispatch that never returns hangs its caller -- for the terminal
    engine, one typed line would eat the process. The forever-run is
    `python -m saipet.monitor`, which can actually be stopped."""
    result = make_bridge([[]]).dispatch("watch", cycles=cycles)

    assert result.ok is False
    assert "cycles must be an integer of at least 1" in result.error


def test_watch_feeds_the_bridges_own_notifier(make_bridge):
    sink = _Collector()
    bridge = make_bridge([[_candidate("a")]], notifier=sink)

    bridge.dispatch("watch", cycles=1)

    assert [n.kind for n in sink.sent] == ["finding"]
    assert sink.sent[0].data["id"] == "a"


def test_a_weak_finding_does_not_reach_the_notifier(make_bridge):
    weak = Candidate(
        source="fixture",
        id="weak",
        title="agent keeps losing context",
        body="lost context, handoff, resume, checkpoint, session state, deterministic protocol",
        permalink="https://reddit.com/r/testsub/weak",
        subreddit="testsub",
        created_utc=NOW - 3600,
    )
    bridge = make_bridge([[weak]])

    result = bridge.dispatch("watch", cycles=1)

    assert result.data["monitor"]["queued"] == 1
    assert result.data["monitor"]["notified"] == 0


def test_the_min_score_argument_wins_over_the_config(make_bridge):
    weak = Candidate(
        source="fixture",
        id="weak",
        title="agent keeps losing context",
        body="lost context, handoff, resume, checkpoint, session state, deterministic protocol",
        permalink="https://reddit.com/r/testsub/weak",
        subreddit="testsub",
        created_utc=NOW - 3600,
    )
    bridge = make_bridge([[weak]])

    result = bridge.dispatch("watch", cycles=1, min_score=0)

    assert result.data["monitor"]["notified"] == 1


def test_watch_is_reachable_from_the_terminal_engine(make_bridge):
    bridge = make_bridge([[_candidate("a")]])

    response = handle_line(bridge, "watch cycles=1")

    assert response["ok"] is True
    assert response["data"]["monitor"]["cycles"] == 1
    json.dumps(response)  # must stay one serialisable line


def test_status_still_states_it_cannot_post(make_bridge):
    bridge = make_bridge([[_candidate("a")]])
    bridge.dispatch("watch", cycles=1)
    assert bridge.dispatch("status").data["can_post"] is False
