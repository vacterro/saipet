"""The surface an external agent drives SAIPET through.

`cli.main()` is a script, not an API: it parses argv, prints, and prompts.
An agent driving it would have to import argparse internals or scrape
stdout, and both break the moment the CLI's wording changes. This module is
the stable seam instead -- one `dispatch(verb, **args)`, structured results,
no printing.

**The verb set is closed, and that is the security property, not a
convenience.** Everything this bridge acts on originates on the public
internet: a Reddit title is attacker-controlled text. A dispatcher that
resolved verbs dynamically -- `getattr(self, verb)`, a dynamic evaluation,
a shell call -- would turn "fetched some posts" into "ran what a stranger
wrote". An unknown verb is refused and never executed, and the mapping
below is the whole list of things that can happen.

(The words this paragraph avoids spelling out are the literal tokens
tests/test_no_autopost.py greps for across this package. Naming them even
in prose fails that scan -- which is the scan doing its job, so the prose
works around it rather than the other way round.)

No verb posts anything. There is no write endpoint anywhere in `saipet/`
(tests/test_no_autopost.py scans for it); `approve` flips a status flag and
hands back text for a human to paste, exactly as the interactive loop does.

Every verb validates its arguments against a per-verb schema before the
handler runs, so a malformed value is refused with a stable error instead of
being silently coerced into a different meaning.
"""

import inspect
import math
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from saipet import config
from saipet.cli import DEFAULT_LIMIT, build_source, scout
from saipet.draft import build_draft
from saipet.notify import Notifier, NullNotifier
from saipet.report import DEFAULT_REPORT_DIR, list_complete_reports, write_report
from saipet.review import (
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    ReviewQueue,
)
from saipet.signals import extract_signals
from saipet.jsonio import StateFileError, pid_alive, read_json
from saipet.store import SeenStore
from saipet.review_store import ReviewStore, TransitionConflict


@dataclass(frozen=True)
class Result:
    """One dispatch outcome. `ok=False` means nothing was executed."""

    ok: bool
    verb: str
    data: dict = field(default_factory=dict)
    error: str = ""


class TransitionResultError(Exception):
    """Bridge-level wrapper for compare-and-set refusals. Carries the
    structured conflict payload so `dispatch()` can render it as an
    `ok=False` Result with authoritative status/identity for callers."""

    def __init__(self, exc: "TransitionConflict"):
        self.exc = exc
        super().__init__(str(exc))


def _item_view(item) -> dict:
    candidate = item.candidate
    return {
        "id": candidate.id,
        "source": candidate.source,
        "subreddit": candidate.subreddit,
        "title": candidate.title,
        "permalink": candidate.permalink,
        "score": item.relevance_score,
        "band": item.band,
        "status": item.status,
    }


# -- per-verb argument schemas (CORE-009) --------------------------------

_STR = "str"
_INT = "int"
_FLOAT = "float"
_LIST = "list"
_BOOL = "bool"

_SCHEMAS: dict[str, dict] = {
    "scout": {
        "subreddits": {"type": _LIST, "required": False},
        "limit": {"type": _INT, "min": 1, "required": False},
        "since_hours": {"type": _FLOAT, "min": 0, "required": False},
    },
    "report": {},
    "status": {},
    "queue": {},
    "approve": {
        "id": {"type": _STR, "nonempty": True, "required": True},
        "source": {"type": _STR, "required": False},
        "solution": {"type": _STR, "nonempty": True, "required": True},
        "mention_saipen": {"type": _BOOL, "required": False},
    },
    "inspect": {
        "id": {"type": _STR, "nonempty": True, "required": True},
        "source": {"type": _STR, "required": False},
    },
    "reject": {
        "id": {"type": _STR, "nonempty": True, "required": True},
        "source": {"type": _STR, "required": False},
    },
    "watch": {
        "cycles": {"type": _INT, "min": 1, "required": False},
        "interval_seconds": {"type": _FLOAT, "min": 0, "required": False},
        "min_score": {"type": _FLOAT, "min": 0, "max": 100, "required": False},
        "subreddits": {"type": _LIST, "required": False},
        "limit": {"type": _INT, "min": 1, "required": False},
        "since_hours": {"type": _FLOAT, "min": 0, "required": False},
    },
}


def _validate_args(verb: str, args: dict) -> str | None:
    """Return an error string for invalid arguments, else None."""
    schema = _SCHEMAS.get(verb, {})
    for key in args:
        if key not in schema:
            return f"unknown argument {key!r} for {verb!r}"
    for key, rules in schema.items():
        if key not in args or args[key] is None:
            continue
        value = args[key]
        kind = rules["type"]
        if kind == _STR:
            if not isinstance(value, str):
                return f"{key} must be a string"
            if rules.get("nonempty") and not value.strip():
                return f"{key} must not be empty"
        elif kind == _INT:
            if isinstance(value, bool) or not isinstance(value, int):
                return f"{key} must be an integer"
            if "min" in rules and value < rules["min"]:
                return f"{key} must be at least {rules['min']}"
            if "max" in rules and value > rules["max"]:
                return f"{key} must be at most {rules['max']}"
        elif kind == _FLOAT:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return f"{key} must be a number"
            numeric = float(value)
            if not math.isfinite(numeric):
                return f"{key} must be finite"
            if "min" in rules and numeric < rules["min"]:
                return f"{key} must be at least {rules['min']}"
            if "max" in rules and numeric > rules["max"]:
                return f"{key} must be at most {rules['max']}"
        elif kind == _LIST:
            if not isinstance(value, list):
                return f"{key} must be a list"
            if any(not isinstance(i, str) or not i.strip() for i in value):
                return f"{key} must be a list of non-empty strings"
        elif kind == _BOOL:
            if not isinstance(value, bool):
                return f"{key} must be a boolean"
    return None


class _ReviewAwareSeen:
    """Answers 'already known' from both the seen store and the review store.

    The review store holds durable review state, so a candidate already in it
    (pending, approved or rejected) must never re-enter the queue even if the
    seen store has no record of it (e.g. a crash happened between a persisted
    queue and its seen marks). `mark_many` writes only the seen store.
    """

    def __init__(self, seen, review_store):
        self.seen = seen
        self.review = review_store

    def has(self, source: str, candidate_id: str) -> bool:
        return self.seen.has(source, candidate_id) or self.review.contains(source, candidate_id)

    def mark_many(self, pairs) -> None:
        self.seen.mark_many(pairs)

    def finalize_many(self, pairs) -> None:
        """W2-001: promote provisional seen claims to final after the review
        store's durable record landed. Delegates straight to the seen store."""
        self.seen.finalize_many(pairs)

    def claim(self, source: str, candidate_id: str) -> bool:
        """CORE-006 atomic admission; review state counts as already known."""
        if self.review.contains(source, candidate_id):
            return False
        return self.seen.claim(source, candidate_id)

    def release(self, source: str, candidate_id: str) -> None:
        self.seen.release(source, candidate_id)


class Bridge:
    """Holds one driving session: the last queue, the last report, the store.

    State lives here rather than in module globals so a caller can run two
    independent sessions (a test and a live one, say) without either
    seeing the other's queue.
    """

    def __init__(
        self,
        report_dir: str | Path = DEFAULT_REPORT_DIR,
        seen_path: str | Path = "seen.json",
        review_path: str | Path | None = None,
        source_factory=build_source,
        now_fn=time.time,
        notifier: Notifier | None = None,
        status_path: str | Path | None = None,
        seen_store: "SeenStore | None" = None,
        review_store: "ReviewStore | None" = None,
    ):
        self.report_dir = Path(report_dir)
        # PERF-006: accept already-constructed store instances so a caller
        # (the GUI) can classify and reuse one validated SeenStore/ReviewStore
        # instead of opening each twice at startup. Path-based defaults are
        # retained for all existing callers.
        self.seen = seen_store if seen_store is not None else SeenStore(seen_path)
        self._source_factory = source_factory
        self._now_fn = now_fn
        # Only the `watch` verb uses this; a caller that just dispatches
        # `scout` reads the result instead of being told about it.
        self._notifier = notifier if notifier is not None else NullNotifier()
        # Durable monitor status, if a daemon path was configured: lets a
        # fresh Bridge answer "is the daemon alive" from disk, not from this
        # process's private memory (W2-008).
        self._status_path = Path(status_path) if status_path is not None else None
        # Durable review state: when a `review_path` is supplied (or a
        # pre-built store is passed), the queue survives restarts and
        # approve/reject persist immediately. `None` keeps the purely in-memory
        # session for callers that do not need it.
        if review_store is not None:
            self._review_store = review_store
        else:
            self._review_store = ReviewStore(review_path) if review_path is not None else None
        self.queue = ReviewQueue()
        if self._review_store is not None:
            for item in self._review_store.pending():
                self.queue.add(item)
        self.last_report: dict = {}
        self.last_report_error: str = ""
        self.last_failures: list = []
        self.last_health: dict = {}
        # T-025: the unattended monitor suppresses report pairs for empty
        # cycles (96 cycles/day must not mean 192 files/day). The CLI and
        # bounded `watch` still write empty reports -- "ran and found nothing"
        # is a distinct fact there.
        self.skip_empty_reports = False
        # Filled by the `watch` verb; read by `status`. Kept here rather than
        # inside the loop so a driving agent can ask "is the monitor alive"
        # between calls.
        self.monitor_state = None
        # PERF-006: cooperative cancellation for the unattended loop. Set by
        # a GUI shutting down; honoured BETWEEN cycles, never mid-write, so
        # an in-flight atomic commit always finishes.
        self.cancel_event: threading.Event | None = None

    def _durable_monitor_status(self) -> dict:
        """The daemon's on-disk status record, or None. Stale records are
        labelled, never presented as live (W2-008).

        CORE-010: when no explicit path was configured, a fresh Bridge (the
        terminal engine, the GUI, a reconnecting agent) still reads the ONE
        canonical default status file the daemon writes -- production and
        consumption of that record are wired to the same path. W2-002: the
        record's `lifecycle` plus PID liveness decide `live`; a future
        `next_cycle_at` is never proof of life by itself.
        """
        path = self._status_path
        if path is None:
            from saipet.monitor import DEFAULT_STATUS_FILE  # lazy: import cycle

            candidate = Path(DEFAULT_STATUS_FILE)
            path = candidate if candidate.exists() else None
        if path is None or not Path(path).exists():
            return None
        try:
            record = read_json(Path(path))
        except StateFileError:
            return {"error": "corrupt status file"}
        if not isinstance(record, dict):
            return {"error": "malformed status file"}
        next_cycle = record.get("next_cycle_at")
        age = None
        if next_cycle is not None:
            age = max(0.0, self._now_fn() - float(next_cycle))
        record["stale"] = bool(age is not None and age > 600)
        lifecycle = record.get("lifecycle", "running")
        pid = record.get("pid")
        alive = isinstance(pid, int) and not isinstance(pid, bool) and pid_alive(pid)
        record["live"] = bool(alive and lifecycle == "running")
        return record

    # -- persistence ---------------------------------------------------

    def _persist_scouted(self, queue, source) -> None:
        """Write newly scouted pending items to the review store. Called by
        `scout` BEFORE it marks anything seen, so a crash between the two
        never leaves a seen-but-unreviewable candidate."""
        if self._review_store is None:
            return
        self._review_store.commit_scouted(queue.pending())

    def _persist_report(self, queue, source) -> None:
        """Write the run report. For a non-durable (no ReviewStore) session
        this is the durable pre-seen record: it must complete before the
        scout marks anything seen, so a crash between the two never loses a
        finding (W2-001). An empty cycle in the unattended monitor writes no
        report pair (T-025)."""
        self.last_failures = list(getattr(source, "last_failures", []))
        health = source.health
        if self.skip_empty_reports and not queue.pending():
            self.last_health = health.as_dict()
            return
        paths = write_report(
            queue.pending(),
            directory=self.report_dir,
            failures=self.last_failures,
            now_fn=self._now_fn,
        )
        self.last_report = {"jsonl": str(paths.jsonl), "markdown": str(paths.markdown)}
        self.last_health = health.as_dict()

    def _merge_scouted_into_queue(self, delta) -> None:
        """Add the new scout delta to the durable review worklist already in
        `self.queue`. Approved/rejected history loaded at init stays; only
        genuinely new pending items are added (PERF-002: no full rebuild)."""
        for item in delta.pending():
            if self.queue.find(item.candidate.source, item.candidate.id) is None:
                self.queue.add(item)

    # -- verbs ---------------------------------------------------------

    def _scout(
        self,
        subreddits: list[str] | None = None,
        limit: int = DEFAULT_LIMIT,
        since_hours: float | None = None,
    ) -> dict:
        targets = list(dict.fromkeys(subreddits)) if subreddits else sorted(config.SUBREDDIT_ALLOWLIST)
        source = self._source_factory(targets, config.SYMPTOMS)
        max_age = config.MAX_AGE_HOURS if since_hours is None else since_hours

        guard = self.seen
        persist = None
        if self._review_store is not None:
            guard = _ReviewAwareSeen(self.seen, self._review_store)
            persist = self._persist_scouted
        else:
            persist = self._persist_report  # report is the durable pre-seen record

        # W2-002: admit notify-worthy items to the outbox DURING the persist
        # phase (inside `scout()`), BEFORE the final seen commit. This means
        # a crash between the durable record landing and the seen finalize
        # leaves the candidate reconsiderable AND with a retryable
        # obligation, eliminating the "seen-but-un-notified" hole.
        # W2-008: capture the original notification creation time so retries
        # reuse the same identity (FileNotifier._identity includes `at`).
        outbox = getattr(self, "_monitor_outbox", None)
        outbox_min_score = getattr(self, "_monitor_min_score", config.NOTIFY_MIN_SCORE)
        if outbox is not None:
            orig_persist = persist
            def _persist_with_outbox(queue, source):
                if orig_persist is not None:
                    orig_persist(queue, source)
                now = self._now_fn()
                for item in queue.pending():
                    iv = _item_view(item)
                    if float(iv.get("score", 0.0)) >= outbox_min_score:
                        outbox.add(iv, notification_at=now)
            persist = _persist_with_outbox

        delta = scout(
            source,
            signal_fn=extract_signals,
            seen=guard,
            persist=persist,
            limit=limit,
            max_age_hours=max_age or None,
            now_fn=self._now_fn,
        )
        delta_pending = delta.pending()
        delta_views = [_item_view(item) for item in delta_pending]

        if self._review_store is not None:
            # The review store commit already happened inside scout (before
            # seen marks), so the finding is durable. W2-001: it must also be
            # VISIBLE in this process now -- merging happens before the
            # report artifact and survives its failure, because a stranded
            # pending finding is unreviewable until restart otherwise.
            self._merge_scouted_into_queue(delta)
            try:
                self._persist_report(delta, source)
                self.last_report_error = ""
            except Exception as exc:  # noqa: BLE001 -- durability is done; the artifact is not worth the queue
                self.last_report = {}
                self.last_report_error = f"{type(exc).__name__}: {exc}"
        else:
            # Non-durable session: the review worklist IS this run's delta.
            self.queue = delta

        health = source.health
        return {
            "source": source.name,
            "subreddits": targets,
            # CORE-004: the run delta, never the durable backlog.
            "queued": len(delta_pending),
            "items": delta_views,
            "health": health.state,
            "fetch": self.last_health,
            "failures": [{"subreddit": sub, "error": err} for sub, err in self.last_failures],
            "report": self.last_report,
            "report_error": self.last_report_error,
        }

    def _report(self) -> dict:
        """The last run's report, plus whatever complete report pairs are on
        disk.

        Listing the directory matters for a fresh process: an agent that
        reconnects has no `last_report` but the runs are still there.
        """
        known = sorted(str(p) for p in list_complete_reports(self.report_dir))
        return {"last": self.last_report, "available": known}

    def _status(self) -> dict:
        return {
            "queued": len(self.queue.pending()),
            "allowlist": sorted(config.SUBREDDIT_ALLOWLIST),
            "symptoms": list(config.SYMPTOMS),
            "max_age_hours": config.MAX_AGE_HOURS,
            "gate": {
                "ignore_below": config.GATE_IGNORE_BELOW,
                "prioritize_at": config.GATE_PRIORITIZE_AT,
            },
            "failures": [{"subreddit": sub, "error": err} for sub, err in self.last_failures],
            "fetch": self.last_health,
            "report": self.last_report,
            "monitor": self.monitor_state.as_dict() if self.monitor_state else None,
            "monitor_durable": self._durable_monitor_status(),
            "notify_min_score": config.NOTIFY_MIN_SCORE,
            "can_post": False,  # stated, not implied: there is no write path at all
        }

    def _watch(
        self,
        cycles: int = 1,
        interval_seconds: float = 0,
        min_score: float | None = None,
        subreddits: list[str] | None = None,
        limit: int = DEFAULT_LIMIT,
        since_hours: float | None = None,
    ) -> dict:
        """Run the monitor loop for a bounded number of cycles.

        **Bounded, always.** A dispatch that never returns hangs whoever
        called it -- for the terminal engine that means one typed line eats
        the whole process -- so `cycles` has no "forever" value here. The
        unattended forever-run is `python -m saipet.monitor`, which is a
        process the caller can actually stop.

        Imported lazily: `monitor` imports this module, so a module-level
        import would be a cycle.
        """
        from saipet.monitor import MonitorState, run_monitor

        if not isinstance(cycles, int) or isinstance(cycles, bool) or cycles < 1:
            raise ValueError("cycles must be an integer of at least 1")

        scout_args: dict = {"limit": limit}
        if subreddits:
            scout_args["subreddits"] = list(dict.fromkeys(subreddits))
        if since_hours is not None:
            scout_args["since_hours"] = since_hours

        if self.monitor_state is None:
            self.monitor_state = MonitorState()

        run_monitor(
            self,
            notifier=self._notifier,
            interval_seconds=interval_seconds,
            cycles=cycles,
            scout_args=scout_args,
            min_score=min_score,
            now_fn=self._now_fn,
            state=self.monitor_state,
            cancel_fn=(lambda: bool(self.cancel_event is not None and self.cancel_event.is_set())),
        )
        # CORE-011: return only the findings produced by THIS watch call, not
        # the cumulative lifetime history.
        this_run = list(self.monitor_state.last_run_findings)
        return {
            "monitor": self.monitor_state.as_dict(),
            "findings": this_run,
        }

    def _queue(self) -> dict:
        # W2-002: re-read durable state so a finding another process committed
        # is visible in this process's queue listing.
        if self._review_store is not None:
            self._review_store.refresh()
        items = sorted(self.queue.pending(), key=lambda i: -i.relevance_score)
        return {"items": [_item_view(item) for item in items]}

    def _resolve_item(self, candidate_id: str, source: str | None):
        # CORE-008: resolve across ALL applicable live and historical
        # candidates by canonical identity, deduplicating the same (source,id)
        # that may appear through more than one view (a live pending copy plus
        # a terminal journal record). One rule applies to the whole set:
        #   zero identities    -> not found
        #   one identity       -> return it
        #   more than one      -> ambiguous id, call for a source
        # A source-qualified lookup stays exact.
        # W2-002: re-read durable state so a finding another process committed
        # is visible here; PERF-002: a point query, never a full journal scan.
        if self._review_store is not None:
            self._review_store.refresh()

        if source is not None:
            item = self.queue.find(source, candidate_id)
            if item is None and self._review_store is not None:
                item = self._review_store.find(source, candidate_id)
            return item

        identities: dict[tuple[str, str], object] = {}
        for candidate in self.queue._items:
            if candidate.candidate.id == candidate_id:
                identities.setdefault(candidate.identity(), candidate)
        if self._review_store is not None:
            for stored in self._review_store.find_by_id(candidate_id):
                identities.setdefault(stored.identity(), stored)
        if not identities:
            return None
        if len(identities) > 1:
            raise KeyError(f"ambiguous id {candidate_id!r}: supply a source")
        return next(iter(identities.values()))

    def _approve(
        self,
        id: str,
        solution: str,
        mention_saipen: bool = True,
        source: str | None = None,
    ) -> dict:
        """Attach a human-written solution to one queued item and mark it.

        `solution` is required and never generated here: the whole point of
        the review queue is that a person wrote the answer. Approving still
        posts nothing -- it returns the text for a human to paste.
        """
        if not solution or not solution.strip():
            raise ValueError("solution must be a non-empty string written by a human")

        item = self._resolve_item(id, source)
        if item is None or item.status != STATUS_PENDING:
            raise KeyError(f"no pending item with id {id!r}")

        prospective = replace(
            item,
            draft=build_draft(solution, mention_saipen=mention_saipen),
            status=STATUS_APPROVED,
        )
        if self._review_store is not None:
            # CORE-003: persist the prospective state BEFORE publishing it
            # to the live queue. A failed write leaves both unchanged.
            try:
                self._review_store.commit_transition(prospective)
            except TransitionConflict as exc:
                self._refresh_queue_from_store()
                raise TransitionResultError(exc) from exc
            # PERF-005: drop the now-terminal item from the live queue; the
            # review store owns its historical record.
            self.queue.remove(item.candidate.source, item.candidate.id)
        else:
            item.draft = prospective.draft
            item.status = prospective.status
        return {"id": id, "draft": prospective.draft, "posted": False}

    def _inspect(self, id: str, source: str | None = None) -> dict:
        """The full, sanitized view of one candidate -- body and all.

        The queue listing stays deliberately compact (score, band, title:
        everything a scan needs). Body text and the signal breakdown are
        what a human actually reads before writing a solution, so a GUI
        calls this for the one row it is looking at. Read-only: nothing here
        changes review state or touches disk.
        """
        item = self._resolve_item(id, source)
        if item is None:
            raise KeyError(f"no item with id {id!r}")
        candidate = item.candidate
        return {
            "id": candidate.id,
            "source": candidate.source,
            "subreddit": candidate.subreddit,
            "title": candidate.title,
            "body": candidate.body,
            "permalink": candidate.permalink,
            "created_utc": candidate.created_utc,
            "score": item.relevance_score,
            "band": item.band,
            "status": item.status,
            "signals": dict(item.signals),
        }

    def _reject(self, id: str, source: str | None = None) -> dict:
        """Dismiss one pending item without touching anything remote.

        A review queue fills with noise; a GUI needs a way to say "this one
        is irrelevant" that is not approve. Rejection flips a local status
        flag and nothing else -- no posting, no deletion, no network.
        """
        item = self._resolve_item(id, source)
        if item is None or item.status != STATUS_PENDING:
            raise KeyError(f"no pending item with id {id!r}")

        prospective = replace(item, status=STATUS_REJECTED)
        if self._review_store is not None:
            try:
                self._review_store.commit_transition(prospective)
            except TransitionConflict as exc:
                self._refresh_queue_from_store()
                raise TransitionResultError(exc) from exc
            # PERF-005: drop the now-terminal item from the live queue; the
            # review store owns its historical record.
            self.queue.remove(item.candidate.source, item.candidate.id)
        else:
            item.status = prospective.status
        return {"id": id, "status": STATUS_REJECTED}

    def _refresh_queue_from_store(self) -> None:
        """CORE-005: this instance's view lost a race. Drop the stale local
        copy and rehydrate the live queue from the durable store."""
        if self._review_store is None:
            return
        self._review_store.refresh()
        fresh = ReviewQueue()
        for stored in self._review_store.pending():
            fresh.add(stored)
        self.queue = fresh

    # -- dispatch ------------------------------------------------------

    def dispatch(self, verb: str, **args) -> Result:
        handler = self._handlers().get(verb)
        if handler is None:
            # Refused, not attempted. Nothing below this line runs.
            return Result(
                ok=False,
                verb=verb,
                error=f"unknown verb {verb!r} -- known verbs: {', '.join(sorted(VERBS))}",
            )
        schema_error = _validate_args(verb, args)
        if schema_error is not None:
            return Result(ok=False, verb=verb, error=schema_error)
        try:
            return Result(ok=True, verb=verb, data=handler(**args))
        except TransitionResultError as exc:
            # W2-004: a compare-and-set refusal is a REJECTED operation, not a
            # successful one. Render it as ok=False with the authoritative
            # durable status so callers don't treat a refused transition as
            # accepted.
            c = exc.exc
            return Result(
                ok=False,
                verb=verb,
                data={
                    "id": c.identity[1],
                    "conflict": str(c),
                    "status": c.durable_status,
                },
                error=str(c),
            )
        except TypeError as exc:
            # W2-009: after schema validation passed, a TypeError can still
            # be the caller's fault only when the arguments do not BIND to
            # the handler's signature. Anything else is an internal failure
            # (a source factory bug, a config problem) and must be reported
            # as one, not disguised as a caller mistake.
            try:
                inspect.signature(handler).bind(**args)
            except TypeError:
                return Result(ok=False, verb=verb, error=f"bad arguments for {verb!r}: {exc}")
            return Result(ok=False, verb=verb, error=f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 -- a driven session reports, never crashes
            return Result(ok=False, verb=verb, error=f"{type(exc).__name__}: {exc}")

    def _handlers(self) -> dict:
        return {
            "scout": self._scout,
            "report": self._report,
            "status": self._status,
            "queue": self._queue,
            "approve": self._approve,
            "inspect": self._inspect,
            "reject": self._reject,
            "watch": self._watch,
        }


VERBS = frozenset({"scout", "report", "status", "queue", "approve", "inspect", "reject", "watch"})
