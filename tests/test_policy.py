import pytest

from saipet.cli import scout
from saipet.policy import is_subreddit_allowed
from saipet.sources.base import Candidate, FixtureSource


def _signal_fn(_candidate):
    return {"problem_match": 40, "audience_fit": 20, "workflow_fit": 15, "protocol_fit": 20}


def test_default_deny_unlisted_subreddit():
    assert not is_subreddit_allowed("some_random_sub")


def test_allowlisted_subreddit_passes(monkeypatch):
    monkeypatch.setattr("saipet.policy.SUBREDDIT_ALLOWLIST", {"allowed_sub"})
    assert is_subreddit_allowed("allowed_sub")
    assert not is_subreddit_allowed("other_sub")


def test_denied_subreddit_candidate_never_reaches_review_queue(monkeypatch):
    monkeypatch.setattr("saipet.policy.SUBREDDIT_ALLOWLIST", {"allowed_sub"})
    source = FixtureSource(
        [
            Candidate(source="fixture", id="1", title="x", body="", permalink="p1", subreddit="denied_sub"),
            Candidate(source="fixture", id="2", title="x", body="", permalink="p2", subreddit="allowed_sub"),
        ]
    )
    result = scout(source, signal_fn=_signal_fn)
    assert [i.candidate.id for i in result.pending()] == ["2"]
