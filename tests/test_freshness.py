import pytest

from saipet.cli import is_fresh, scout
from saipet.sources.base import Candidate, FixtureSource

HOUR = 3600.0
NOW = 1_800_000_000.0


def _signal_fn(_candidate):
    return {"problem_match": 40, "audience_fit": 20, "workflow_fit": 15, "protocol_fit": 20}


def _candidate(candidate_id: str, created_utc: float) -> Candidate:
    return Candidate(
        source="fixture",
        id=candidate_id,
        title="x",
        body="lost context handoff resume",
        permalink=f"p/{candidate_id}",
        subreddit="testsub",
        created_utc=created_utc,
    )


@pytest.fixture(autouse=True)
def _allow_testsub(monkeypatch):
    monkeypatch.setattr("saipet.policy.SUBREDDIT_ALLOWLIST", {"testsub"})


def test_only_the_fresh_candidate_reaches_the_queue():
    source = FixtureSource(
        [
            _candidate("fresh", NOW - 2 * HOUR),
            _candidate("stale", NOW - 400 * HOUR),
        ]
    )

    queue = scout(source, signal_fn=_signal_fn, max_age_hours=168, now_fn=lambda: NOW)

    assert [item.candidate.id for item in queue.pending()] == ["fresh"]


def test_a_candidate_exactly_on_the_boundary_is_kept():
    assert is_fresh(_candidate("edge", NOW - 168 * HOUR), 168, NOW)
    assert not is_fresh(_candidate("past", NOW - 168 * HOUR - 1), 168, NOW)


def test_a_candidate_without_a_timestamp_is_kept():
    """Every FixtureSource candidate leaves created_utc at 0.0 -- unknown age
    is not evidence of age, and dropping those would empty the fixtures."""
    assert is_fresh(_candidate("no-timestamp", 0.0), 1, NOW)


def test_no_window_keeps_everything():
    source = FixtureSource([_candidate("ancient", NOW - 10_000 * HOUR)])

    queue = scout(source, signal_fn=_signal_fn, max_age_hours=None, now_fn=lambda: NOW)

    assert len(queue.pending()) == 1


def test_the_window_runs_before_scoring_not_after():
    """A stale candidate must not cost a signal computation."""
    seen_by_signal_fn = []

    def _recording_signal_fn(candidate):
        seen_by_signal_fn.append(candidate.id)
        return _signal_fn(candidate)

    source = FixtureSource([_candidate("stale", NOW - 400 * HOUR)])
    scout(source, signal_fn=_recording_signal_fn, max_age_hours=168, now_fn=lambda: NOW)

    assert seen_by_signal_fn == []
