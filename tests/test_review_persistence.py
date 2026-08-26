"""Durable review state: the queue must survive restarts, approve/reject must
persist, and a crash between "persist queue" and "mark seen" must never
permanently lose a finding.
"""

import json

import pytest

from saipet.bridge import Bridge
from saipet.cli import scout
from saipet.jsonio import StateFileError
from saipet.review_store import ReviewStore
from saipet.sources.base import Candidate, FixtureSource
from saipet.store import SeenStore

NOW = 1_800_000_000.0
_RELEVANT_BODY = "Lost context, session state, handoff, resume, checkpoint, deterministic protocol."


@pytest.fixture(autouse=True)
def _allow_testsub(monkeypatch):
    monkeypatch.setattr("saipet.policy.SUBREDDIT_ALLOWLIST", {"testsub"})
    monkeypatch.setattr("saipet.config.SUBREDDIT_ALLOWLIST", {"testsub"})


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


def _bridge(tmp_path, candidates, review=True):
    return Bridge(
        report_dir=tmp_path / "runs",
        seen_path=tmp_path / "seen.json",
        review_path=(tmp_path / "review-state.json") if review else None,
        source_factory=lambda *_: FixtureSource(candidates),
        now_fn=lambda: NOW,
    )


def _empty(tmp_path):
    return Bridge(
        report_dir=tmp_path / "runs",
        seen_path=tmp_path / "seen.json",
        review_path=tmp_path / "review-state.json",
        source_factory=lambda *_: FixtureSource([]),
        now_fn=lambda: NOW,
    )


# -- GUI-018 / GUI-019: state survives restart -------------------------


def test_pending_item_survives_restart(tmp_path):
    first = _bridge(tmp_path, [_candidate()])
    first.dispatch("scout", subreddits=["testsub"], limit=25, since_hours=48)
    assert len(first.dispatch("queue").data["items"]) == 1

    second = _empty(tmp_path)
    items = second.dispatch("queue").data["items"]

    assert len(items) == 1
    assert items[0]["id"] == "t1"
    assert items[0]["status"] == "pending"


def test_approved_state_survives_restart(tmp_path):
    first = _bridge(tmp_path, [_candidate()])
    first.dispatch("scout", subreddits=["testsub"], limit=25, since_hours=48)
    first.dispatch("approve", id="t1", solution="set a checkpoint after each step")

    second = _empty(tmp_path)

    assert second.dispatch("queue").data["items"] == []
    assert second.dispatch("inspect", id="t1").data["status"] == "approved"
    # The generated draft is durable too, even though inspect stays compact.
    # PERF-003: a decided item lives in the cold journal -- read it as the
    # explicit-history consumer.
    restored = next(
        i
        for i in ReviewStore(tmp_path / "review-state.json").items()
        if i.identity() == ("fixture", "t1")
    )
    assert restored.draft.startswith("set a checkpoint after each step")


def test_rejected_state_survives_restart(tmp_path):
    first = _bridge(tmp_path, [_candidate()])
    first.dispatch("scout", subreddits=["testsub"], limit=25, since_hours=48)
    first.dispatch("reject", id="t1")

    second = _empty(tmp_path)

    assert second.dispatch("queue").data["items"] == []
    assert second.dispatch("inspect", id="t1").data["status"] == "rejected"


def test_restored_pending_is_not_re_queued_or_duplicated(tmp_path):
    first = _bridge(tmp_path, [_candidate()])
    first.dispatch("scout", subreddits=["testsub"], limit=25, since_hours=48)

    second = _empty(tmp_path)
    assert len(second.dispatch("queue").data["items"]) == 1

    # A second scout against the same (empty) feed must not duplicate t1,
    # and the restored pending item stays the only one.
    second.dispatch("scout", subreddits=["testsub"], limit=25, since_hours=48)
    assert len(second.dispatch("queue").data["items"]) == 1


def test_approve_persists_immediately_to_disk(tmp_path):
    first = _bridge(tmp_path, [_candidate()])
    first.dispatch("scout", subreddits=["testsub"], limit=25, since_hours=48)
    first.dispatch("approve", id="t1", solution="checkpoint each step")

    # PERF-003: the terminal decision lands in the cold journal; the hot
    # file keeps only pending work. Both are on disk immediately.
    history = json.loads(
        (tmp_path / "review-state.json.history.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    assert history["item"]["status"] == "approved"
    assert history["item"]["draft"].startswith("checkpoint each step")
    hot = json.loads((tmp_path / "review-state.json").read_text(encoding="utf-8"))
    assert hot["pending"] == []


def test_the_review_file_is_atomic_and_leaves_no_temp_files(tmp_path):
    first = _bridge(tmp_path, [_candidate()])
    first.dispatch("scout", subreddits=["testsub"], limit=25, since_hours=48)
    first.dispatch("reject", id="t1")

    leftovers = [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []
    json.loads((tmp_path / "review-state.json").read_text(encoding="utf-8"))


# -- GUI-020: corrupt state is detected safely -------------------------


def test_a_corrupt_review_file_raises_not_silently_empties(tmp_path):
    path = tmp_path / "review-state.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(StateFileError, match="corrupt"):
        ReviewStore(path)


def test_a_corrupt_review_file_blocks_bridge_construction(tmp_path):
    path = tmp_path / "review-state.json"
    path.write_text('{"items": "nonsense"}', encoding="utf-8")

    with pytest.raises(StateFileError):
        Bridge(
            report_dir=tmp_path / "runs",
            seen_path=tmp_path / "seen.json",
            review_path=path,
            source_factory=lambda *_: FixtureSource([]),
        )


def test_a_corrupt_seen_file_raises(tmp_path):
    path = tmp_path / "seen.json"
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(StateFileError):
        _bridge(tmp_path, [], review=False)


# -- ordering: persist BEFORE seen marks -------------------------------


def _signal_fn(_candidate):
    return {"problem_match": 40, "audience_fit": 20, "workflow_fit": 15, "protocol_fit": 20}


def test_persist_runs_before_any_seen_mark(tmp_path):
    """A crash between the durable write and the seen marks must leave the
    candidate in the review store, never seen-and-forgotten."""
    calls: list[str] = []

    def persist(_queue, _source):
        calls.append("persist")

    class CrashOnMark:
        def has(self, _s, _i):
            return False

        def mark_many(self, _pairs):
            nonlocal calls
            calls.append("mark")
            raise RuntimeError("simulated crash before seen mark")

    source = FixtureSource([_candidate()])
    with pytest.raises(RuntimeError):
        scout(source, signal_fn=_signal_fn, seen=CrashOnMark(), persist=persist)

    # The durable write happened before the seen mark was attempted -- that
    # is the ordering that keeps a crash from losing the finding.
    assert calls[0] == "persist"
    assert "mark" in calls
    assert calls.index("persist") < calls.index("mark")


def test_scout_without_persist_still_marks_seen_after_the_loop(tmp_path):
    seen_path = tmp_path / "seen.json"
    first = _bridge(tmp_path, [_candidate()], review=False)
    first.dispatch("scout", subreddits=["testsub"], limit=25, since_hours=48)

    assert SeenStore(seen_path).has("fixture", "t1")


# CORE-009: strict validation of the persisted review domain --------------------


def _valid_doc():
    return {
        "version": 1,
        "items": [
            {
                "candidate": {
                    "source": "fixture",
                    "id": "t1",
                    "title": "t",
                    "body": "b",
                    "permalink": "https://reddit.com/r/testsub/t1",
                    "subreddit": "testsub",
                    "created_utc": 1800000000.0,
                },
                "relevance_score": 72,
                "band": "review",
                "draft": "",
                "status": "pending",
                "signals": {"problem_match": 40.0},
                "discovered": 1800000000.0,
            }
        ],
    }


def test_a_valid_current_version_document_round_trips(tmp_path):
    import json as _json

    from saipet.review_store import ReviewStore

    path = tmp_path / "rs.json"
    path.write_text(_json.dumps(_valid_doc()), encoding="utf-8")
    store = ReviewStore(path)
    assert store.contains("fixture", "t1")
    assert len(store.pending()) == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(version=999),
        lambda d: d["items"][0].update(status="pnding"),
        lambda d: d["items"][0].update(band="urgent"),
        lambda d: d["items"][0].update(relevance_score=float("nan")),
        lambda d: d["items"][0].update(relevance_score="high"),
        lambda d: d["items"][0]["candidate"].update(source=""),
        lambda d: d["items"][0]["candidate"].update(id=None),
        lambda d: d["items"][0].update(signals={"s": float("inf")}),
        lambda d: d["items"].append(dict(d["items"][0])),
    ],
)
def test_invalid_domains_fail_explicitly_and_suppress_nothing(tmp_path, mutate):
    """CORE-009: version 999 and status 'pnding' used to LOAD fine -- such an
    item was neither actionable (not pending) nor rediscoverable
    (_ReviewAwareSeen contained it), silently hidden forever. Every invalid
    domain now raises StateFileError."""
    import json as _json

    from saipet.jsonio import StateFileError
    from saipet.review_store import ReviewStore

    doc = _valid_doc()
    mutate(doc)
    path = tmp_path / "rs.json"
    path.write_text(_json.dumps(doc, allow_nan=True), encoding="utf-8")

    with pytest.raises(StateFileError):
        ReviewStore(path)


# PERF-003: history-independent hot paths ---------------------------------------

from dataclasses import replace

from saipet.review import STATUS_APPROVED, ReviewItem


def _candidate_n(n):
    return Candidate(
        source="fixture",
        id=f"t{n}",
        title="agent keeps losing context between sessions",
        body="Lost context, session state, handoff, resume, checkpoint, deterministic protocol.",
        permalink=f"https://reddit.com/r/testsub/t{n}",
        subreddit="testsub",
        created_utc=NOW - 3600,
    )


def test_terminal_transitions_do_not_rewrite_lifetime_history(tmp_path):
    """PERF-003: a transition used to serialize/fsync EVERY historical item;
    cost grew with project age. Now the terminal outcome appends one O(1)
    journal line and the hot file holds pending work only."""
    from saipet.review_store import ReviewStore

    path = tmp_path / "review-state.json"
    bridge = _bridge(tmp_path, [], review=True)
    store = ReviewStore(path)
    items = [ReviewItem(candidate=_candidate_n(n), relevance_score=70,
                        band="review", draft="", signals={}, discovered=NOW)
             for n in range(50)]
    store.commit_scouted(items)

    hot_size_after_scout = (path).stat().st_size
    for n in range(48):
        item = replace(next(i for i in items if i.candidate.id == f"t{n}"),
                       status=STATUS_APPROVED)
        store.commit_transition(item)

    import json as _json

    hot = _json.loads(path.read_text(encoding="utf-8"))
    assert len(hot["pending"]) == 2  # t48, t49 stay pending
    # The hot file SHRANK when items were decided -- it never held history.
    assert path.stat().st_size < hot_size_after_scout
    lines = (tmp_path / "review-state.json.history.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(lines) == 48

    # Full history remains retrievable through the explicit consumer.
    everything = {i.identity(): i.status for i in ReviewStore(path).items()}
    assert sum(s == STATUS_APPROVED for s in everything.values()) == 48


def test_bridge_startup_hydrates_pending_only(tmp_path):
    """PERF-003: Bridge startup used to materialize the whole terminal
    history into a second in-memory queue. The live worklist is pending
    work, nothing else."""
    first = _bridge(tmp_path, [_candidate(), _candidate("t2")], review=True)
    first.dispatch("scout", subreddits=["testsub"], limit=25, since_hours=48)
    first.dispatch("approve", id="t1", solution="done deal")

    second = _empty(tmp_path)
    queued = second.dispatch("queue").data["items"]
    assert [i["id"] for i in queued] == ["t2"]
    assert len(second.queue._items) == 1
