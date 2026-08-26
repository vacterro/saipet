"""Durable review state, split into a hot file and a cold journal.

The in-memory ReviewQueue alone loses pending findings on exit: a candidate
is marked seen during a scout, so a queue that disappears is a candidate that
never returns. This store persists review state so a pending opportunity can
never become permanently unreachable -- while keeping every hot-path cost a
function of the ACTIVE workload, not of project age.

Three coordinated pieces, one invariant each:

- `<path>` -- the HOT state. A small JSON document (`version: 2`) holding
  only the pending worklist plus a byte checkpoint into the journal. Its size
  tracks the live backlog, never lifetime history (PERF-001).
- `<path>.history.jsonl` -- the COLD journal. One appended line per terminal
  transition (approved/rejected), carrying the complete serialized item.
  Appends are O(1); nothing in the hot path ever rewrites them. A transition
  appends BEFORE the hot-file replace, so a crash between the two leaves at
  worst a duplicate line -- replay is idempotent by identity (CORE-001).
- `<path>.index.sqlite` -- the TERMINAL index. One row per terminal identity
  carrying its status and the exact `(offset, length)` byte span of its
  journal record. This makes a single historical lookup a point query plus
  one journal seek, never a whole-journal materialisation, so inspect/approve
  of an old finding stay O(1) regardless of how many terminal records exist
  (PERF-002).

Writes to all three are crash-safe (jsonio.atomic_write_json for the hot
file, fsync'd append for the journal, transactional SQLite for the index) and
serialised by an inter-process lock. Every commit reloads the hot state under
the lock before merging; the transition is a compare-and-set against the
expected predecessor status, so a stale writer cannot overwrite a decision
another process already committed (TransitionConflict).

Identity is the canonical (source, id) pair. Loading validates the format
version and every persisted enum/domain strictly (CORE-008): an unsupported
version or an invalid status/band/number (or a contradictory pending-vs-
terminal state) raises StateFileError instead of becoming live state that is
neither actionable nor rediscoverable. A torn trailing journal append (an
interrupted write) is recovered without poisoning the store (W2-003); a
malformed interior line is hard corruption.

Stale cross-process reads are fixed by an explicit `refresh()` (W2-002): the
methods named for reading queue/inspect now re-read durable state at the
external synchronisation points.
"""

import json
import math
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from saipet.jsonio import (
    InterProcessLock,
    StateFileError,
    atomic_write_json,
    read_json,
)
from saipet.review import (
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    ReviewItem,
)
from saipet.sources.base import Candidate

_FORMAT_VERSION = 2

_KNOWN_STATUSES = frozenset({STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED})
_KNOWN_BANDS = frozenset({"ignore", "review", "priority"})


class TransitionConflict(Exception):
    """The durable item moved on before this transition landed.

    The interprocess lock serialises writes; it cannot stop a stale WRITER.
    Carrying the authoritative status lets the caller refresh instead of
    retrying blind."""

    def __init__(self, identity: tuple[str, str], durable_status, attempted_status):
        self.identity = identity
        self.durable_status = durable_status
        self.attempted_status = attempted_status
        source, candidate_id = identity
        super().__init__(
            f"transition conflict for ({source!r}, {candidate_id!r}): "
            f"durable status is {durable_status!r}, expected {attempted_status!r} -- "
            "another process decided first"
        )


def _serialize(item: ReviewItem) -> dict:
    c = item.candidate
    return {
        "candidate": {
            "source": c.source,
            "id": c.id,
            "title": c.title,
            "body": c.body,
            "permalink": c.permalink,
            "subreddit": c.subreddit,
            "created_utc": c.created_utc,
        },
        "relevance_score": item.relevance_score,
        "band": item.band,
        "draft": item.draft,
        "status": item.status,
        "signals": dict(item.signals),
        "discovered": item.discovered,
    }


def _deserialize(raw: dict) -> ReviewItem:
    c_raw = raw["candidate"]
    candidate = Candidate(
        source=c_raw["source"],
        id=c_raw["id"],
        title=c_raw.get("title", ""),
        body=c_raw.get("body", ""),
        permalink=c_raw.get("permalink", ""),
        subreddit=c_raw.get("subreddit", ""),
        created_utc=c_raw.get("created_utc", 0.0),
    )
    return ReviewItem(
        candidate=candidate,
        relevance_score=raw["relevance_score"],
        band=raw.get("band", "review"),
        draft=raw.get("draft", ""),
        status=raw.get("status", "pending"),
        signals=dict(raw.get("signals", {})),
        discovered=raw.get("discovered", 0.0),
    )


def _finite_number(value, field: str, path) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StateFileError(f"{path} is corrupt: {field} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise StateFileError(f"{path} is corrupt: {field} must be finite")
    return number


def _validate_item_entry(entry, path):
    """Strict domain validation for one serialized item (CORE-008)."""
    if not isinstance(entry, dict):
        raise StateFileError(f"{path} is corrupt: item entry is not an object")
    status = entry.get("status", STATUS_PENDING)
    if status not in _KNOWN_STATUSES:
        raise StateFileError(f"{path} is corrupt: unknown item status {status!r}")
    band = entry.get("band", "review")
    if band not in _KNOWN_BANDS:
        raise StateFileError(f"{path} is corrupt: unknown band {band!r}")
    _finite_number(entry.get("relevance_score"), "relevance_score", path)
    _finite_number(entry.get("discovered", 0.0), "discovered", path)
    c_raw = entry.get("candidate")
    if not isinstance(c_raw, dict):
        raise StateFileError(f"{path} is corrupt: 'candidate' must be an object")
    for field in ("source", "id"):
        value = c_raw.get(field)
        if not isinstance(value, str) or not value:
            raise StateFileError(
                f"{path} is corrupt: candidate.{field} must be a non-empty string"
            )
    for field in ("title", "body", "permalink", "subreddit"):
        if not isinstance(c_raw.get(field, ""), str):
            raise StateFileError(
                f"{path} is corrupt: candidate.{field} must be a string"
            )
    _finite_number(c_raw.get("created_utc", 0.0), "created_utc", path)
    signals = entry.get("signals", {})
    if not isinstance(signals, dict) or not all(
        isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))
        for v in signals.values()
    ):
        raise StateFileError(
            f"{path} is corrupt: signals must be an object of finite numbers"
        )
    return (c_raw["source"], c_raw["id"])


def _history_path_for(path: Path) -> Path:
    return path.with_name(path.name + ".history.jsonl")


def _index_path_for(path: Path) -> Path:
    return path.with_name(path.name + ".index.sqlite")


def _load_history_tail(path: Path, start_byte: int = 0):
    """Replay the cold journal from `start_byte` (PERF-001 byte checkpoint).

    Returns a list of `(identity, item, offset, length)` for every COMPLETE
    terminal record since the checkpoint. A torn trailing append (an
    interrupted write that never finished its line) is skipped, not treated as
    corruption (W2-003); any malformed INTERIOR line is hard corruption and
    raises StateFileError.
    """
    history_file = _history_path_for(path)
    if not history_file.exists():
        return []
    try:
        with history_file.open("rb") as f:
            f.seek(start_byte)
            raw = f.read().decode("utf-8", "replace")
    except OSError as exc:
        raise StateFileError(f"{history_file} is unreadable: {exc}") from exc
    lines = raw.split("\n")
    n = len(lines)
    result = []
    seen: set[tuple[str, str]] = set()
    offset = start_byte
    for i, line in enumerate(lines):
        span = len(line.encode("utf-8"))
        is_last = i == n - 1
        if not is_last:
            span += 1  # the newline byte
        if not is_last and line.strip() == "":
            offset += span
            continue
        if is_last:
            # A clean journal ends with a newline, leaving a final empty
            # element; a torn write leaves a non-empty final element.
            if line == "" or line.strip() == "":
                break
        try:
            record = json.loads(line)
            entry = record["item"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            if is_last:
                # W2-003: trailing partial write -- recover by skipping it.
                break
            raise StateFileError(f"{history_file} is corrupt: {exc}") from exc
        identity = _validate_item_entry(entry, history_file)
        # Replay is idempotent: the LAST occurrence of an identity wins.
        if identity in seen:
            result = [r for r in result if r[0] != identity]
        seen.add(identity)
        result.append((identity, _deserialize(entry), offset, span))
        offset += span
    return result


def _load_history(path: Path) -> list[ReviewItem]:
    """Full cold journal as items. An explicit-consumer read; nothing in the
    hot path calls this."""
    return [item for (_id, item, _o, _l) in _load_history_tail(path, 0)]


def _append_history(path: Path, item: ReviewItem) -> tuple[int, int]:
    """One O(1) journal line for a terminal transition.

    Called under the interprocess lock. The line is flushed AND fsync'd before
    returning so that the subsequent hot-file replace (which removes the item
    from the active pending set) can never be durably published before the
    terminal record it depends on (CORE-001). Returns the `(offset, length)`
    byte span of the written record for the terminal index.
    """
    history_file = _history_path_for(path)
    record = {"item": _serialize(item)}
    data = json.dumps(record, ensure_ascii=False) + "\n"
    with history_file.open("a", encoding="utf-8") as handle:
        offset = handle.tell()
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
        end = handle.tell()
    return offset, end - offset


def _truncate_torn_tail(path: Path) -> None:
    """Under the interprocess lock: if the journal ends with a torn (incomplete)
    trailing record, remove just that partial line so the next append is clean
    and the byte checkpoint stays consistent (W2-003)."""
    history_file = _history_path_for(path)
    if not history_file.exists():
        return
    try:
        with history_file.open("rb") as f:
            raw = f.read().decode("utf-8", "replace")
    except OSError:
        return
    lines = raw.split("\n")
    if not lines:
        return
    last = lines[-1]
    if last == "":
        return  # clean trailing newline
    try:
        json.loads(last)
        # Complete record but missing its trailing newline: repair it.
        with history_file.open("ab") as f:
            f.write(b"\n")
    except (json.JSONDecodeError, ValueError):
        # Torn partial: truncate back to the start of this final line.
        cut = 0
        for line in lines[:-1]:
            cut += len(line.encode("utf-8")) + 1
        try:
            with history_file.open("r+b") as f:
                f.truncate(cut)
        except OSError:
            pass


class _TerminalIndex:
    """SQLite-backed point-addressable terminal identity index (PERF-002).

    One row per terminal identity: its status plus the exact `(offset, length)`
    byte span of its journal record, so a single historical lookup is one SQL
    point query plus one journal seek -- never a whole-journal read.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # The GUI builds the store on the UI thread and drives it from its
            # single worker; `check_same_thread=False` allows that (the
            # controller's one-worker rule plus SQLite's own locking keep
            # access serialised), matching SeenStore's contract.
            self._db = sqlite3.connect(str(self.path), timeout=10, check_same_thread=False)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS identities ("
                "  source TEXT NOT NULL,"
                "  id TEXT NOT NULL,"
                "  status TEXT NOT NULL,"
                "  journal_offset INTEGER NOT NULL,"
                "  journal_length INTEGER NOT NULL,"
                "  PRIMARY KEY (source, id))"
            )
            self._db.commit()
        except sqlite3.DatabaseError as exc:
            raise StateFileError(f"{self.path} is corrupt: {exc}") from exc

    def get(self, source: str, candidate_id: str):
        row = self._db.execute(
            "SELECT status, journal_offset, journal_length "
            "FROM identities WHERE source = ? AND id = ?",
            (source, candidate_id),
        ).fetchone()
        if row is None:
            return None
        return {"status": row[0], "offset": row[1], "length": row[2]}

    def set(self, source: str, candidate_id: str, status: str, offset: int, length: int) -> None:
        self._db.execute(
            "INSERT INTO identities (source, id, status, journal_offset, journal_length) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(source, id) DO UPDATE SET "
            "  status = excluded.status,"
            "  journal_offset = excluded.journal_offset,"
            "  journal_length = excluded.journal_length",
            (source, candidate_id, status, offset, length),
        )
        self._db.commit()

    def contains(self, source: str, candidate_id: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM identities WHERE source = ? AND id = ?",
            (source, candidate_id),
        ).fetchone()
        return row is not None

    def find_by_id(self, candidate_id: str) -> list[dict]:
        rows = self._db.execute(
            "SELECT source, id, status, journal_offset, journal_length "
            "FROM identities WHERE id = ?",
            (candidate_id,),
        ).fetchall()
        return [
            {"source": r[0], "id": r[1], "status": r[2], "offset": r[3], "length": r[4]}
            for r in rows
        ]

    def all(self) -> list[dict]:
        rows = self._db.execute(
            "SELECT source, id, status, journal_offset, journal_length FROM identities"
        ).fetchall()
        return [
            {"source": r[0], "id": r[1], "status": r[2], "offset": r[3], "length": r[4]}
            for r in rows
        ]

    def close(self) -> None:
        try:
            self._db.close()
        except sqlite3.Error:
            pass


@dataclass
class ReviewStore:
    """The durable repository behind a review workflow."""

    path: str | Path

    def __post_init__(self):
        self._path = Path(self.path)
        # W2-007: a legacy version-1 store is migrated in place, under the
        # interprocess lock, before any normal load -- so the hot path never
        # re-runs an interrupted migration.
        self._migrate_v1()
        self._index_db = _TerminalIndex(_index_path_for(self._path))
        self._pending, self._checkpoint = self._load_hot()

    def _migrate_v1(self) -> None:
        """Convert a legacy version-1 single-document store in place.

        Splits pending items (stay hot) from terminal items (move to the cold
        journal), appending only terminal records the journal does not already
        carry, then atomically rewrites the hot file as version 2 so migration
        completion is itself durable. Runs under the interprocess lock so two
        first-opening processes cannot race the same migration.
        """
        data = read_json(self._path)
        if not isinstance(data, dict) or data.get("version") != 1:
            return
        with InterProcessLock(self._path):
            data = read_json(self._path)
            if not isinstance(data, dict) or data.get("version") != 1:
                return  # another process migrated first
            raw_items = data.get("items")
            if not isinstance(raw_items, list):
                raise StateFileError(f"{self._path} is corrupt: 'items' must be a list")
            history_file = _history_path_for(self._path)
            history_items = _load_history(self._path) if history_file.exists() else []
            already_terminal = {i.identity() for i in history_items}
            pending: list[ReviewItem] = []
            migrated: list[ReviewItem] = []
            identities: set[tuple[str, str]] = set()
            for entry in raw_items:
                identity = _validate_item_entry(entry, self._path)
                if identity in identities:
                    raise StateFileError(
                        f"{self._path} is corrupt: duplicate canonical identity {identity}"
                    )
                identities.add(identity)
                item = _deserialize(entry)
                if item.status == STATUS_PENDING:
                    pending.append(item)
                elif identity not in already_terminal:
                    migrated.append(item)
            for item in migrated:
                _append_history(self._path, item)
            # The terminal index is a separate SQLite file created AFTER
            # migration (in __post_init__), so it starts empty. Reopening must
            # replay the whole journal into it: checkpoint 0 forces a full
            # resync on the first load after migration (idempotent), then
            # normal transitions advance the checkpoint to the journal size.
            atomic_write_json(
                self._path,
                {
                    "version": _FORMAT_VERSION,
                    "pending": [_serialize(i) for i in pending],
                    "journal_checkpoint": 0,
                },
            )

    def _read_journal_item(self, offset: int, length: int) -> ReviewItem:
        history_file = _history_path_for(self._path)
        with history_file.open("rb") as f:
            f.seek(offset)
            raw = f.read(length).decode("utf-8")
        record = json.loads(raw)
        return _deserialize(record["item"])

    def _resync_index(self, checkpoint: int) -> set[tuple[str, str]]:
        """Replay the journal tail since `checkpoint` into the terminal index.

        Idempotent by identity: reopening converges the index to the journal's
        authoritative terminal decisions. Returns the set of identities the
        journal itself marked terminal (used to reconcile a crash where the
        hot pending list was not yet updated).
        """
        journal_terminal: set[tuple[str, str]] = set()
        for identity, item, offset, length in _load_history_tail(self._path, checkpoint):
            self._index_db.set(identity[0], identity[1], item.status, offset, length)
            journal_terminal.add(identity)
        return journal_terminal

    def _load_hot(self) -> tuple[list[ReviewItem], int]:
        data = read_json(self._path)
        if data is None:
            return [], 0
        if not isinstance(data, dict):
            raise StateFileError(f"{self._path} is corrupt: expected an object")
        version = data.get("version")

        if version == _FORMAT_VERSION:
            raw_pending = data.get("pending", [])
            if not isinstance(raw_pending, list):
                raise StateFileError(f"{self._path} is corrupt: 'pending' must be a list")
            pending: list[ReviewItem] = []
            pending_ids: set[tuple[str, str]] = set()
            for entry in raw_pending:
                identity = _validate_item_entry(entry, self._path)
                if entry.get("status", STATUS_PENDING) != STATUS_PENDING:
                    raise StateFileError(
                        f"{self._path} is corrupt: hot pending entries must be pending"
                    )
                if identity in pending_ids:
                    raise StateFileError(
                        f"{self._path} is corrupt: duplicate canonical identity {identity}"
                    )
                pending_ids.add(identity)
                pending.append(_deserialize(entry))
            # PERF-001: checkpoint is a byte offset into the journal, not a
            # line count. Validate its type/domain strictly (CORE-008).
            checkpoint = data.get("journal_checkpoint", 0)
            if isinstance(checkpoint, bool) or not isinstance(checkpoint, int) or checkpoint < 0:
                raise StateFileError(
                    f"{self._path} is corrupt: journal_checkpoint must be a "
                    f"non-negative integer"
                )
            history_file = _history_path_for(self._path)
            if history_file.exists():
                size = history_file.stat().st_size
                if checkpoint > size:
                    raise StateFileError(
                        f"{self._path} is corrupt: journal_checkpoint "
                        f"({checkpoint}) beyond journal size ({size})"
                    )
            journal_terminal = self._resync_index(checkpoint)
            # A terminal journal record is authoritative: reconcile a crash
            # where the hot pending list still carried the item.
            pending = [
                it for it in pending
                if it.identity() not in journal_terminal
            ]
            # CORE-008: after reconciliation, a pending identity that is
            # STILL terminal in the index (i.e. not produced by the journal
            # replay) is a contradictory hot file and must not be published.
            for item in pending:
                if self._index_db.contains(item.candidate.source, item.candidate.id):
                    raise StateFileError(
                        f"{self._path} is corrupt: identity {item.identity()} "
                        "is both pending and terminal"
                    )
            return pending, checkpoint

        if version == 1:
            # Migrated in `_migrate_v1` before any normal load. Reaching here
            # means that migration did not run -- a programming error.
            raise StateFileError(
                f"{self._path} is corrupt: version-1 store reached the hot "
                "loader without migration"
            )

        raise StateFileError(
            f"{self._path} is corrupt: unsupported format version {version!r} "
            f"(this build reads {_FORMAT_VERSION})"
        )

    def _durable_status(self, source: str, candidate_id: str, pending=None) -> str | None:
        if pending is None:
            pending = self._pending
        for item in pending:
            if item.candidate.source == source and item.candidate.id == candidate_id:
                return STATUS_PENDING
        row = self._index_db.get(source, candidate_id)
        return row["status"] if row is not None else None

    def _publish(self, pending: list[ReviewItem]) -> None:
        self._pending = pending

    def _save_hot(self, pending: list[ReviewItem], checkpoint: int) -> None:
        # PERF-001: the checkpoint is the authoritative processed byte offset.
        # Recompute it from the journal's real size so a committed record can
        # never be replayed twice and an uncommitted one is never skipped.
        history_file = _history_path_for(self._path)
        checkpoint = history_file.stat().st_size if history_file.exists() else 0
        atomic_write_json(
            self._path,
            {
                "version": _FORMAT_VERSION,
                "pending": [_serialize(i) for i in pending],
                "journal_checkpoint": checkpoint,
            },
        )
        self._checkpoint = checkpoint
        self._pending = pending

    # -- read ----------------------------------------------------------

    def items(self) -> list[ReviewItem]:
        """EVERYTHING known: live pending plus full terminal history.

        Explicit-history consumer only (PERF-002): terminal bodies are
        fetched by point seek from the index, not by streaming the whole
        journal into memory at once. Bridge/GUI startup and queue operations
        use `pending()` / `find()`."""
        merged: dict[tuple[str, str], ReviewItem] = {}
        for row in self._index_db.all():
            item = self._read_journal_item(row["offset"], row["length"])
            merged[item.identity()] = item
        for item in self._pending:
            merged[item.identity()] = item
        return list(merged.values())

    def pending(self) -> list[ReviewItem]:
        return list(self._pending)

    def find(self, source: str, candidate_id: str) -> ReviewItem | None:
        for item in self._pending:
            if item.candidate.source == source and item.candidate.id == candidate_id:
                return item
        row = self._index_db.get(source, candidate_id)
        if row is None:
            return None
        return self._read_journal_item(row["offset"], row["length"])

    def find_by_id(self, candidate_id: str) -> list[ReviewItem]:
        found: list[ReviewItem] = []
        for item in self._pending:
            if item.candidate.id == candidate_id:
                found.append(item)
        for row in self._index_db.find_by_id(candidate_id):
            found.append(self._read_journal_item(row["offset"], row["length"]))
        return found

    def contains(self, source: str, candidate_id: str) -> bool:
        for item in self._pending:
            if item.candidate.source == source and item.candidate.id == candidate_id:
                return True
        return self._index_db.contains(source, candidate_id)

    def refresh(self) -> None:
        """W2-002: re-read durable state so a long-running process sees
        findings another process committed. Reloads the hot pending list and
        resyncs the terminal index from the journal checkpoint."""
        with InterProcessLock(self._path):
            self._pending, self._checkpoint = self._load_hot()

    # -- write ---------------------------------------------------------

    def commit_scouted(self, new_items: list[ReviewItem]) -> None:
        """Merge new pending items into the durable store, persist once.

        Runs under the inter-process lock so a concurrently committed
        transition from another process is not lost. A stale/concurrent scout
        writer must never move an identity the store has already APPROVED or
        REJECTED backward to PENDING -- so this merge consults the durable
        terminal index before adding (compare-and-set discipline).
        """
        if not new_items:
            return
        with InterProcessLock(self._path):
            pending, _checkpoint = self._load_hot()
            existing = {(i.candidate.source, i.candidate.id): i for i in pending}
            for item in new_items:
                key = item.identity()
                durable_status = self._durable_status(key[0], key[1], pending)
                if durable_status in (STATUS_APPROVED, STATUS_REJECTED):
                    # Already decided: leave it unchanged rather than
                    # resurrect it as pending.
                    continue
                if key in existing:
                    existing[key] = item
                else:
                    pending.append(item)
                    existing[key] = item
            self._save_hot(list(existing.values()), _checkpoint)

    def commit_transition(
        self, item: ReviewItem, expected_status: str = STATUS_PENDING
    ) -> ReviewItem:
        """Persist one item's state change (status/draft). Returns the
        authoritative item reference on success.

        Runs under the lock: the hot file is reloaded, the item located by
        (source,id) identity, and the durable predecessor must still carry
        `expected_status`. On mismatch this store refreshes to the
        authoritative state and raises TransitionConflict.

        PERF-002: the terminal outcome appends one journal line (fsync'd,
        CORE-001) and the hot file shrinks by one pending item -- no rewrite
        of lifetime history. The terminal index is a single point upsert.
        """
        with InterProcessLock(self._path):
            # Recover any torn trailing append before we add our own record
            # (W2-003), so the byte checkpoint stays consistent.
            _truncate_torn_tail(self._path)
            pending, _checkpoint = self._load_hot()
            key = item.identity()
            durable_status = self._durable_status(key[0], key[1], pending)
            if durable_status != expected_status:
                self._publish(pending)
                raise TransitionConflict(key, durable_status, expected_status)

            if item.status == STATUS_PENDING:
                replaced = False
                for idx, existing_item in enumerate(pending):
                    if existing_item.identity() == key:
                        pending[idx] = item
                        replaced = True
                        break
                if not replaced:
                    pending.append(item)
                self._save_hot(pending, _checkpoint)
                return item

            # Terminal: journal first (O(1), fsync'd), then shrink the hot
            # file. The index gets one point upsert.
            offset, length = _append_history(self._path, item)
            self._index_db.set(key[0], key[1], item.status, offset, length)
            pending = [i for i in pending if i.identity() != key]
            self._save_hot(pending, _checkpoint)
            return item

    def close(self) -> None:
        self._index_db.close()
