"""The one atomic JSON helper: every state file write goes through it."""

import json

import pytest

from saipet.jsonio import (
    InterProcessLock,
    StateFileError,
    _lock_path_for,
    atomic_write_json,
    read_json,
)


def test_atomic_write_round_trips(tmp_path):
    target = tmp_path / "state.json"

    atomic_write_json(target, {"items": [1, 2, 3], "name": "x"})

    assert read_json(target) == {"items": [1, 2, 3], "name": "x"}


def test_atomic_write_leaves_no_temp_files(tmp_path):
    target = tmp_path / "state.json"
    atomic_write_json(target, {"a": 1})

    leftovers = [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_atomic_write_overwrites_completely(tmp_path):
    target = tmp_path / "state.json"
    atomic_write_json(target, {"first": "run"})
    atomic_write_json(target, {"second": "run"})

    assert read_json(target) == {"second": "run"}


def test_read_missing_file_returns_none(tmp_path):
    assert read_json(tmp_path / "nope.json") is None


def test_read_corrupt_file_raises_with_path(tmp_path):
    target = tmp_path / "state.json"
    target.write_text("{not json", encoding="utf-8")

    with pytest.raises(StateFileError, match="state.json is corrupt"):
        read_json(target)


def test_corruption_is_never_silently_empty(tmp_path):
    """A corrupt file is an explicit error, never a silent empty dict that
    would look like a fresh start."""
    target = tmp_path / "state.json"
    target.write_text("garbage", encoding="utf-8")

    with pytest.raises(StateFileError):
        read_json(target)
    assert target.exists()  # the corrupt file is left for inspection


def test_atomic_write_creates_parent_directories(tmp_path):
    target = tmp_path / "deep" / "nested" / "state.json"
    atomic_write_json(target, {})

    assert target.exists()
    assert read_json(target) == {}


def test_written_json_is_parsable_and_stable(tmp_path):
    target = tmp_path / "state.json"
    atomic_write_json(target, {"b": 2, "a": 1})

    json.loads(target.read_text(encoding="utf-8"))


# CORE-007 / W2-008: ownership, liveness, and the path contract -----------------


def _backdate(path, seconds):
    import os
    import time as _t

    old = _t.time() - seconds
    os.utime(path, (old, old))


def test_a_live_holder_past_stale_seconds_is_never_stolen(tmp_path):
    """CORE-007: staleness was judged from mtime alone -- a paused or slow
    live holder had its lock broken by the next caller. Age alone is not
    death: the recorded PID must be dead before a break."""
    import pytest

    target = tmp_path / "state.json"
    holder = InterProcessLock(target, stale_seconds=0.05)
    contender = InterProcessLock(target, stale_seconds=0.05)

    with holder:
        _backdate(_lock_path_for(target), 1)  # far past stale_seconds
        with pytest.raises(TimeoutError):
            with contender:
                pass  # must never get in while the holder lives

    # The holder still exits cleanly and removes its own lock.
    assert not _lock_path_for(target).exists()


def test_exit_spares_a_successors_replacement_lock(tmp_path):
    """CORE-007: __exit__ unlinked by path, so a displaced holder deleted
    its SUCCESSOR's lock and let further writers in. Unlink is conditional
    on the owner token now."""
    target = tmp_path / "state.json"
    holder = InterProcessLock(target)
    holder.__enter__()
    try:
        # A successor replaces the lock file while we still hold ours.
        _lock_path_for(target).write_text("424242 successors-token", encoding="utf-8")
    finally:
        holder.__exit__()

    assert _lock_path_for(target).exists()  # not ours to delete
    assert _lock_path_for(target).read_text(encoding="utf-8") == "424242 successors-token"
    _lock_path_for(target).unlink()  # test cleanup


def test_a_genuinely_dead_owner_is_recovered(tmp_path):
    """CORE-007: bounded dead-owner recovery still works -- age past the
    threshold AND a confirmed-dead PID releases the lock to the waiter."""
    import subprocess
    import sys
    import time as _t2

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    dead_pid = proc.pid
    proc.terminate()
    proc.wait(10)

    target = tmp_path / "state.json"
    lock_path = _lock_path_for(target)
    lock_path.write_text(f"{dead_pid} dead-token", encoding="utf-8")
    _backdate(lock_path, 1)

    t0 = _t2.time()
    with InterProcessLock(target, stale_seconds=0.05):
        pass
    assert _t2.time() - t0 < 5


def test_first_write_into_missing_nested_directories(tmp_path):
    """W2-008: SeenStore/ReviewStore take their lock BEFORE any writer runs,
    so a valid nested path failed on the .lock file's missing parent. The
    lock creates its parent exactly like atomic_write_json does."""
    from saipet.review import ReviewItem
    from saipet.review_store import ReviewStore
    from saipet.store import SeenStore
    from saipet.sources.base import Candidate

    seen = SeenStore(tmp_path / "deep" / "nest" / "seen.json")
    seen.mark("fixture", "t1")
    assert SeenStore(tmp_path / "deep" / "nest" / "seen.json").has("fixture", "t1")

    review = ReviewStore(tmp_path / "other" / "review-state.json")

    def _item(candidate_id):
        return ReviewItem(
            candidate=Candidate(
                source="fixture", id=candidate_id, title="t", body="b",
                permalink="https://reddit.com/r/testsub/x", subreddit="testsub",
                created_utc=0.0,
            ),
            relevance_score=50, band="review", draft="", signals={}, discovered=0.0,
        )

    review.commit_scouted([_item("t1")])
    assert ReviewStore(tmp_path / "other" / "review-state.json").contains("fixture", "t1")


def test_concurrent_first_writers_create_parent_without_losing_locking(tmp_path):
    """W2-008 guardrail: parent-dir creation must not weaken exclusivity --
    N threads racing on a fresh path still serialise."""
    import threading

    target = tmp_path / "fresh" / "dir" / "state.json"
    counter = {"inside": 0, "max_inside": 0}
    errors: list[Exception] = []

    def worker():
        try:
            with InterProcessLock(target):
                counter["inside"] += 1
                counter["max_inside"] = max(counter["max_inside"], counter["inside"])
                counter["inside"] -= 1
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert counter["max_inside"] == 1
