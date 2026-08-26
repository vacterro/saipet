import pytest

from saipet.bridge import VERBS, Bridge
from saipet.sources.base import Candidate, FixtureSource

NOW = 1_800_000_000.0
_RELEVANT_BODY = "Lost context, session state, handoff, resume, checkpoint, deterministic protocol."


def _candidate(candidate_id="t1"):
    return Candidate(
        source="fixture",
        id=candidate_id,
        title="agent keeps losing context between sessions",
        body=_RELEVANT_BODY,
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
        source_factory=lambda *_: FixtureSource([_candidate()]),
        now_fn=lambda: NOW,
    )


def test_every_verb_dispatches(bridge):
    assert bridge.dispatch("scout").ok
    assert bridge.dispatch("queue").ok
    assert bridge.dispatch("status").ok
    assert bridge.dispatch("report").ok
    assert bridge.dispatch("approve", id="t1", solution="here is the fix").ok


def test_the_handler_table_and_the_declared_verb_set_agree(bridge):
    """Two lists of the same thing drift; this is the check that they cannot."""
    assert set(bridge._handlers()) == set(VERBS)


def test_an_unknown_verb_is_refused_and_nothing_runs(bridge):
    result = bridge.dispatch("scout_and_post")

    assert result.ok is False
    assert "unknown verb 'scout_and_post'" in result.error
    assert result.data == {}
    assert bridge.dispatch("queue").data["items"] == []  # no scout happened


@pytest.mark.parametrize("verb", ["_scout", "__init__", "dispatch", "eval", "system"])
def test_dunder_and_private_names_are_not_reachable_as_verbs(bridge, verb):
    """The dispatcher resolves through a fixed table, never getattr. A
    Reddit title is attacker-controlled text; dynamic resolution here would
    turn 'fetched some posts' into 'ran what a stranger wrote'."""
    assert bridge.dispatch(verb).ok is False


def test_scout_reports_what_it_queued_and_where(bridge):
    result = bridge.dispatch("scout")

    assert result.data["queued"] == 1
    assert result.data["subreddits"] == ["testsub"]
    assert result.data["report"]["jsonl"].endswith(".jsonl")


def test_queue_ranks_by_score_and_exposes_the_permalink(bridge):
    bridge.dispatch("scout")

    items = bridge.dispatch("queue").data["items"]

    assert [item["id"] for item in items] == ["t1"]
    assert items[0]["permalink"] == "https://reddit.com/r/testsub/t1"
    assert items[0]["band"] in {"review", "priority"}


def test_status_states_plainly_that_it_cannot_post(bridge):
    assert bridge.dispatch("status").data["can_post"] is False


def test_approve_returns_the_draft_and_posts_nothing(bridge):
    bridge.dispatch("scout")

    result = bridge.dispatch("approve", id="t1", solution="set a checkpoint after each step")

    assert result.data["posted"] is False
    assert "set a checkpoint after each step" in result.data["draft"]
    assert bridge.dispatch("queue").data["items"] == []  # approved leaves the pending list


def test_approve_refuses_to_invent_the_answer(bridge):
    bridge.dispatch("scout")

    result = bridge.dispatch("approve", id="t1", solution="   ")

    assert result.ok is False
    assert "solution" in result.error


def test_approve_on_an_unknown_id_fails_without_touching_the_queue(bridge):
    bridge.dispatch("scout")

    result = bridge.dispatch("approve", id="nope", solution="x")

    assert result.ok is False
    assert "no pending item" in result.error
    assert len(bridge.dispatch("queue").data["items"]) == 1


def test_bad_arguments_are_reported_not_raised(bridge):
    result = bridge.dispatch("scout", nonsense=True)

    assert result.ok is False
    assert "unknown argument 'nonsense'" in result.error


def test_report_lists_runs_on_disk_not_just_this_session(bridge, tmp_path):
    bridge.dispatch("scout")

    fresh = Bridge(report_dir=tmp_path / "runs", seen_path=tmp_path / "seen2.json")
    data = fresh.dispatch("report").data

    assert data["last"] == {}
    assert len(data["available"]) == 1


def test_two_bridges_do_not_share_a_queue(tmp_path, monkeypatch):
    monkeypatch.setattr("saipet.policy.SUBREDDIT_ALLOWLIST", {"testsub"})
    monkeypatch.setattr("saipet.config.SUBREDDIT_ALLOWLIST", {"testsub"})
    one = Bridge(
        report_dir=tmp_path / "a",
        seen_path=tmp_path / "a.json",
        source_factory=lambda *_: FixtureSource([_candidate()]),
        now_fn=lambda: NOW,
    )
    two = Bridge(report_dir=tmp_path / "b", seen_path=tmp_path / "b.json")

    one.dispatch("scout")

    assert two.dispatch("queue").data["items"] == []
