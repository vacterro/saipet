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
"""

import json
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_NOTIFICATION_FILE = "notifications.jsonl"
DEFAULT_INBOX_FILE = "inbox.md"


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
    """Build the notification for one queued item (bridge `queue` shape)."""
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
    """Proof of life. 'Alive and found nothing' and 'died at 03:00' produce
    identical silence otherwise, and only one of them is fine."""
    return Notification(
        kind="heartbeat",
        text=f"cycle {cycle}: {queued} new candidate(s)",
        at=now,
        data={"cycle": cycle, "queued": queued},
    )


def error(message: str, cycle: int, now: float) -> Notification:
    return Notification(
        kind="error",
        text=f"cycle {cycle} failed: {message}",
        at=now,
        data={"cycle": cycle, "error": message},
    )


class Notifier:
    """Sink interface. `send` must not raise: a delivery failure has to
    degrade the monitor, never stop it."""

    def send(self, notification: Notification) -> None:
        raise NotImplementedError


class NullNotifier(Notifier):
    """Drops everything. For tests and for a run the caller drives itself."""

    def send(self, notification: Notification) -> None:
        return


class ConsoleNotifier(Notifier):
    """One line per notification, for whoever is reading this process."""

    def __init__(self, print_fn=print, prefix: str = "NOTIFY"):
        self._print = print_fn
        self._prefix = prefix

    def send(self, notification: Notification) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(notification.at))
        self._print(f"{self._prefix} {stamp} {notification.kind}: {notification.text}")


class FileNotifier(Notifier):
    """Appends to a JSONL feed and a human-readable inbox."""

    def __init__(
        self,
        path: str | Path = DEFAULT_NOTIFICATION_FILE,
        inbox_path: str | Path | None = None,
    ):
        self.path = Path(path)
        self.inbox_path = (
            Path(inbox_path) if inbox_path is not None else self.path.with_name(DEFAULT_INBOX_FILE)
        )

    def send(self, notification: Notification) -> None:
        for target in (self.path, self.inbox_path):
            target.parent.mkdir(parents=True, exist_ok=True)

        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(notification.as_dict(), ensure_ascii=False) + "\n")

        stamp = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(notification.at))
        line = f"- `{stamp}` **{notification.kind}** -- {notification.text}"
        permalink = notification.data.get("permalink")
        if permalink:
            line += f" <{permalink}>"
        with self.inbox_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


class MultiNotifier(Notifier):
    """Fans one notification out to several sinks.

    One dead sink must not cost the others their message, so a failing
    `send` is collected and reported rather than raised -- the monitor's
    job is to keep running.
    """

    def __init__(self, *sinks: Notifier):
        self.sinks = list(sinks)
        self.failures: list[str] = []

    def send(self, notification: Notification) -> None:
        for sink in self.sinks:
            try:
                sink.send(notification)
            except Exception as exc:  # noqa: BLE001 -- a sink may fail any way it likes
                self.failures.append(f"{type(sink).__name__}: {type(exc).__name__}: {exc}")
