"""The two verbs a review GUI needs that the CLI never did.

`inspect` hands a human the body and signal breakdown behind one row of the
compact queue listing. `reject` lets a human dismiss noise without pretending
it was answered. Both are exact-id, local-state-only, and closed against the
same security model as every other verb.
"""

import pytest

from saipet.bridge import VERBS, Bridge
from saipet.sources.base import Candidate, FixtureSource

NOW = 1_800_000_000.0
_RELEVANT_BODY = "Lost context, session state, handoff, resume, checkpoint, deterministic protocol."


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    monkeypatch.setattr("saipet.policy.SUBREDDIT_ALLOWLIST", {"testsub"})
    monkeypatch.setattr("saipet.config.SUBREDDIT_ALLOWLIST", {"testsub"})
    return Bridge(
        report_dir=tmp_path / "runs",
        seen_path=tmp_path / "seen.json",
        source_factory=lambda *_: FixtureSource(
            [
                Candidate(
                    source="fixture",
                    id="t1",
                    title="agent keeps losing context between sessions",
                    body=_RELEVANT_BODY,
                    permalink="https://reddit.com/r/testsub/t1",
                    subreddit="testsub",
                    created_utc=NOW - 3600,
                )
            ]
        ),
        now_fn=lambda: NOW,
    )


def test_inspect_is_a_known_verb():
    assert "inspect" in VERBS
    assert "reject" in VERBS


def test_inspect_returns_the_full_sanitized_view(bridge):
    bridge.dispatch("scout")

    result = bridge.dispatch("inspect", id="t1")

    assert result.ok
    data = result.data
    assert data["id"] == "t1"
    assert data["source"] == "fixture"
    assert data["subreddit"] == "testsub"
    assert data["title"] == "agent keeps losing context between sessions"
    assert data["body"] == _RELEVANT_BODY
    assert data["permalink"] == "https://reddit.com/r/testsub/t1"
    assert data["created_utc"] == NOW - 3600
    assert data["score"] > 0
    assert data["band"] in {"review", "priority"}
    assert data["status"] == "pending"
    assert set(data["signals"]) == {"problem_match", "audience_fit", "workflow_fit", "protocol_fit", "already_solved"}


def test_inspect_is_exact_id_only(bridge):
    bridge.dispatch("scout")

    result = bridge.dispatch("inspect", id="t1extra")

    assert result.ok is False
    assert "no item with id 't1extra'" in result.error


def test_inspect_does_not_mutate_anything(bridge):
    bridge.dispatch("scout")
    before = bridge.dispatch("queue").data["items"]

    bridge.dispatch("inspect", id="t1")

    after = bridge.dispatch("queue").data["items"]
    assert after == before
    assert after[0]["status"] == "pending"


def test_reject_marks_a_pending_item_and_it_leaves_the_queue(bridge):
    bridge.dispatch("scout")

    result = bridge.dispatch("reject", id="t1")

    assert result.ok
    assert result.data == {"id": "t1", "status": "rejected"}
    assert bridge.dispatch("queue").data["items"] == []


def test_reject_refuses_an_unknown_id(bridge):
    bridge.dispatch("scout")

    result = bridge.dispatch("reject", id="nope")

    assert result.ok is False
    assert "no pending item" in result.error
    assert len(bridge.dispatch("queue").data["items"]) == 1


def test_reject_refuses_an_already_approved_item(bridge):
    bridge.dispatch("scout")
    bridge.dispatch("approve", id="t1", solution="checkpoint each step")

    result = bridge.dispatch("reject", id="t1")

    assert result.ok is False
    assert "no pending item" in result.error


def test_inspect_still_works_after_the_item_left_the_pending_list(bridge):
    """Inspect searches every item, not just pending ones, so a human can
    re-open a row they already approved or dismissed."""
    bridge.dispatch("scout")
    bridge.dispatch("approve", id="t1", solution="checkpoint each step")

    data = bridge.dispatch("inspect", id="t1").data

    assert data["status"] == "approved"
