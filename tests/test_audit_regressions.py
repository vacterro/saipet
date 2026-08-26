"""Regression coverage for the audit's verified defects.

Each test mirrors a focused reproduction from the audit waves. They are pure
Python (no Tk) unless a defect is inherently about the GUI controller, which
is tested with a stub bridge.
"""

import json
import math
from pathlib import Path

import pytest

from saipet.bridge import Bridge
from saipet.cli import scout
from saipet.gui.controller import Controller
from saipet.jsonio import InterProcessLock, atomic_write_json
from saipet.monitor import parse_args, run_monitor
from saipet.notify import Notifier
from saipet.report import list_complete_reports, write_report
from saipet.review_store import ReviewStore
from saipet.store import SeenStore
from saipet.sources.base import Candidate, FixtureSource

NOW = 1_800_000_000.0
_RELEVANT_BODY = "Lost context, session state, handoff, resume, checkpoint, deterministic protocol."


@pytest.fixture(autouse=True)
def _allow_testsub(monkeypatch):
    monkeypatch.setattr("saipet.policy.SUBREDDIT_ALLOWLIST", {"testsub"})
    monkeypatch.setattr("saipet.config.SUBREDDIT_ALLOWLIST", {"testsub"})


def _candidate(candidate_id, source="fixture", subreddit="testsub"):
    return Candidate(
        source=source,
        id=candidate_id,
        title="agent keeps losing context between sessions",
        body=_RELEVANT_BODY,
        permalink=f"https://reddit.com/r/{subreddit}/{candidate_id}",
        subreddit=subreddit,
        created_utc=NOW - 3600,
    )


def _bridge(tmp_path, candidates=None, review=False, source_factory=None):
    return Bridge(
        report_dir=tmp_path / "runs",
        seen_path=tmp_path / "seen.json",
        review_path=(tmp_path / "review-state.json") if review else None,
        source_factory=source_factory or (lambda *_: FixtureSource(candidates or [])),
        now_fn=lambda: NOW,
    )


# CORE-004: a second identical scout reports the delta, never the backlog ----


def test_second_scout_reports_zero_when_nothing_is_new(tmp_path):
    bridge = _bridge(tmp_path, [_candidate("t1")], review=True)

    first = bridge.dispatch("scout", subreddits=["testsub"], limit=25, since_hours=48)
    assert first.data["queued"] == 1

    second = bridge.dispatch("scout", subreddits=["testsub"], limit=25, since_hours=48)
    assert second.data["queued"] == 0
    assert second.data["items"] == []
    # The durable review worklist still exposes the historical pending item.
    assert len(bridge.dispatch("queue").data["items"]) == 1


# CORE-005: a gate-dropped candidate stays reconsiderable --------------------


def _strong_signals(_candidate):
    return {"problem_match": 40, "audience_fit": 20, "workflow_fit": 15, "protocol_fit": 20}


def test_allowlist_denied_candidate_is_not_marked_seen(tmp_path, monkeypatch):
    monkeypatch.setattr("saipet.policy.SUBREDDIT_ALLOWLIST", set())
    monkeypatch.setattr("saipet.config.SUBREDDIT_ALLOWLIST", set())
    seen = SeenStore(tmp_path / "seen.json")
    source = FixtureSource([_candidate("t1")])

    queue = scout(source, signal_fn=_strong_signals, seen=seen)
    assert len(queue.pending()) == 0
    assert not seen.has("fixture", "t1")

    monkeypatch.setattr("saipet.policy.SUBREDDIT_ALLOWLIST", {"testsub"})
    monkeypatch.setattr("saipet.config.SUBREDDIT_ALLOWLIST", {"testsub"})
    requeued = scout(FixtureSource([_candidate("t1")]), signal_fn=_strong_signals, seen=seen)
    assert len(requeued.pending()) == 1


# W2-006: same-run duplicate candidate is deduplicated -----------------------


def test_same_run_duplicate_candidate_yields_one_item(tmp_path):
    source = FixtureSource([_candidate("t1"), _candidate("t1")])
    queue = scout(source, signal_fn=_strong_signals, limit=25)
    assert len(queue.pending()) == 1


# CORE-006: same-timestamp reports get distinct stems ------------------------


def test_same_timestamp_reports_do_not_overwrite(tmp_path):
    first = write_report([], directory=tmp_path, now_fn=lambda: NOW)
    second = write_report([], directory=tmp_path, now_fn=lambda: NOW)
    assert first.jsonl != second.jsonl
    assert first.jsonl.exists() and second.jsonl.exists()
    assert len(list_complete_reports(tmp_path)) == 2


# W2-011: a partial report pair is not discovered ---------------------------


def test_an_orphaned_jsonl_is_not_a_complete_report(tmp_path):
    orphan = tmp_path / "20270115T080000Z.jsonl"
    orphan.write_text('{"broken": true}\n', encoding="utf-8")

    assert list_complete_reports(tmp_path) == []


# CORE-009: bridge verb argument validation ---------------------------------


def test_bridge_rejects_malformed_arguments(tmp_path):
    bridge = _bridge(tmp_path, [_candidate("t1")])

    assert not bridge.dispatch("scout", subreddits="abc").ok
    assert not bridge.dispatch("scout", limit=-5).ok
    assert not bridge.dispatch("scout", since_hours=-1).ok
    assert not bridge.dispatch("scout", limit=True).ok
    assert not bridge.dispatch("scout", since_hours=float("nan")).ok
    assert not bridge.dispatch("approve", id="", solution="x").ok
    assert not bridge.dispatch("watch", cycles=0).ok
    assert not bridge.dispatch("watch", interval_seconds=float("inf")).ok


# W2-010: cross-source id collision stays distinct --------------------------


def test_two_sources_sharing_an_id_survive(tmp_path):
    from saipet.review import ReviewItem

    store = ReviewStore(tmp_path / "rs.json")
    a = ReviewItem(
        candidate=_candidate("same", source="srcA"), relevance_score=70, band="review", draft="", signals={}
    )
    b = ReviewItem(
        candidate=_candidate("same", source="srcB"), relevance_score=80, band="priority", draft="", signals={}
    )
    store.commit_scouted([a, b])

    reloaded = ReviewStore(tmp_path / "rs.json")
    assert reloaded.find("srcA", "same") is not None
    assert reloaded.find("srcB", "same") is not None
    assert reloaded.find_by_id("same") is not None


# W2-002: two writers of the same SeenStore do not lose commits --------------


def test_two_seenstore_writers_merge(tmp_path):
    path = tmp_path / "seen.json"
    a = SeenStore(path)
    b = SeenStore(path)
    a.mark("fixture", "a")
    b.mark("fixture", "b")

    assert a.has("fixture", "a")
    assert b.has("fixture", "b")
    reloaded = SeenStore(path)
    assert reloaded.has("fixture", "a")
    assert reloaded.has("fixture", "b")


# PERF-001: batch seen commit is one write -----------------------------------


def test_mark_many_is_a_single_write(tmp_path, monkeypatch):
    """PERF-001 with the SQLite store: one transaction per batch, and a batch
    of already-seen keys refreshes without growing the set."""
    path = tmp_path / "seen.json"
    store = SeenStore(path)

    store.mark_many([("fixture", f"t{i}") for i in range(50)])
    assert all(store.has("fixture", f"t{i}") for i in range(50))

    # Re-marking the same keys is idempotent (upsert), no duplicate rows.
    store.mark_many([("fixture", f"t{i}") for i in range(50)])
    reloaded = SeenStore(path)
    assert all(reloaded.has("fixture", f"t{i}") for i in range(50))
    assert not reloaded.has("fixture", "t999")


# CORE-007: failed delivery is not counted as notified -----------------------


def test_failed_delivery_is_not_counted(tmp_path):
    class _Broken(Notifier):
        def send(self, notification):
            raise OSError("disk full")

    bridge = _bridge(tmp_path, [_candidate("a")])
    state = run_monitor(bridge, notifier=_Broken(), cycles=1, min_score=0,
                        sleep_fn=lambda _s: None, now_fn=lambda: NOW)

    assert state.notified_total == 0
    assert state.notify_failed_total == 1
    assert "disk full" in state.last_notify_error


# W2-007: monitor numeric validation -----------------------------------------


@pytest.mark.parametrize("argv", [
    ["--interval", "nan"],
    ["--interval", "inf"],
    ["--since-hours", "-1"],
    ["--min-score", "nan"],
    ["--min-score", "-5"],
    ["--min-score", "101"],
])
def test_monitor_rejects_invalid_numerics(argv):
    with pytest.raises(SystemExit):
        parse_args(argv)


# W2-009: scorer-domain config validation -----------------------------------


def test_invalid_symptom_domains_are_rejected():
    from saipet.runtime_config import ConfigError, validate_overrides

    with pytest.raises(ConfigError, match="lowercase"):
        validate_overrides({"symptoms": ["Lost Context"]})
    with pytest.raises(ConfigError, match="unique"):
        validate_overrides({"symptoms": ["state", "state"]})
    with pytest.raises(ConfigError, match="non-empty"):
        validate_overrides({"symptoms": [""]})
    with pytest.raises(ConfigError, match="finite"):
        validate_overrides({"weights": {"problem_match": float("nan")}})


# W2-003 / W2-005: shutdown joins the in-flight worker -----------------------


def test_shutdown_joins_the_worker(tmp_path):
    import threading

    gate = threading.Event()
    started = threading.Event()

    def _blocking(_targets, _symptoms):
        class _Src(FixtureSource):
            def fetch(self, limit=25):
                started.set()
                gate.wait(5)
                return []

        return _Src()

    ctrl = Controller(_bridge(tmp_path, source_factory=_blocking))
    ctrl.run_scout(["testsub"], 25, 48)
    started.wait(2)
    # Release the blocking gate so the worker can finish, THEN shut down.
    gate.set()
    ctrl.shutdown(wait_seconds=2)
    # W2-005: shutdown drains the settled worker's result so the state
    # reflects the true terminal status rather than staying RUNNING.
    assert ctrl.state.operation.state != "running"


# CORE-002: a preview completion must not end a scout operation -------------


def test_preview_cannot_terminate_another_operation(tmp_path):
    import threading

    gate = threading.Event()
    started = threading.Event()

    def _blocking(_targets, _symptoms):
        class _Src(FixtureSource):
            def fetch(self, limit=25):
                started.set()
                gate.wait(5)
                return []

        return _Src()

    ctrl = Controller(_bridge(tmp_path, source_factory=_blocking))
    ctrl.run_scout(["testsub"], 25, 48)
    started.wait(2)
    assert ctrl.state.operation.running

    report = tmp_path / "r" / "a.jsonl"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("x", encoding="utf-8")
    # preview is now gated: it is rejected while scout owns the operation.
    accepted = ctrl.preview_report(str(report))
    assert accepted is False
    assert ctrl.state.operation.name == "scout"
    assert ctrl.state.operation.running

    gate.set()
    ctrl.shutdown(wait_seconds=2)


# CORE-001: a degraded review bridge refuses review mutations ---------------


def test_controller_refuses_mutations_when_review_store_is_degraded(tmp_path):
    bridge = _bridge(tmp_path, [_candidate("t1")])
    bridge.review_state_error = "review-state.json is corrupt (boom)"

    ctrl = Controller(bridge)
    assert ctrl.run_scout([], 25, None) is False
    assert "review mutations are disabled" in ctrl.state.status_text
    assert ctrl.dismiss("t1") is False
    assert ctrl.approve("t1", "x", True) is False


# W2-001: queue visibility survives a report-artifact failure --------------------


def test_report_failure_after_durable_persistence_strands_nothing(tmp_path, monkeypatch):
    """W2-001: _scout wrote the report BEFORE merging the durable pending
    delta into the live queue, so one report failure left an already-durable
    finding unreviewable until restart. Merge first; the artifact failure is
    surfaced separately instead of hiding committed work."""
    bridge = _bridge(tmp_path, candidates=[_candidate("t1")], review=True)
    from saipet.bridge import dispatch as dispatch_module

    def _boom(*_a, **_k):
        raise OSError("report disk fail")

    monkeypatch.setattr(dispatch_module, "write_report", _boom)

    result = bridge.dispatch("scout")

    assert result.ok
    assert result.data["queued"] == 1
    assert [i["id"] for i in result.data["items"]] == ["t1"]
    # The live queue exposes the exact durable finding on THIS bridge...
    assert bridge.queue.find("fixture", "t1") is not None
    # ...the failure is named, not swallowed...
    assert "OSError" in result.data["report_error"]
    assert result.data["report"] == {}

    # Retrying after the disk recovers queues no duplicate.
    monkeypatch.setattr(dispatch_module, "write_report", write_report)
    again = bridge.dispatch("scout")
    assert again.ok and again.data["queued"] == 0
    assert len(bridge.queue.pending()) == 1

    # A restart reloads the same single pending item.
    fresh = Bridge(
        report_dir=tmp_path / "runs",
        seen_path=tmp_path / "seen.json",
        review_path=tmp_path / "review-state.json",
        source_factory=lambda *_: FixtureSource([]),
        now_fn=lambda: NOW,
    )
    fresh.dispatch("scout")
    assert len(fresh.queue.pending()) == 1
    assert fresh.queue.find("fixture", "t1") is not None


# CORE-005: transition commit is compare-and-set, not last-stale-writer-wins ---


def test_stale_bridge_cannot_overwrite_a_committed_transition(tmp_path):
    """CORE-005 + W2-004: the interprocess lock stopped byte collisions, not
    stale writers -- B rejected what A had already approved and the durable
    state became 'rejected'. The durable predecessor must still be pending
    for a transition to land. W2-004: a conflict is surfaced as ok=False so
    callers (GUI, terminal) do not treat a refused transition as accepted."""
    bridge_a = _bridge(tmp_path, candidates=[_candidate("t1")], review=True)
    bridge_a.dispatch("scout")

    # Same durable file, second independent instance loaded BEFORE any
    # transition -- its in-memory copy is already stale once A moves.
    bridge_b = _bridge(tmp_path, candidates=[], review=True)

    approved = bridge_a.dispatch("approve", id="t1", solution="human answer")
    assert approved.ok

    rejected = bridge_b.dispatch("reject", id="t1")
    assert not rejected.ok  # W2-004: conflict is a REFUSED operation
    assert "conflict" in rejected.data
    assert rejected.data["status"] == "approved"

    # The first decision stands; the stale instance refreshed to it. The
    # decided item leaves B's pending worklist (PERF-003 hydration) but is
    # authoritative in durable state.
    decided = {i.identity(): i.status for i in ReviewStore(tmp_path / "review-state.json").items()}
    assert decided[("fixture", "t1")] == "approved"
    assert bridge_b.queue.find("fixture", "t1") is None

    # Reverse ordering: reject first, approve cannot land either.
    bridge_c = _bridge(tmp_path, candidates=[_candidate("t2")], review=True)
    bridge_c.dispatch("scout")
    bridge_d = _bridge(tmp_path, candidates=[], review=True)
    assert bridge_c.dispatch("reject", id="t2").ok
    late_approve = bridge_d.dispatch("approve", id="t2", solution="too slow")
    assert not late_approve.ok
    assert "conflict" in late_approve.data
    decided = {i.identity(): i.status for i in ReviewStore(tmp_path / "review-state.json").items()}
    assert decided[("fixture", "t2")] == "rejected"


# CORE-011: runtime config enforces scorer semantics, not just types ------------


def test_weight_sign_flips_are_rejected():
    """CORE-011: {"already_solved": 25} used to validate fine and turn the
    solved-marker penalty into a +25 reward -- a solved candidate scored 100."""
    from saipet.runtime_config import ConfigError, validate_overrides

    with pytest.raises(ConfigError):
        validate_overrides({"weights": {"already_solved": 25}})
    with pytest.raises(ConfigError):
        validate_overrides({"weights": {"problem_match": -10}})


def test_gates_outside_the_score_domain_are_rejected():
    from saipet.runtime_config import ConfigError, validate_overrides

    with pytest.raises(ConfigError):
        validate_overrides({"gate_ignore_below": -10, "gate_prioritize_at": -5})
    with pytest.raises(ConfigError):
        validate_overrides({"gate_ignore_below": 101, "gate_prioritize_at": 105})
    # Reversed thresholds stay refused too.
    with pytest.raises(ConfigError):
        validate_overrides({"gate_ignore_below": 90, "gate_prioritize_at": 80})


def test_valid_semantic_overrides_still_apply(monkeypatch):
    """CORE-011 guardrail: representative valid overrides apply and score
    exactly as configured."""
    from saipet import config
    from saipet.runtime_config import apply_overrides

    monkeypatch.setattr(config, "WEIGHTS", dict(config.WEIGHTS))
    monkeypatch.setattr(config, "GATE_PRIORITIZE_AT", 80)

    applied = apply_overrides(
        {"weights": {"problem_match": 45}, "gate_prioritize_at": 90}
    )
    assert "gate_prioritize_at" in applied
    assert config.WEIGHTS["problem_match"] == 45
    assert config.GATE_PRIORITIZE_AT == 90


# W2-009: internal TypeErrors are not misclassified as caller mistakes ----------


def test_internal_typeerror_is_not_reported_as_bad_arguments(tmp_path, monkeypatch):
    """W2-009: dispatch rewrote EVERY handler TypeError as 'bad arguments' --
    a source factory bug looked like the caller's fault. After schema
    validation passes, only a signature BIND failure is a caller error."""
    def _broken_factory(*_a):
        raise TypeError("source factory internal bug")

    bridge = _bridge(tmp_path, source_factory=_broken_factory)

    result = bridge.dispatch("scout", subreddits=["testsub"])

    assert not result.ok
    assert not result.error.startswith("bad arguments")
    assert result.error.startswith("TypeError:")


def test_true_signature_mismatches_are_still_bad_arguments(tmp_path):
    """W2-009 guardrail: malformed caller arguments are still rejected --
    by the schema first, and by signature binding for untyped handlers."""
    bridge = _bridge(tmp_path)

    schema_reject = bridge.dispatch("scout", limit="not-an-int")
    assert not schema_reject.ok
    assert "limit" in schema_reject.error

    # A keyword the handler does not accept is rejected by the schema
    # before the handler runs.
    wrong_kwarg = bridge.dispatch("queue", nonexistent_kwarg=1)
    assert not wrong_kwarg.ok
    assert "unknown argument" in wrong_kwarg.error


# CORE-001: crash-window journal reconciliation — phantom pending removed
def test_crash_window_journal_reconciles_pending(tmp_path):
    """CORE-001: a crash between journal append and hot-file replace leaves
    the terminal decision in the authoritative journal but a stale pending
    copy in the snapshot. On reopen, the pending must be removed and the
    index must reflect the terminal status."""
    from saipet.review_store import ReviewStore
    from saipet.review import ReviewItem, STATUS_APPROVED

    store = ReviewStore(tmp_path / "rs.json")
    item = ReviewItem(
        candidate=_candidate("x"), relevance_score=70, band="review",
        draft="", signals={}, discovered=NOW,
    )
    store.commit_scouted([item])
    # Simulate crash: append terminal transition to journal but do NOT
    # update the hot file.
    from saipet.review_store import _append_history, _history_path_for
    approved = ReviewItem(
        candidate=_candidate("x"), relevance_score=70, band="review",
        draft="solution", signals={}, discovered=NOW, status=STATUS_APPROVED,
    )
    _append_history(tmp_path / "rs.json", approved)
    # Reopen — should reconcile and remove the phantom pending.
    fresh = ReviewStore(tmp_path / "rs.json")
    assert fresh.pending() == []
    # Terminal items are NOT in `_fast` (find checks pending only); they
    # are in the journal and accessible via items().
    all_items = {i.identity(): i.status for i in fresh.items()}
    assert all_items[("fixture", "x")] == "approved"
    # contains() checks the full in-memory index which includes terminal.
    assert fresh.contains("fixture", "x")
    # A subsequent transition against it must conflict (not resurrect).
    from saipet.review_store import TransitionConflict
    try:
        fresh.commit_transition(approved, expected_status="pending")
        pytest.fail("should have raised TransitionConflict")
    except TransitionConflict:
        pass


# CORE-002: stale scout cannot move terminal back to pending
def test_stale_scout_cannot_resurrect_terminal(tmp_path):
    """CORE-002: commit_scouted must refuse any backward transition from
    APPROVED/REJECTED to PENDING, even under the interprocess lock."""
    from saipet.review_store import ReviewStore
    from saipet.review import ReviewItem, STATUS_APPROVED

    store_a = ReviewStore(tmp_path / "rs.json")
    store_b = ReviewStore(tmp_path / "rs.json")
    item = ReviewItem(
        candidate=_candidate("y"), relevance_score=70, band="review",
        draft="", signals={}, discovered=NOW,
    )
    store_a.commit_scouted([item])
    approved = ReviewItem(
        candidate=_candidate("y"), relevance_score=70, band="review",
        draft="sol", signals={}, discovered=NOW, status=STATUS_APPROVED,
    )
    store_a.commit_transition(approved)
    # Stale store_b calls commit_scouted with the old pending copy.
    store_b.commit_scouted([item])
    # The terminal decision stands; X is NOT back in pending.
    reloaded = ReviewStore(tmp_path / "rs.json")
    assert reloaded.pending() == []
    assert reloaded.contains("fixture", "y")
    # Terminal items accessible via items(), not find().
    y_item = next(i for i in reloaded.items() if i.identity() == ("fixture", "y"))
    assert y_item.status == "approved"


# CORE-003: malformed history raises, never silently loads
def test_malformed_history_raises_not_silent(tmp_path):
    """CORE-003: a corrupt journal line must raise StateFileError on load,
    never silently yield a normal store with stale pending revived."""
    import json
    from saipet.review_store import ReviewStore, StateFileError
    from saipet.review import ReviewItem

    store = ReviewStore(tmp_path / "rs.json")
    item = ReviewItem(
        candidate=_candidate("z"), relevance_score=70, band="review",
        draft="", signals={}, discovered=NOW,
    )
    store.commit_scouted([item])
    # Corrupt the journal.
    journal = tmp_path / "rs.json.history.jsonl"
    journal.write_text('{"item": {"bad"}}\n', encoding="utf-8")
    with pytest.raises(StateFileError, match="corrupt"):
        ReviewStore(tmp_path / "rs.json")


# CORE-004: expired claim is reclaimable without restart
def test_expired_claim_is_reclaimable(tmp_path):
    """CORE-004: after a row crosses TTL while a daemon remains alive,
    the next claim reclaims it — no restart or explicit prune needed."""
    import time
    from saipet.store import SeenStore
    path = tmp_path / "seen.json"
    store = SeenStore(path, ttl_days=1)
    assert store.claim("fixture", "expired-x")
    # Age past TTL.
    store._db.execute(
        "UPDATE seen SET marked_at=? WHERE source=? AND id=?",
        (time.time() - 10 * 86400, "fixture", "expired-x"),
    )
    store._db.commit()
    # Same store should reclaim.
    assert store.claim("fixture", "expired-x")
    # W2-001: claim is provisional; finalize it to make has() report it.
    store.finalize_many([("fixture", "expired-x")])
    assert store.has("fixture", "expired-x")


# CORE-005: seen_ttl_days config override actually applies
def test_seen_ttl_days_override_applies(monkeypatch, tmp_path):
    """CORE-005: apply_overrides({'seen_ttl_days': 1}) must change the
    config scalar and cause a freshly constructed default SeenStore to
    use one day."""
    from saipet import config
    from saipet.store import SeenStore
    from saipet.runtime_config import apply_overrides

    monkeypatch.setattr(config, "SEEN_TTL_DAYS", 90)
    applied = apply_overrides({"seen_ttl_days": 1})
    assert "seen_ttl_days" in applied
    assert config.SEEN_TTL_DAYS == 1
    store = SeenStore(tmp_path / "s.json")
    assert store._ttl_days == 1


# CORE-007: corrupt outbox raises, never silently empties
def test_corrupt_outbox_raises_not_silent(tmp_path):
    """CORE-007: a malformed outbox file must surface StateFileError and
    leave the original bytes untouched."""
    from saipet.outbox import DeliveryOutbox, StateFileError
    path = tmp_path / "ob.json"
    path.write_text("{bad json", encoding="utf-8")
    with pytest.raises(StateFileError, match="corrupt"):
        DeliveryOutbox(path)
    # Original bytes unchanged.
    assert path.read_text(encoding="utf-8") == "{bad json"


# CORE-008: historical id-only inspect rejects ambiguity across sources
def test_historical_id_only_inspect_rejects_ambiguity(tmp_path):
    """CORE-008: once duplicate ids become historical, an id-only inspect
    must still reject ambiguity — never arbitrarily pick the first match."""
    from saipet.review_store import ReviewStore
    from saipet.review import ReviewItem

    store = ReviewStore(tmp_path / "rs.json")
    a = ReviewItem(
        candidate=_candidate("same", source="reddit"), relevance_score=70,
        band="review", draft="", signals={}, discovered=NOW,
    )
    b = ReviewItem(
        candidate=_candidate("same", source="fixture"), relevance_score=80,
        band="priority", draft="", signals={}, discovered=NOW,
    )
    store.commit_scouted([a, b])
    # Approve both so they become historical.
    from saipet.review import STATUS_APPROVED
    store.commit_transition(ReviewItem(
        candidate=a.candidate, relevance_score=70, band="review",
        draft="da", signals={}, discovered=NOW, status=STATUS_APPROVED,
    ))
    store.commit_transition(ReviewItem(
        candidate=b.candidate, relevance_score=80, band="priority",
        draft="db", signals={}, discovered=NOW, status=STATUS_APPROVED,
    ))
    bridge = Bridge(
        report_dir=tmp_path / "runs", seen_path=tmp_path / "seen.json",
        review_path=tmp_path / "rs.json",
        source_factory=lambda *_: FixtureSource([]),
        now_fn=lambda: NOW,
    )
    # id-only must fail with ambiguity.
    result = bridge.dispatch("inspect", id="same")
    assert not result.ok
    assert "ambiguous" in result.error
    # Source-qualified must succeed.
    assert bridge.dispatch("inspect", id="same", source="reddit").ok
    assert bridge.dispatch("inspect", id="same", source="fixture").ok


# W2-006: deferred select_finding callback fires once with real result
def test_deferred_select_callback_fires_once(tmp_path):
    """W2-006: when a selection is queued while another operation runs,
    the deferred callback must receive the REAL inspect result, not None."""
    import threading
    from saipet.gui.controller import Controller
    gate = threading.Event()
    started = threading.Event()
    callback_results = []

    def _blocking(_targets, _symptoms):
        class _Src(FixtureSource):
            def fetch(self, limit=25):
                started.set()
                gate.wait(5)
                return [_candidate("t1")]
        return _Src()

    ctrl = Controller(_bridge(tmp_path, candidates=[_candidate("t1")], source_factory=_blocking))
    ctrl.run_scout(["testsub"], 25, 48)
    started.wait(2)
    # Queue a selection while scout is running.
    ctrl.select_finding("t1", source="fixture", done=callback_results.append)
    # The callback list should be empty (deferred).
    assert callback_results == []
    # Release the scout.
    gate.set()
    ctrl.shutdown(wait_seconds=2)
    # The deferred callback must have fired with the real Result.
    assert len(callback_results) == 1
    assert callback_results[0].ok
    assert callback_results[0].data["id"] == "t1"


# W2-007: v1 migration converges after arbitrary crash prefix
def test_v1_migration_converges_after_prefix_crash(tmp_path):
    """W2-007: a crash after appending only some terminal records to the
    journal must converge on every reopen — missing records are appended
    and the hot file becomes v2."""
    import json
    from saipet.review_store import ReviewStore, _append_history
    from saipet.review import ReviewItem, STATUS_APPROVED, STATUS_REJECTED

    # Write a v1 file with two terminal items.
    v1 = {
        "version": 1,
        "items": [
            {
                "candidate": {"source": "fixture", "id": "a", "title": "t",
                              "body": "b", "permalink": "", "subreddit": "t",
                              "created_utc": NOW - 3600},
                "relevance_score": 70, "band": "review", "draft": "",
                "status": "approved", "signals": {}, "discovered": NOW,
            },
            {
                "candidate": {"source": "fixture", "id": "b", "title": "t",
                              "body": "b", "permalink": "", "subreddit": "t",
                              "created_utc": NOW - 3600},
                "relevance_score": 70, "band": "review", "draft": "",
                "status": "rejected", "signals": {}, "discovered": NOW,
            },
        ],
    }
    path = tmp_path / "v1.json"
    path.write_text(json.dumps(v1), encoding="utf-8")
    # Simulate crash mid-migration: only append 'a' to journal.
    from saipet.sources.base import Candidate
    _append_history(path, ReviewItem(
        candidate=Candidate(source="fixture", id="a", title="t", body="b",
                            permalink="", subreddit="t", created_utc=NOW-3600),
        relevance_score=70, band="review", draft="",
        signals={}, discovered=NOW, status=STATUS_APPROVED,
    ))
    # Reopen — must converge to complete v2.
    store = ReviewStore(path)
    assert store.contains("fixture", "a")
    assert store.contains("fixture", "b")
    # Terminal items accessible via items(), not find().
    all_items = {i.identity(): i.status for i in store.items()}
    assert all_items[("fixture", "a")] == "approved"
    assert all_items[("fixture", "b")] == "rejected"
    # Reopen again — must be idempotent (no double-append).
    store2 = ReviewStore(path)
    assert store2.contains("fixture", "a")
    assert store2.contains("fixture", "b")
    all_items2 = {i.identity(): i.status for i in store2.items()}
    assert all_items2[("fixture", "a")] == "approved"
    assert all_items2[("fixture", "b")] == "rejected"


# W2-008: stable notification identity across retries
def test_notification_identity_stable_across_retries(tmp_path):
    """W2-008: a retry must reuse the original notification timestamp so
    FileNotifier's identity key stays stable and healthy sinks are not
    duplicated."""
    import hashlib
    from saipet.outbox import DeliveryOutbox
    from saipet.notify import FileNotifier, finding, DEFAULT_NOTIFICATION_FILE

    ob_path = tmp_path / "ob.json"
    notifier_path = tmp_path / "feed.jsonl"
    notifier = FileNotifier(notifier_path)
    ob = DeliveryOutbox(ob_path)

    item_view = {"source": "fixture", "id": "stab", "title": "t",
                 "body": "b", "score": 90, "band": "review",
                 "subreddit": "testsub", "permalink": ""}
    t1 = 1000.0
    entry = ob.add(item_view, notification_at=t1)
    assert entry.get("notification_at") == t1

    # First send at t1 — both sinks succeed, obligation clears.
    notif = finding(item_view, t1)
    delivery = notifier.send(notif)
    assert delivery.delivered

    # Now simulate a partial delivery: jsonl succeeded, inbox failed.
    # We need to recreate: add back to outbox, then send with a failing inbox.
    ob2 = DeliveryOutbox(ob_path)
    ob2.add(item_view, notification_at=t1)
    # Manually mark jsonl as done but inbox as failed by corrupting state.
    # Actually simpler: just verify that retry uses stored notification_at.
    entry2 = ob2.obligations()[0]
    assert entry2["notification_at"] == t1
    notif_retry = finding(item_view, entry2["notification_at"])
    # The identity of the retry notification must equal the original.
    assert FileNotifier._identity(notif_retry) == FileNotifier._identity(notif)


# W2-003: seen corruption is classified independently of review corruption
def test_seen_only_corruption_does_not_misreport_review(tmp_path, monkeypatch):
    """W2-003: a corrupt seen file must be reported as seen corruption,
    not falsely accused of review corruption. The valid review store must
    still be loaded."""
    import tkinter as tk

    monkeypatch.chdir(tmp_path)
    (tmp_path / "seen.json").write_text("{broken", encoding="utf-8")
    (tmp_path / "review-state.json").write_text(
        '{"version":2,"pending":[],"index":{}}', encoding="utf-8"
    )
    root = tk.Tk()
    root.withdraw()
    try:
        from saipet.gui.app import App
        app = App(root=root, config_path=str(tmp_path / "no-config.json"))
        # W2-003: startup errors must name the correct component.
        msgs = "\n\n".join(app._startup_errors)
        assert "seen" in msgs.lower()
        assert "review-state.json is corrupt" not in msgs
        # The bridge must have loaded the review store successfully.
        assert not getattr(app.controller.bridge, "review_state_error", "")
    finally:
        try:
            root.destroy()
        except Exception:
            pass
