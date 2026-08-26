"""Where a finding goes when nobody is watching the terminal.

The monitor's whole point is that it runs unattended, so "print it and
hope" is not a delivery mechanism. Two sinks ship:

- `ConsoleNotifier` -- one line to stdout, which is what a supervising
  agent reading this process's output actually consumes;
- `FileNotifier` -- appends `notifications.jsonl` (machine) and `inbox.md`
  (human). Appends, never rewrites: an inbox that overwrites itself is
  worse than no inbox, because it looks full while losing everything.

**No shell call and no HTTP here, deliberately.** A desktop toast on
Windows would mean handing a notification body to another program -- and
that body carries a Reddit title, i.e. text a stranger wrote. That is
precisely the path tests/test_no_autopost.py exists to keep out of this
package. A webhook sink is a reasonable thing to want, but it is an
outbound network write and belongs to a ticket where the user names the
endpoint, not to a default.

Every `send()` returns a `Delivery` so the caller can count only
successful deliveries and surface sink failures (CORE-007).
"""

import hashlib
import json
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import sqlite3

DEFAULT_NOTIFICATION_FILE = "notifications.jsonl"
DEFAULT_INBOX_FILE = "inbox.md"

# W2-007: one shared implementation of the text-hygiene rules (textutil).
# The underscore-prefixed names below are kept as aliases because tests and
# sibling modules import them from here.
from saipet.textutil import markdown_escape as _escape_markdown  # noqa: E402
from saipet.textutil import safe_link_destination as _validate_permalink  # noqa: E402
from saipet.textutil import strip_untrusted as _strip_untrusted  # noqa: E402


@dataclass(frozen=True)
class Delivery:
    """The outcome of one notification delivery to one or more sinks.

    `delivered` is True only when no sink failed. A partial multi-sink
    failure (some sinks succeeded, some failed) is reported as not delivered
    and the caller decides how to count it.
    """
    delivered: bool
    failures: tuple = ()  # (sink_name, reason) pairs


@dataclass(frozen=True)
class Notification:
    """One thing worth telling the user about."""

    kind: str  # "finding" | "heartbeat" | "error"
    text: str
    at: float
    data: dict

    def as_dict(self) -> dict:
        return {"kind": self.kind, "text": self.text, "at": self.at, "data": self.data}


def finding(item_view: dict, now: float) -> Notification:
    return Notification(
        kind="finding",
        text=(
            f"[{item_view.get('band', '?')}] {item_view.get('score', 0):.0f} "
            f"r/{item_view.get('subreddit') or '?'} -- {item_view.get('title', '')}"
        ),
        at=now,
        data=dict(item_view),
    )


def heartbeat(cycle: int, queued: int, now: float) -> Notification:
    return Notification(
        kind="heartbeat",
        text=f"cycle {cycle}: {queued} new candidate(s)",
        at=now,
        data={"cycle": cycle, "queued": queued},
    )


def degraded(detail: str, cycle: int, now: float) -> Notification:
    return Notification(
        kind="warning",
        text=f"cycle {cycle} degraded: {detail}",
        at=now,
        data={"cycle": cycle, "detail": detail},
    )


def error(message: str, cycle: int, now: float) -> Notification:
    return Notification(
        kind="error",
        text=f"cycle {cycle} failed: {message}",
        at=now,
        data={"cycle": cycle, "error": message},
    )


class Notifier:
    """Sink interface. `send` returns a `Delivery` summarising downstream
    success. Raise to signal a hard failure; the caller may still catch it."""

    def send(self, notification: Notification) -> Delivery:
        raise NotImplementedError


class NullNotifier(Notifier):
    """Drops everything. For tests and for a run the caller drives itself."""

    def send(self, notification: Notification) -> Delivery:
        return Delivery(delivered=True)


class ConsoleNotifier(Notifier):
    """One line per notification, for whoever is reading this process."""

    def __init__(self, print_fn=print, prefix: str = "NOTIFY"):
        self._print = print_fn
        self._prefix = prefix

    def send(self, notification: Notification) -> Delivery:
        stamp = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(notification.at))
        line = f"{self._prefix} {stamp} {notification.kind}: {_strip_untrusted(notification.text)}"
        try:
            self._print(line)
            return Delivery(delivered=True)
        except Exception as exc:
            return Delivery(delivered=False, failures=(("ConsoleNotifier", str(exc)),))


class FileNotifier(Notifier):
    """Appends to a JSONL feed and a human-readable inbox.

    W2-003: the two appends are independent writes, so they are tracked
    independently. A stable per-notification identity plus a tiny
    completion-state sidecar (`<feed>.delivery.json`) means a failure of the
    SECOND destination no longer masquerades as total failure after the
    first one durably landed -- and a retry re-sends only to the
    destination that actually missed it, never duplicating the other.
    """

    def __init__(
        self,
        path: str | Path = DEFAULT_NOTIFICATION_FILE,
        inbox_path: str | Path | None = None,
    ):
        self.path = Path(path)
        self.inbox_path = (
            Path(inbox_path) if inbox_path is not None else self.path.with_name(DEFAULT_INBOX_FILE)
        )
        # PERF-002: completion state is a point-addressable SQLite store keyed
        # by notification identity, so membership and per-destination completion
        # are O(1) point lookups/updates that never load or rewrite all
        # historical identities. The connection is created lazily and no
        # historical state is materialised into resident Python on startup.
        self._state_path = self.path.with_name(self.path.stem + ".delivery.sqlite")
        self._db = None

    def _db_conn(self):
        if self._db is None:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(self._state_path), timeout=10)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS delivery ("
                "  identity TEXT PRIMARY KEY,"
                "  jsonl_done INTEGER NOT NULL DEFAULT 0,"
                "  inbox_done INTEGER NOT NULL DEFAULT 0"
                ")"
            )
            self._db.commit()
        return self._db

    @staticmethod
    def _identity(notification: Notification) -> str:
        # W2-008: the key must be stable across retries. The retry wall clock
        # (`at`) is deliberately excluded -- the monitor already reconstructs
        # each obligation with its original `notification_at`, so `at` is the
        # same on every retry; excluding it keeps a stray caller that passes a
        # fresh `now` from forking the identity and duplicating a delivered sink.
        # The canonical finding identity rides in `data`.
        payload = (
            f"{notification.kind}|{notification.text}|"
            f"{notification.data.get('permalink', '')}|"
            f"{notification.data.get('source', '')}|{notification.data.get('id', '')}"
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]

    def _is_done(self, identity: str, column: str) -> bool:
        row = self._db_conn().execute(
            f"SELECT {column} FROM delivery WHERE identity=?", (identity,)
        ).fetchone()
        return bool(row and row[0])

    def _mark_done(self, identity: str, column: str) -> None:
        db = self._db_conn()
        db.execute(
            "INSERT INTO delivery (identity, jsonl_done, inbox_done) VALUES (?, ?, ?) "
            "ON CONFLICT(identity) DO UPDATE SET "
            "jsonl_done = jsonl_done OR excluded.jsonl_done, "
            "inbox_done = inbox_done OR excluded.inbox_done",
            (
                identity,
                1 if column == "jsonl" else 0,
                1 if column == "inbox" else 0,
            ),
        )
        db.commit()

    def send(self, notification: Notification) -> Delivery:
        identity = self._identity(notification)
        failures: list[tuple[str, str]] = []

        if not self._is_done(identity, "jsonl_done"):
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(notification.as_dict(), ensure_ascii=False) + "\n"
                    )
                self._mark_done(identity, "jsonl")
            except Exception as exc:
                failures.append((f"{type(self).__name__}.jsonl", str(exc)))

        if not self._is_done(identity, "inbox_done"):
            try:
                self.inbox_path.parent.mkdir(parents=True, exist_ok=True)
                stamp = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(notification.at))
                line = f"- `{stamp}` **{_escape_markdown(notification.kind)}** -- {_escape_markdown(notification.text)}"
                permalink = _validate_permalink(notification.data.get("permalink"))
                if permalink:
                    line += f" <{permalink}>"
                with self.inbox_path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                self._mark_done(identity, "inbox")
            except Exception as exc:
                failures.append((f"{type(self).__name__}.inbox", str(exc)))

        if failures:
            return Delivery(delivered=False, failures=tuple(failures))
        return Delivery(delivered=True)


class MultiNotifier(Notifier):
    """Fans one notification out to several sinks.

    One dead sink must not cost the others their message, so a failing
    `send` is collected and reported rather than raised -- the monitor's
    job is to keep running. Diagnostics are bounded (PERF-003): only the
    most recent failures are kept, and a lifetime failure counter tracks
    the total.
    """

    def __init__(self, *sinks: Notifier, max_recorded_failures: int = 20):
        self.sinks = list(sinks)
        self.failure_total = 0
        self._latest_failures: deque[tuple[str, str]] = deque(maxlen=max_recorded_failures)

    @property
    def latest_failures(self) -> list[tuple[str, str]]:
        return list(self._latest_failures)

    @property
    def failures(self) -> list[str]:
        return [f"{name}: {reason}" for name, reason in self._latest_failures]

    def send(self, notification: Notification) -> Delivery:
        collected: list[tuple[str, str]] = []
        for sink in self.sinks:
            try:
                result = sink.send(notification)
                if not result.delivered:
                    collected.extend(result.failures)
            except Exception as exc:  # noqa: BLE001 -- a sink may fail any way it likes
                collected.append((type(sink).__name__, f"{type(exc).__name__}: {exc}"))
        if collected:
            self.failure_total += len(collected)
            self._latest_failures.extend(collected)
        return Delivery(delivered=not collected, failures=tuple(collected))
