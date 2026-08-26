"""Durable pending-delivery state for the unattended monitor.

CORE-004: seen-state answers "did we discover this", not "did the human
hear about it". A sink that fails once used to lose the alert forever --
the candidate was already deduplicated, so no later cycle would retry the
notification. The outbox closes that hole: before the first send attempt
an obligation is recorded on disk, and it stays there until every sink has
confirmed delivery or the entry is explicitly retired.

Shape is deliberately boring: one JSON list of entries, rewritten atomically
on every mutation. The volume is bounded by the notify bar (only
notify-worthy findings enter), and each entry carries enough to redeliver:

- `source` / `id` -- the canonical finding identity;
- `item` -- the item view the notification body is built from;
- `confirmed` -- sinks that already accepted this exact notification, so a
  partial multi-sink failure does not duplicate the healthy half on retry.

PERF-003: obligations are stored in a SQLite table keyed by (source,id) so
each mutation is a point update (INSERT/UPDATE/DELETE) rather than a full
list snapshot. No per-operation JSON serialization of unrelated entries.

PERF-004: each obligation tracks `attempt_count` and `next_attempt_at` so
permanently-failing sinks are paced with exponential backoff instead of
being retried every cycle alongside fresh discoveries.
"""

import json
import sqlite3
import time
from pathlib import Path

from saipet.jsonio import StateFileError


class DeliveryOutbox:
    """Pending notification obligations, persisted across restarts."""

    # PERF-004: max backoff seconds between retry attempts.
    _MAX_BACKOFF_SECONDS = 3600.0  # 1 hour
    # PERF-004: backoff multiplier per consecutive failure.
    _BACKOFF_MULTIPLIER = 2

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._db = sqlite3.connect(str(self.path), timeout=10)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS obligations ("
                "  source TEXT NOT NULL,"
                "  id TEXT NOT NULL,"
                "  item_json TEXT NOT NULL,"
                "  confirmed TEXT NOT NULL DEFAULT '[]',"
                "  failed TEXT NOT NULL DEFAULT '[]',"
                "  notification_at REAL,"
                "  attempt_count INTEGER NOT NULL DEFAULT 0,"
                "  next_attempt_at REAL,"
                "  PRIMARY KEY (source, id)"
                ")"
            )
            # PERF-003: index the retry scheduling column so due-only drains
            # are a point/range query, not a full-table scan.
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS ix_obligations_next_attempt "
                "ON obligations(next_attempt_at)"
            )
            self._db.commit()
        except sqlite3.DatabaseError as exc:
            raise StateFileError(f"{self.path} is corrupt: {exc}") from exc

    @staticmethod
    def _row_to_entry(row) -> dict:
        source, cid, item_json, confirmed, failed, notif_at, attempts, next_at = row
        return {
            "source": source,
            "id": cid,
            "item": json.loads(item_json),
            "confirmed": json.loads(confirmed),
            "failed": json.loads(failed),
            "notification_at": notif_at,
            "attempt_count": attempts,
            "next_attempt_at": next_at,
        }

    def _get(self, source: str, candidate_id: str):
        """PERF-003: point query for exactly one obligation."""
        row = self._db.execute(
            "SELECT source, id, item_json, confirmed, failed, "
            "       notification_at, attempt_count, next_attempt_at "
            "FROM obligations WHERE source = ? AND id = ?",
            (source, candidate_id),
        ).fetchone()
        return self._row_to_entry(row) if row is not None else None

    @property
    def _pending(self) -> dict[str, dict]:
        """Full materialisation, kept for callers/tests that need every
        obligation. Point operations no longer use this (PERF-003)."""
        rows = self._db.execute(
            "SELECT source, id, item_json, confirmed, failed, "
            "       notification_at, attempt_count, next_attempt_at "
            "FROM obligations"
        ).fetchall()
        return {(r[0], r[1]): self._row_to_entry(r) for r in rows}

    @staticmethod
    def _key(source: str, candidate_id: str) -> str:
        return json.dumps([source, candidate_id], ensure_ascii=False)

    def obligations(self) -> list[dict]:
        return [dict(entry) for entry in self._pending.values()]

    def due_obligations(self, now: float) -> list[dict]:
        """PERF-003: only obligations whose retry window has opened. A single
        range query against the indexed column -- never a full-table scan, so
        a large future backlog does not inflate a one-row due drain."""
        rows = self._db.execute(
            "SELECT source, id, item_json, confirmed, failed, "
            "       notification_at, attempt_count, next_attempt_at "
            "FROM obligations WHERE next_attempt_at IS NULL OR next_attempt_at <= ?",
            (now,),
        ).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def add(self, item_view: dict, notification_at: float | None = None) -> dict:
        # PERF-003: point-update INSERT OR IGNORE; the key is the primary
        # so an already-present obligation is left untouched (idempotent
        # against duplicate adds for the same identity). Then read back only
        # the one inserted row -- no whole-table scan.
        key = (item_view.get("source"), item_view.get("id"))
        self._db.execute(
            "INSERT OR IGNORE INTO obligations "
            "(source, id, item_json, confirmed, failed, notification_at, "
            " attempt_count, next_attempt_at) VALUES (?, ?, ?, '[]', '[]', ?, 0, NULL)",
            (key[0], key[1], json.dumps(item_view, ensure_ascii=False), notification_at),
        )
        self._db.commit()
        entry = self._get(key[0], key[1])
        return dict(entry) if entry else {}

    def record_failure(self, source: str, candidate_id: str, sink_name: str, reason: str) -> None:
        """One sink rejected this notification; remember it for targeting.
        Also bumps the attempt counter and schedules the next retry with
        exponential backoff (PERF-004). PERF-003: point query, no full scan."""
        row = self._db.execute(
            "SELECT failed, attempt_count FROM obligations WHERE source = ? AND id = ?",
            (source, candidate_id),
        ).fetchone()
        if row is None:
            return
        existing, attempts = json.loads(row[0]), row[1]
        known = [name for name, _r in existing]
        if sink_name not in known:
            existing.append([sink_name, reason])
        new_attempts = attempts + 1
        # Exponential backoff: 1x, 2x, 4x, 8x ... capped at _MAX_BACKOFF_SECONDS.
        backoff = min(self._MAX_BACKOFF_SECONDS, self._BACKOFF_MULTIPLIER ** (new_attempts - 1))
        next_at = time.time() + backoff
        self._db.execute(
            "UPDATE obligations SET failed=?, attempt_count=?, next_attempt_at=? "
            "WHERE source=? AND id=?",
            (json.dumps(existing, ensure_ascii=False), new_attempts, next_at, source, candidate_id),
        )
        self._db.commit()

    def record_confirmed(self, source: str, candidate_id: str, sink_names) -> None:
        """Sinks that accepted this notification must not receive it twice.
        PERF-003: point query, no full scan."""
        row = self._db.execute(
            "SELECT confirmed FROM obligations WHERE source = ? AND id = ?",
            (source, candidate_id),
        ).fetchone()
        if row is None:
            return
        existing = json.loads(row[0])
        for name in sink_names:
            if name not in existing:
                existing.append(name)
        self._db.execute(
            "UPDATE obligations SET confirmed=? WHERE source=? AND id=?",
            (json.dumps(existing, ensure_ascii=False), source, candidate_id),
        )
        self._db.commit()

    def remove(self, source: str, candidate_id: str) -> bool:
        cursor = self._db.execute(
            "DELETE FROM obligations WHERE source=? AND id=?", (source, candidate_id)
        )
        self._db.commit()
        return cursor.rowcount > 0

    def close(self) -> None:
        try:
            self._db.close()
        except sqlite3.Error:
            pass
