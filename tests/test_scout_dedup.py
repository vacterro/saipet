import pytest

from saipet.cli import scout
from saipet.sources.base import Candidate, FixtureSource
from saipet.store import SeenStore

_RELEVANT_BODY = "Lost context, session state, handoff, resume, checkpoint, deterministic protocol."


def _signal_fn(_candidate):
    return {"problem_match": 40, "audience_fit": 20, "workflow_fit": 15, "protocol_fit": 20}


@pytest.fixture(autouse=True)
def _allow_testsub(monkeypatch):
    monkeypatch.setattr("saipet.policy.SUBREDDIT_ALLOWLIST", {"testsub"})


def test_second_scout_run_yields_nothing_already_seen(tmp_path):
    seen = SeenStore(tmp_path / "seen.json")
    source = FixtureSource(
        [
            Candidate(
                source="fixture",
                id="t1",
                title="x",
                body=_RELEVANT_BODY,
                permalink="p1",
                subreddit="testsub",
            )
        ]
    )

    first = scout(source, signal_fn=_signal_fn, seen=seen)
    assert len(first.pending()) == 1

    second = scout(source, signal_fn=_signal_fn, seen=seen)
    assert len(second.pending()) == 0


def test_fresh_id_still_comes_through(tmp_path):
    seen = SeenStore(tmp_path / "seen.json")
    seen.mark("fixture", "t1")
    source = FixtureSource(
        [
            Candidate(
                source="fixture",
                id="t2",
                title="x",
                body=_RELEVANT_BODY,
                permalink="p2",
                subreddit="testsub",
            )
        ]
    )
    result = scout(source, signal_fn=_signal_fn, seen=seen)
    assert len(result.pending()) == 1
    assert result.pending()[0].candidate.id == "t2"
