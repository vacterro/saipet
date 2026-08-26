"""Durable seen-state store.

Tracks which (source, id) candidates have already been processed, so a later
scout never re-surfaces one. Backed by SQLite (stdlib) with a
`(source, id)` primary key (T-027): transactional, indexed, and TTL-aware.

Why SQLite instead of a JSON snapshot:
- every `mark_many` is one transaction, not a whole-file rewrite (PERF-001);
- concurrent writers are serialised by SQLite itself, so two processes
  (monitor + GUI) cannot silently overwrite each other's committed keys
  (W2-002) -- `INSERT ... ON CONFLICT DO UPDATE` refreshes the timestamp
  without clobbering unrelated rows;
- rows carry `marked_at`, and `has()` treats an entry older than the TTL as
  absent, so a long-deduped thread can eventually be reconsidered.

W2-001: admission is TWO-phased. `claim()` creates a PROVISIONAL row
(`finalized=0`, owned by the claiming process, carrying an owner token). It is
the atomic single-winner gate, but it is NOT a durable "seen" decision until
`finalize_many()` runs -- which the scout does only AFTER its durable record
(review store / report / outbox) has been written. `has()` only reports
FINALIZED non-expired rows, so a crash after the provisional claim but before
the durable record leaves the candidate reconsiderable instead of
seen-and-lost.

CORE-009: a claim is owned by an opaque per-claim token, not merely by PID.
Two store instances can share a PID, so PID alone cannot stop a slow original
scout from deleting or prematurely finalizing a successor's claim. `release`
and `finalize_many` are conditional on that token, so a stale owner becomes
incapable of mutating a successor's row.

CORE-002: a legacy v0.24 `seen.json` was a JSON array of `"source:id"` keys.
The current file is SQLite; opening the old format raised "file is not a
database". On open we detect the legacy list and migrate it once into SQLite
as finalized rows, preserving the original on any failure.
"""

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

from saipet import config
from saipet.jsonio import StateFileError, pid_alive


def _maybe_migrate_legacy_seen(path: Path) -> bool:
    """CORE-002: if `path` is a legacy v0.24 JSON-list seen store, migrate it
    once into the SQLite format. Returns True if migration ran.

    The original file is preserved (moved aside) so a failure never destroys
    established dedup state; the new SQLite database is written to a temp file
    and only published via atomic rename.
    """
    if not path.exists():
        return False
    # Already a SQLite database? Then nothing to do.
    try:
        probe = sqlite3.connect(str(path), timeout=1)
        probe.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='seen'"
        )
        probe.close()
        return False
    except sqlite3.DatabaseError:
        pass  # not a SQLite db -> possibly legacy JSON
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        # Not legacy JSON either; let the real connect surface the error.
        return False
    if not isinstance(data, list):
        return False
    rows = []
    for entry in data:
        if isinstance(entry, str) and ":" in entry:
            source, candidate_id = entry.split(":", 1)
            if not source or not candidate_id:
                raise StateFileError(f"{path} is corrupt: empty legacy seen key")
            rows.append((source, candidate_id))
        elif (
            isinstance(entry, dict)
            and isinstance(entry.get("source"), str)
            and isinstance(entry.get("id"), str)
        ):
            rows.append((entry["source"], entry["id"]))
        else:
            raise StateFileError(
                f"{path} is corrupt: unrecognized legacy seen entry {entry!r}"
            )
    backup = path.with_name(path.name + ".legacy-bak")
    tmp_db = path.with_name(path.name + ".migrate.tmp")
    try:
        if tmp_db.exists():
            tmp_db.unlink()
        db = sqlite3.connect(str(tmp_db), timeout=10)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS seen ("
                "  source TEXT NOT NULL,"
                "  id TEXT NOT NULL,"
                "  marked_at REAL NOT NULL,"
                "  finalized INTEGER NOT NULL DEFAULT 0,"
                "  owner_pid INTEGER,"
                "  owner_token TEXT,"
                "  PRIMARY KEY (source, id))"
            )
            now = time.time()
            db.executemany(
                "INSERT INTO seen (source, id, marked_at, finalized, owner_pid, owner_token) "
                "VALUES (?, ?, ?, 1, NULL, NULL)",
                [(s, cid, now) for s, cid in rows],
            )
            db.commit()
        finally:
            db.close()
        if backup.exists():
            backup.unlink()
        os.replace(str(path), str(backup))
        os.replace(str(tmp_db), str(path))
    except BaseException:
        if tmp_db.exists():
            try:
                tmp_db.unlink()
            except OSError:
                pass
        # Restore the original if we managed to move it aside.
        if backup.exists() and not path.exists():
            os.replace(str(backup), str(path))
        raise
    return True


class SeenStore:
    """SQLite-backed (source, id) dedup set with TTL expiry and two-phase
    admission (W2-001: provisional claim -> durable persist -> finalize)."""

    def __init__(self, path: str | Path = "seen.json", ttl_days: float | None = None):
        self.path = Path(path)
        self._ttl_days = config.SEEN_TTL_DAYS if ttl_days is None else ttl_days
        # CORE-009: a per-instance token namespace so claim ownership is not
        # confused across instances that happen to share a PID.
        self._instance_token = uuid.uuid4().hex
        self._claims: dict[tuple[str, str], str] = {}
        self._next_prune = 0.0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _maybe_migrate_legacy_seen(self.path)
        try:
            # The GUI creates the store on the UI thread but drives it from
            # its single worker; `check_same_thread=False` allows that, and
            # the controller's one-worker rule plus SQLite's own locking keeps
            # access serialised.
            self._db = sqlite3.connect(str(self.path), timeout=10, check_same_thread=False)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA busy_timeout=10000")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS seen ("
                "  source TEXT NOT NULL,"
                "  id TEXT NOT NULL,"
                "  marked_at REAL NOT NULL,"
                "  finalized INTEGER NOT NULL DEFAULT 0,"
                "  owner_pid INTEGER,"
                "  owner_token TEXT,"
                "  PRIMARY KEY (source, id)"
                ")"
            )
            # PERF-005: index expiry deletions so a long-lived daemon does not
            # scan the whole table when pruning.
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS ix_seen_marked_at ON seen(marked_at)"
            )
            self._ensure_owner_token_column()
            self._db.commit()
        except sqlite3.DatabaseError as exc:
            # A corrupt or foreign file at the path is reported explicitly so
            # the GUI's controlled recovery path can act (CORE-010), not a
            # raw sqlite traceback.
            raise StateFileError(f"{self.path} is corrupt: {exc}") from exc
        self.prune_expired()

    def _ensure_owner_token_column(self) -> None:
        cols = {
            r[1]
            for r in self._db.execute("PRAGMA table_info(seen)").fetchall()
        }
        if "owner_token" not in cols:
            try:
                self._db.execute("ALTER TABLE seen ADD COLUMN owner_token TEXT")
                self._db.commit()
            except sqlite3.OperationalError:
                pass

    def _cutoff(self) -> float:
        return time.time() - self._ttl_days * 86400

    # -- maintenance ---------------------------------------------------

    def _maybe_prune(self) -> None:
        """PERF-005: opportunistically drop expired rows at most daily, tied
        to write activity so a long-lived process does not accumulate dead
        rows for its whole lifetime. Never prunes on every lookup."""
        now = time.time()
        if now >= self._next_prune:
            self.prune_expired()
            self._next_prune = now + 86400.0

    def maintain(self) -> None:
        """PERF-005: explicit maintenance hook for the monitor's write/maintenance
        path; forces a prune if the daily deadline has passed."""
        self._maybe_prune()

    # -- query ---------------------------------------------------------

    def has(self, source: str, candidate_id: str) -> bool:
        # W2-001: only FINALIZED rows count as seen. A provisional claim whose
        # durable record never landed stays reconsiderable. CORE-004: rows
        # older than the TTL are absent too.
        row = self._db.execute(
            "SELECT 1 FROM seen WHERE source = ? AND id = ? "
            "AND marked_at >= ? AND finalized = 1",
            (source, candidate_id, self._cutoff()),
        ).fetchone()
        return row is not None

    # -- atomic claim (CORE-006 / CORE-004 / W2-001 / CORE-009) ---------

    def claim(self, source: str, candidate_id: str) -> bool:
        """Atomically claim an unseen identity. True iff THIS caller won.

        CORE-006: the check-and-reserve is arbitrated by SQLite inside one
        transaction, so exactly one process ever wins a given `(source, id)`,
        and `has()` always consults the live database -- there is no stale
        in-memory snapshot to converge.

        W2-001: the claim is PROVISIONAL (`finalized=0`), owned by this
        process's PID and a unique per-claim token (CORE-009). It reserves the
        identity for single-winner admission without making it a durable seen
        decision -- the caller must `finalize_many()` after its durable record
        lands, and `has()` ignores unfinalized rows, so a crash in between
        leaves the identity reconsiderable. A provisional row whose owner is
        dead (a crashed writer) is reclaimed by the next claimant.

        CORE-004: an EXPIRED row (finalized or not) is reclaimed in the same
        statement, so a long-lived daemon can reconsider an old finding
        without waiting for a restart. A FINALIZED non-expired row makes the
        claimant lose (still claimed).
        """
        now = time.time()
        cutoff = self._cutoff()
        claim_token = uuid.uuid4().hex
        with self._db:
            row = self._db.execute(
                "SELECT finalized, owner_pid, owner_token, marked_at FROM seen "
                "WHERE source = ? AND id = ?",
                (source, candidate_id),
            ).fetchone()
            if row is not None:
                finalized, owner_pid, owner_token, marked_at = row
                expired = marked_at < cutoff
                stale_provisional = finalized == 0 and not pid_alive(owner_pid)
                if not expired and not stale_provisional:
                    # A live finalized row, or a live owner's provisional claim.
                    return False
            self._db.execute(
                "INSERT INTO seen (source, id, marked_at, finalized, owner_pid, owner_token) "
                "VALUES (?, ?, ?, 0, ?, ?) "
                "ON CONFLICT(source, id) DO UPDATE SET "
                "  marked_at = excluded.marked_at,"
                "  finalized = 0,"
                "  owner_pid = excluded.owner_pid,"
                "  owner_token = excluded.owner_token",
                (source, candidate_id, now, os.getpid(), claim_token),
            )
        self._claims[(source, candidate_id)] = claim_token
        self._maybe_prune()
        return True

    def finalize_many(self, pairs) -> None:
        """Promote provisional claims to final seen records (W2-001).

        Called by the scout ONLY after its durable record (review store /
        report / outbox) has been written. CORE-009: only claims THIS instance
        actually owns (matching owner token) are finalized -- a successor's
        claim cannot be prematurely finalized by a stale original writer.
        """
        if not pairs:
            return
        tracked = [(s, cid) for (s, cid) in pairs if (s, cid) in self._claims]
        if not tracked:
            return
        with self._db:
            for source, candidate_id in tracked:
                token = self._claims[(source, candidate_id)]
                self._db.execute(
                    "UPDATE seen SET finalized = 1 "
                    "WHERE source = ? AND id = ? AND owner_token = ?",
                    (source, candidate_id, token),
                )
        for key in tracked:
            self._claims.pop(key, None)
        self._maybe_prune()

    def release(self, source: str, candidate_id: str) -> None:
        """Undo a claim (persist failed). The identity becomes admissible
        again; if another writer claimed it meanwhile, their claim wins --
        CORE-009: only our own token matches, so a stale owner cannot delete a
        successor's row."""
        token = self._claims.get((source, candidate_id))
        if token is None:
            # Not a claim this instance won; never touch another's row.
            return
        self._db.execute(
            "DELETE FROM seen WHERE source = ? AND id = ? AND owner_token = ?",
            (source, candidate_id, token),
        )
        self._db.commit()
        self._claims.pop((source, candidate_id), None)

    # -- write ---------------------------------------------------------

    def mark(self, source: str, candidate_id: str) -> None:
        self.mark_many([(source, candidate_id)])

    def mark_many(self, pairs) -> None:
        """Durably mark several (source, id) pairs in one transaction.

        The non-claim admission path writes rows as FINALIZED directly -- the
        caller has already persisted its durable record. Re-marking an existing
        key refreshes its timestamp. Because the write is a single SQLite
        transaction, it is either fully committed or not at all.
        """
        if not pairs:
            return
        now = time.time()
        try:
            self._db.executemany(
                "INSERT INTO seen (source, id, marked_at, finalized, owner_pid, owner_token) "
                "VALUES (?, ?, ?, 1, NULL, NULL) "
                "ON CONFLICT(source, id) DO UPDATE SET "
                "  marked_at = excluded.marked_at,"
                "  finalized = 1",
                [(source, candidate_id, now) for source, candidate_id in pairs],
            )
            self._db.commit()
        except sqlite3.Error:
            # CORE-003: a failed commit must not publish the row. Roll the
            # transaction back so `has()` on this connection reads only
            # committed state, then re-raise.
            self._db.rollback()
            raise
        self._maybe_prune()

    # -- maintenance ---------------------------------------------------

    def prune_expired(self, max_age_days: float | None = None) -> int:
        """Delete rows older than the TTL. Returns the number removed. The
        query path treats expired rows as absent regardless, so pruning is
        tidy rather than correctness-critical."""
        cutoff = time.time() - (max_age_days if max_age_days is not None else self._ttl_days) * 86400
        cursor = self._db.execute("DELETE FROM seen WHERE marked_at < ?", (cutoff,))
        self._db.commit()
        return cursor.rowcount

    def close(self) -> None:
        try:
            self._db.close()
        except sqlite3.Error:
            pass
