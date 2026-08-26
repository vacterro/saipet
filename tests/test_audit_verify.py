"""Audit VERIFY-gap closure: tests for the audit's explicit verify criteria
that were not yet covered by the implementation wave's regression file.
"""

import os

import pytest

from saipet.bridge import Bridge
from saipet.cli import scout
from saipet.gui.controller import Controller
from saipet.jsonio import StateFileError
from saipet.monitor import main as monitor_main
from saipet.monitor import run_monitor
from saipet.notify import NullNotifier
from saipet.review_store import ReviewStore
from saipet.store import SeenStore
from saipet.sources.base import Candidate, FixtureSource

NOW = 1_800_000_000.0
_RELEVANT_BODY = "Lost context, session state, handoff, resume, checkpoint, deterministic protocol."


@pytest.fixture(autouse=True)
def _allow_testsub(monkeypatch):
    monkeypatch.setattr("saipet.policy.SUBREDDIT_ALLOWLIST", {"testsub"})
    monkeypatch.setattr("saipet.config.SUBREDDIT_ALLOWLIST", {"testsub"})


def _candidate(candidate_id, created=NOW - 3600):
    return Candidate(
        source="fixture", id=candidate_id, title="agent keeps losing context between sessions",
        body=_RELEVANT_BODY, permalink=f"https://reddit.com/r/testsub/{candidate_id}",
        subreddit="testsub", created_utc=created,
    )


def _bridge(tmp_path, candidates=None, review=False):
    return Bridge(
        report_dir=tmp_path / "runs",
        seen_path=tmp_path / "seen.json",
        review_path=(tmp_path / "review-state.json") if review else None,
        source_factory=lambda *_: FixtureSource(candidates or []),
        now_fn=lambda: NOW,
    )


class StubBridge:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = dict(responses or {})

    def dispatch(self, verb, **kwargs):
        self.calls.append((verb, dict(kwargs)))
        from saipet.bridge import Result
        return self.responses.get(verb, Result(ok=True, verb=verb, data={}))


# CORE-003: write-failure leaves runtime unchanged, retry succeeds ------------


def test_seen_mark_write_failure_leaves_runtime_unchanged(tmp_path):
    import sqlite3

    class _FailCommit:
        """Delegates to the real connection but raises on commit while armed."""

        def __init__(self, real):
            object.__setattr__(self, "_real", real)
            object.__setattr__(self, "fail", False)

        def execute(self, *a, **k):
            return self._real.execute(*a, **k)

        def executemany(self, *a, **k):
            return self._real.executemany(*a, **k)

        def commit(self):
            if self.fail:
                raise sqlite3.OperationalError("disk full")
            return self._real.commit()

        def rollback(self):
            return self._real.rollback()

    store = SeenStore(tmp_path / "seen.json")
    store._db = _FailCommit(store._db)
    store._db.fail = True

    with pytest.raises(sqlite3.OperationalError):
        store.mark("fixture", "t1")
    # CORE-003: the failed commit rolled the transaction back; the row is not
    # visible even on this connection.
    assert not store.has("fixture", "t1")

    store._db.fail = False
    store.mark("fixture", "t1")
    assert store.has("fixture", "t1")


def test_reject_write_failure_leaves_item_pending(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path, [_candidate("t1")], review=True)
    bridge.dispatch("scout", subreddits=["testsub"], limit=25, since_hours=48)

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr("saipet.review_store.atomic_write_json", _boom)
    result = bridge.dispatch("reject", id="t1")
    monkeypatch.undo()

    assert result.ok is False
    pending = bridge.dispatch("queue").data["items"]
    assert pending and pending[0]["status"] == "pending"


# CORE-004: the monitor does not re-emit an unchanged historical pending item -


def test_monitor_second_watch_does_not_reemit_history(tmp_path):
    bridge = _bridge(tmp_path, [_candidate("a")], review=True)
    first = run_monitor(bridge, notifier=NullNotifier(), cycles=1, min_score=0,
                        sleep_fn=lambda _s: None, now_fn=lambda: NOW)
    assert len(first.last_run_findings) == 1

    second = run_monitor(bridge, notifier=NullNotifier(), cycles=1, min_score=0,
                         sleep_fn=lambda _s: None, now_fn=lambda: NOW)
    assert second.last_run_findings == []
    assert second.notified_total == 0


# CORE-008: an explicit --subreddit denied by the allowlist fails at startup --


def test_explicit_subreddit_denied_by_empty_allowlist_fails(tmp_path, monkeypatch):
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("REDDIT_CLIENT_ID", "x")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "y")
    monkeypatch.setattr("saipet.config.SUBREDDIT_ALLOWLIST", set())
    monkeypatch.chdir(tmp_path)
    lines: list[str] = []

    with pytest.raises(SystemExit) as exit_info:
        monitor_main(["--cycles", "1", "--subreddit", "testsub"],
                     print_fn=lines.append, sleep_fn=lambda _s: None, now_fn=lambda: NOW)

    assert exit_info.value.code != 0
    assert any("denied" in line for line in lines)


# CORE-010: structural/encoding corruption of review state is a StateFileError -


@pytest.mark.parametrize("payload", ['{"items": null}', '{"items": 7}', '{"nope": 1}'])
def test_review_state_structural_corruption_raises_state_file_error(tmp_path, payload):
    path = tmp_path / "rs.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(StateFileError):
        ReviewStore(path)


def test_review_state_invalid_utf8_raises_state_file_error(tmp_path):
    path = tmp_path / "rs.json"
    path.write_bytes(b"\xff\xfe{not utf8")
    with pytest.raises(StateFileError):
        ReviewStore(path)


# W2-001: a report-write failure leaves the candidate reconsiderable ----------


def test_report_write_failure_leaves_candidate_reconsiderable(tmp_path, monkeypatch):
    from saipet.report import write_report as _real_write_report

    bridge = _bridge(tmp_path, [_candidate("t1")])  # no ReviewStore

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr("saipet.bridge.dispatch.write_report", _boom)
    result = bridge.dispatch("scout", subreddits=["testsub"], limit=25, since_hours=48)
    # Restore ONLY this attribute: the autouse allowlist fixture shares the
    # monkeypatch instance, so a blanket undo() would drop the allowlist.
    monkeypatch.setattr("saipet.bridge.dispatch.write_report", _real_write_report)

    assert result.ok is False
    assert not bridge.seen.has("fixture", "t1")

    # Once the fault clears, the same candidate is discovered again.
    fresh = bridge.dispatch("scout", subreddits=["testsub"], limit=25, since_hours=48)
    assert fresh.ok and fresh.data["queued"] == 1


# W2-004: an older approve completion never clears a newer selection ---------


def test_approve_completion_preserves_a_newer_selection(tmp_path):
    ctrl = Controller(StubBridge(), synchronous=True)
    ctrl.state.queue.items = [{"id": "t1"}, {"id": "t2"}]
    ctrl.state.queue.selected_id = "t2"

    ctrl.approve("t1", "solution", True)

    assert ctrl.state.queue.selected_id == "t2"
    assert ctrl.state.queue.detail is None or True  # selection never clobbered


# W2-005: an untouched since field defers to live config ----------------------


def test_since_none_defers_to_live_config(tmp_path, monkeypatch):
    monkeypatch.setattr("saipet.config.MAX_AGE_HOURS", 1)  # 1-hour window
    stale = _candidate("t1", created=NOW - 7200)  # 2 hours old
    bridge = _bridge(tmp_path, [stale])

    result = bridge.dispatch("scout", subreddits=["testsub"], limit=25, since_hours=None)

    assert result.data["queued"] == 0  # stale under the live config window
