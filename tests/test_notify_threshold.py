import pytest

from saipet import config
from saipet.bridge import Bridge
from saipet.monitor import is_notify_worthy, run_monitor
from saipet.notify import Notification, Notifier
from saipet.runtime_config import ConfigError, apply_overrides
from saipet.sources.base import Candidate, FixtureSource

NOW = 1_800_000_000.0

# Everything the signal extractor rewards: symptoms, an audience cue, a
# workflow cue and a protocol cue. Scores at the top of the range.
_STRONG_BODY = (
    "My AI agent keeps losing context between sessions. The handoff has no checkpoint, "
    "so resume is guesswork. Is there a deterministic protocol or workflow for this?"
)
# Enough to clear the queue gate, not enough to page anyone: symptoms plus
# one protocol cue, no audience or workflow signal.
_WEAK_BODY = "lost context, handoff, resume, checkpoint, session state, deterministic protocol"


class _Collector(Notifier):
    def __init__(self):
        self.sent: list[Notification] = []

    def send(self, notification):
        self.sent.append(notification)


@pytest.fixture(autouse=True)
def _restore_notify_bar():
    saved = config.NOTIFY_MIN_SCORE
    yield
    config.NOTIFY_MIN_SCORE = saved


def _candidate(candidate_id, body):
    return Candidate(
        source="fixture",
        id=candidate_id,
        title="agent keeps losing context",
        body=body,
        permalink=f"https://reddit.com/r/testsub/{candidate_id}",
        subreddit="testsub",
        created_utc=NOW - 3600,
    )


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    monkeypatch.setattr("saipet.policy.SUBREDDIT_ALLOWLIST", {"testsub"})
    monkeypatch.setattr("saipet.config.SUBREDDIT_ALLOWLIST", {"testsub"})
    return Bridge(
        report_dir=tmp_path / "runs",
        seen_path=tmp_path / "seen.json",
        source_factory=lambda *_: FixtureSource(
            [_candidate("strong", _STRONG_BODY), _candidate("weak", _WEAK_BODY)]
        ),
        now_fn=lambda: NOW,
    )


def test_the_default_bar_is_the_priority_gate_not_the_queue_gate():
    """The queue gate decides what a human may look at when they sit down;
    this decides what is worth interrupting them for."""
    assert config.NOTIFY_MIN_SCORE == config.GATE_PRIORITIZE_AT
    assert config.NOTIFY_MIN_SCORE > config.GATE_IGNORE_BELOW


@pytest.mark.parametrize(
    "score, bar, expected",
    [(85, 80, True), (80, 80, True), (79.9, 80, False), (0, 0, True)],
)
def test_the_bar_is_inclusive(score, bar, expected):
    assert is_notify_worthy({"score": score}, bar) is expected


def test_only_the_strong_finding_notifies(bridge):
    """Both items are queued -- both cleared the gate -- and only one is
    worth waking someone for."""
    sink = _Collector()

    state = run_monitor(
        bridge, notifier=sink, cycles=1, sleep_fn=lambda _s: None, now_fn=lambda: NOW
    )

    assert state.queued == 2
    assert state.notified == 1
    assert [n.data["id"] for n in sink.sent] == ["strong"]


def test_dropping_the_bar_lets_everything_through(bridge):
    sink = _Collector()

    state = run_monitor(
        bridge, notifier=sink, cycles=1, min_score=0, sleep_fn=lambda _s: None, now_fn=lambda: NOW
    )

    assert state.notified == 2


def test_raising_the_bar_silences_everything(bridge):
    sink = _Collector()

    state = run_monitor(
        bridge, notifier=sink, cycles=1, min_score=101, sleep_fn=lambda _s: None, now_fn=lambda: NOW
    )

    assert state.queued == 2
    assert state.notified == 0
    assert sink.sent == []


def test_the_bar_is_read_each_cycle_so_a_config_reload_is_honoured(tmp_path, monkeypatch):
    """Frozen at start-up, a running monitor would ignore the config change
    that was made precisely because it was too noisy."""
    monkeypatch.setattr("saipet.policy.SUBREDDIT_ALLOWLIST", {"testsub"})
    monkeypatch.setattr("saipet.config.SUBREDDIT_ALLOWLIST", {"testsub"})
    batches = [[_candidate("first", _WEAK_BODY)], [_candidate("second", _WEAK_BODY)]]
    live = Bridge(
        report_dir=tmp_path / "runs",
        seen_path=tmp_path / "seen.json",
        source_factory=lambda *_: FixtureSource(batches.pop(0) if batches else []),
        now_fn=lambda: NOW,
    )
    sink = _Collector()

    config.NOTIFY_MIN_SCORE = 101
    run_monitor(live, notifier=sink, cycles=1, sleep_fn=lambda _s: None, now_fn=lambda: NOW)
    assert sink.sent == []

    config.NOTIFY_MIN_SCORE = 0
    run_monitor(live, notifier=sink, cycles=1, sleep_fn=lambda _s: None, now_fn=lambda: NOW)
    assert [n.data["id"] for n in sink.sent] == ["second"]


def test_the_config_file_can_move_the_bar():
    apply_overrides({"notify_min_score": 55})
    assert config.NOTIFY_MIN_SCORE == 55


@pytest.mark.parametrize("bad", [-1, 101])
def test_an_impossible_bar_is_refused(bad):
    with pytest.raises(ConfigError, match="notify_min_score must be between 0 and 100"):
        apply_overrides({"notify_min_score": bad})
