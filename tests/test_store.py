from saipet.store import SeenStore


def test_persists_and_reloads(tmp_path):
    path = tmp_path / "seen.json"
    store = SeenStore(path)
    assert not store.has("reddit", "abc")
    store.mark("reddit", "abc")
    assert store.has("reddit", "abc")

    reloaded = SeenStore(path)  # fresh instance, same file
    assert reloaded.has("reddit", "abc")
    assert not reloaded.has("reddit", "other")


def test_a_key_older_than_the_ttl_is_reconsidered(tmp_path):
    """T-027: an entry past its TTL is treated as absent, so a long-deduped
    thread becomes eligible for review again."""
    import time

    store = SeenStore(tmp_path / "seen.json", ttl_days=90)
    store.mark("reddit", "old")
    # Age the row well past any TTL window, deterministically.
    store._db.execute(
        "UPDATE seen SET marked_at = ? WHERE source='reddit' AND id='old'",
        (time.time() - 10_000_000,),
    )
    store._db.commit()

    assert not store.has("reddit", "old")

    # Re-marking refreshes the timestamp and makes it live again.
    store.mark("reddit", "old")
    assert store.has("reddit", "old")


def test_mark_many_is_transactional_and_re_marking_is_idempotent(tmp_path):
    store = SeenStore(tmp_path / "seen.json")
    store.mark_many([("r", "a"), ("r", "b"), ("r", "a")])  # duplicate in batch
    assert store.has("r", "a") and store.has("r", "b")

    reloaded = SeenStore(tmp_path / "seen.json")
    assert reloaded.has("r", "a") and reloaded.has("r", "b")


def test_concurrent_writers_do_not_lose_keys(tmp_path):
    """W2-002 with the SQLite store: two writers marking different keys both
    survive -- SQLite serialises the writes instead of last-writer-wins."""
    path = tmp_path / "seen.json"
    a = SeenStore(path)
    b = SeenStore(path)
    a.mark("fixture", "a")
    b.mark("fixture", "b")

    reloaded = SeenStore(path)
    assert reloaded.has("fixture", "a")
    assert reloaded.has("fixture", "b")


# CORE-006: admission is an atomic cross-process claim --------------------------


def test_claim_admits_exactly_one_of_two_racers(tmp_path):
    """CORE-006: has()-then-mark was two statements -- two processes could
    both see 'unseen' and both admit. claim() is arbitrated by SQLite: exactly
    one winner, and both stores converge on the same fact.

    W2-001: a bare claim is PROVISIONAL -- it reserves the identity for
    single-winner admission but is not a durable "seen" decision until
    finalized (which the scout does only after its durable record lands). A
    provisional claim alone is therefore NOT reported by `has()`."""
    a = SeenStore(tmp_path / "seen.json")
    b = SeenStore(tmp_path / "seen.json")

    assert a.claim("fixture", "t1") is True
    assert b.claim("fixture", "t1") is False  # already claimed -> lost the race

    # W2-001: provisional claim is reconsiderable (not "seen") until finalized.
    assert not a.has("fixture", "t1")
    assert not b.has("fixture", "t1")

    # After the durable record landed, the winner finalizes its claim.
    a.finalize_many([("fixture", "t1")])
    assert a.has("fixture", "t1")
    assert b.has("fixture", "t1")


# CORE-006: admission is an atomic cross-process claim --------------------------

import pytest

from saipet import cli as cli_mod
from saipet.signals import extract_signals
from saipet.sources.base import Candidate, FixtureSource

_NOW = 1_800_000_000.0


def _candidate(candidate_id):
    return Candidate(
        source="fixture",
        id=candidate_id,
        title="agent keeps losing context between sessions",
        body="Lost context, session state, handoff, resume, checkpoint, deterministic protocol.",
        permalink=f"https://reddit.com/r/testsub/{candidate_id}",
        subreddit="testsub",
        created_utc=_NOW - 3600,
    )


def test_claim_admits_exactly_one_of_two_racers(tmp_path):
    """CORE-006: has()-then-mark was two statements -- two processes could
    both see 'unseen' and both admit. claim() is one INSERT, arbitrated by
    SQLite: exactly one winner, and both stores converge on the same fact.

    W2-001: a bare claim is PROVISIONAL -- it reserves the identity for
    single-winner admission but is not a durable "seen" decision until
    finalized. A provisional claim alone is therefore NOT reported by `has()`."""
    a = SeenStore(tmp_path / "seen.json")
    b = SeenStore(tmp_path / "seen.json")

    assert a.claim("fixture", "t1") is True
    assert b.claim("fixture", "t1") is False  # already claimed -> lost the race

    # W2-001: provisional claims stay reconsiderable until finalized.
    assert not a.has("fixture", "t1")
    assert not b.has("fixture", "t1")

    a.finalize_many([("fixture", "t1")])
    assert a.has("fixture", "t1")
    assert b.has("fixture", "t1")


def test_release_undoes_own_failed_persist(tmp_path):
    store = SeenStore(tmp_path / "seen.json")
    assert store.claim("fixture", "t1")
    store.release("fixture", "t1")
    assert not store.has("fixture", "t1")  # reconsiderable again


def test_scout_releases_claims_when_persistence_fails(tmp_path, monkeypatch):
    """W2-001 ordering under CORE-006 claims: a failed durable write must
    undo every claim made during the run so nothing becomes seen-but-lost."""
    monkeypatch.setattr("saipet.policy.SUBREDDIT_ALLOWLIST", {"testsub"})
    monkeypatch.setattr("saipet.config.SUBREDDIT_ALLOWLIST", {"testsub"})

    store = SeenStore(tmp_path / "seen.json")

    def _boom(*_a, **_k):
        raise OSError("persist disk fail")

    with pytest.raises(OSError):
        cli_mod.scout(
            FixtureSource([_candidate("t1")]),
            signal_fn=extract_signals,
            seen=store,
            persist=_boom,
            now_fn=lambda: _NOW,
        )

    assert not store.has("fixture", "t1")


def test_scout_claims_admitted_findings_atomically(tmp_path, monkeypatch):
    monkeypatch.setattr("saipet.policy.SUBREDDIT_ALLOWLIST", {"testsub"})
    monkeypatch.setattr("saipet.config.SUBREDDIT_ALLOWLIST", {"testsub"})

    store = SeenStore(tmp_path / "seen.json")
    cli_mod.scout(
        FixtureSource([_candidate("t1")]),
        signal_fn=extract_signals,
        seen=store,
        now_fn=lambda: _NOW,
    )
    assert store.has("fixture", "t1")

    # A second scout on the same store sees the claim and queues nothing.
    second = cli_mod.scout(
        FixtureSource([_candidate("t1")]),
        signal_fn=extract_signals,
        seen=store,
        now_fn=lambda: _NOW,
    )
    assert len(second.pending()) == 0
