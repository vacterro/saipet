"""The one translation layer between GUI actions and Bridge.dispatch().

A view never reimplements scouting, scoring, or reporting: it calls a
controller method, the controller calls one bridge verb, and the result
arrives back on the UI thread. Every operation runs on a single gated worker
(`_begin`/`_run`/`_end`), so the Tk main loop is never blocked and bridge
state is never mutated from two threads at once.

Worker-to-UI delivery uses a thread-safe queue drained on the Tk event loop
(see `drain_results` / App's poller). Tkinter forbids touching widgets from
any other thread, and `root.after` itself may not be called off the main
thread, so the worker only ever queues a closure; the UI thread runs it.

`synchronous=True` exists for tests: every operation then runs inline in the
calling thread and the result is delivered before the method returns.
"""

import queue
import threading
from pathlib import Path
from urllib.parse import urlparse
from webbrowser import open as _open_browser

from saipet.bridge import Result
from saipet.gui.state import GuiState, Operation
from saipet.runtime_config import (
    apply_overrides,
    load_overrides,
    validate_overrides,
    write_overrides,
)

_APPROVED_REDDIT_HOSTS = frozenset({"reddit.com", "www.reddit.com", "old.reddit.com"})


class Controller:
    """Owns the bridge, the worker, and the presentation state."""

    # PERF-006: the bounded shutdown deadline, shared by the blocking
    # variant and the App's async poller.
    SHUTDOWN_DEADLINE_SECONDS = 10.0

    def __init__(self, bridge, synchronous=False):
        self.bridge = bridge
        self.state = GuiState()
        self.on_status = None  # callable(kind, text); set by the app
        self.on_activity = None  # callable(); set by the app to wake the poller
        self._synchronous = synchronous
        self._result_queue: queue.Queue = queue.Queue()
        self._worker = None
        self._closed = False

    # -- status --------------------------------------------------------

    def post_status(self, kind: str, text: str) -> None:
        self.state.status_kind = kind
        self.state.status_text = text
        if self.on_status is not None:
            self.on_status(kind, text)

    # -- operation lifecycle -------------------------------------------

    # Operations that mutate review/seen state and therefore must not run
    # through a degraded (corrupt review-store) session.
    _REVIEW_MUTATIONS = frozenset({"scout", "watch", "approve", "reject"})

    def _begin(self, name: str) -> bool:
        """Start a new operation. Returns True iff accepted (no other op
        running AND shutdown has not begun). The caller must not render
        busy UI without this signal."""
        # W2-005: once shutdown has begun, no new work is accepted -- not even
        # a fresh scout or queue refresh. Forcing IDLE here would lie about
        # ownership of a genuinely in-flight worker.
        if self._closed:
            self.post_status(
                "warn",
                f"{name} not started: the controller is shut down.",
            )
            return False
        if name in self._REVIEW_MUTATIONS:
            degraded = getattr(self.bridge, "review_state_error", None)
            if degraded:
                self.post_status(
                    "error",
                    f"Cause: {degraded}\n"
                    "Effect: review mutations are disabled until the durable review "
                    "state is repaired.\n"
                    "Fix: correct or remove the corrupt review state file and restart.",
                )
                return False
        if self.state.operation.running:
            self.post_status(
                "warn",
                f"{name} not started: {self.state.operation.name} is still running.",
            )
            return False
        op = self.state.operation
        op.name = name
        op.id += 1
        op.state = Operation.RUNNING
        op.result = None
        op.error = ""
        return True

    def _end(self, result: Result) -> None:
        op = self.state.operation
        if result.ok:
            op.state = Operation.SUCCEEDED
            op.result = result.data
        else:
            op.state = Operation.FAILED
            op.error = result.error
        # W2-005: the latest selection made while this operation owned the
        # bridge is now served -- exactly one deferred read, always for what
        # the user clicked LAST. W2-006: we pass the original callback so the
        # view is notified of the ACTUAL deferred result, not a fake None.
        pending = self.state.queue.pending_select
        if pending is not None:
            cb = self.state.queue.pending_select_cb
            self.state.queue.pending_select = None
            self.state.queue.pending_select_cb = None
            source, item_id = pending
            self.select_finding(item_id, source=source, done=cb)
        pending_preview = self.state.reports.pending_preview
        if pending_preview is not None:
            cb = self.state.reports.pending_preview_cb
            self.state.reports.pending_preview = None
            self.state.reports.pending_preview_cb = None
            if self.state.reports.selected_path == pending_preview:
                self.preview_report(pending_preview, done=cb)

    # -- the single worker path ----------------------------------------

    def _run(self, label, work, apply_state, done):
        """Run `work()` (returns a Result) under the busy gate, then apply.

        The caller must have already passed `_begin(label)`. `apply_state`
        mutates presentation state; `done` is the view's refresh hook. Both
        run on the UI thread -- synchronously in test mode, via the result
        queue otherwise.
        """

        def wrapped():
            try:
                return work()
            except Exception as exc:
                return Result(ok=False, verb=label, error=f"{type(exc).__name__}: {exc}")

        def finish(result):
            apply_state(result)
            self._end(result)
            if done is not None:
                done(result)

        if self._synchronous:
            finish(wrapped())
            return

        def target():
            result = wrapped()
            # CORE-006: deliver through EXACTLY the same single completion path
            # as synchronous mode. `finish` itself calls `_end` (once), so
            # enqueuing only `finish` avoids double-releasing operation
            # ownership -- the old `(self._end(result), finish(result))` ran
            # `_end` twice, which could mark a still-running deferred preview
            # as terminal with the previous operation's result.
            self._result_queue.put(lambda: finish(result))

        self._worker = threading.Thread(
            target=target, daemon=True, name=f"saipet-gui-{label}"
        )
        if self.on_activity is not None:
            self.on_activity()
        self._worker.start()

    def drain_results(self) -> None:
        """Run every queued result on the UI thread. After shutdown, they
        are discarded without touching widgets."""
        while True:
            try:
                fn = self._result_queue.get_nowait()
            except queue.Empty:
                return
            if self._closed:
                continue
            try:
                fn()
            except Exception as exc:  # a result handler must not kill the loop
                self.post_status(
                    "error",
                    f"Cause: a background result handler raised: {exc}\n"
                    "Effect: the result was not applied.\n"
                    "Fix: retry the operation.",
                )

    # -- config load on startup ----------------------------------------

    def load_config(self, config_path: str) -> str | None:
        """Load and apply persisted overrides. Returns an error message on
        failure, None on success (or no file to load)."""
        try:
            overrides = load_overrides(config_path)
        except Exception as exc:
            return (
                f"Cause: config file is invalid: {exc}\n"
                "Effect: saved settings were not applied.\n"
                f"Fix: correct or remove {config_path} and reload."
            )
        if not overrides:
            return None
        try:
            validate_overrides(overrides)
            apply_overrides(overrides)
        except Exception as exc:
            return (
                f"Cause: config file is invalid: {exc}\n"
                "Effect: saved settings were not applied.\n"
                f"Fix: correct or remove {config_path} and reload."
            )
        return None

    # -- scout ---------------------------------------------------------

    def run_scout(self, subreddits, limit, since_hours, done=None) -> bool:
        if not self._begin("scout"):
            return False
        self.post_status("info", "Scout running...")

        def work():
            return self.bridge.dispatch(
                "scout",
                subreddits=subreddits,
                limit=limit,
                since_hours=since_hours,
            )

        def apply(result):
            if result.ok:
                self.state.scout.result = result.data
                self.state.scout.completed_run = True
                self.post_status(
                    "info",
                    f"Scout completed: {result.data.get('queued', 0)} findings queued.",
                )
            else:
                self.state.scout.completed_run = False
                self.state.scout.error = result.error
                self.post_status(
                    "error",
                    f"Cause: {result.error}\nEffect: the scout run did not finish.\n"
                    "Fix: correct the reported cause, then run scout again.",
                )

        self._run("scout", work, apply, done)
        return True

    # -- queue ---------------------------------------------------------

    def refresh_queue(self, done=None) -> bool:
        if not self._begin("queue"):
            return False

        def work():
            return self.bridge.dispatch("queue")

        def apply(result):
            if result.ok:
                items = result.data.get("items", [])
                self.state.queue.items = items
                self.state.queue.snapshot_stale = False
                sel = (self.state.queue.selected_source, self.state.queue.selected_id)
                if sel[1] is not None and not any(
                    i["id"] == sel[1] and i.get("source") == sel[0] for i in items
                ):
                    # CORE-012: clear on full-identity match only.
                    self.state.queue.selected_source = None
                    self.state.queue.selected_id = None
                    self.state.queue.detail = None
                self.post_status("info", f"Queue refreshed: {len(items)} pending finding(s).")
            else:
                self.state.queue.error = result.error
                self.state.queue.snapshot_stale = True
                self.post_status(
                    "warn",
                    f"Refresh failed: showing previous data.\nCause: {result.error}\n"
                    "Effect: the queue still shows the previous snapshot.\n"
                    "Fix: try the refresh again.",
                )

        self._run("queue", work, apply, done)
        return True

    def select_finding(self, item_id, done=None, source=None) -> None:
        """Inspect one row. Synchronous in-memory lookup, but only when the
        bridge is not owned by a mutating worker (W2-004): the UI thread must
        never dispatch into a bridge another thread is mutating. The row is
        identified by its canonical (source, id) pair (CORE-012)."""
        self.state.queue.selected_source = source
        self.state.queue.selected_id = item_id
        self.state.queue.detail = None
        if self.state.operation.running:
            # W2-006: remember THIS click (and its callback) as the one deferred
            # read; when the worker settles, _end serves exactly it with the
            # real result. An earlier queued read is superseded, never stacked.
            self.state.queue.pending_select = (source, item_id)
            self.state.queue.pending_select_cb = done
            return
        kwargs = {"id": item_id}
        if source is not None:
            kwargs["source"] = source
        result = self.bridge.dispatch("inspect", **kwargs)
        if result.ok:
            self.state.queue.detail = result.data
        else:
            self.post_status(
                "error",
                f"Cause: {result.error}\nEffect: the finding detail could not be loaded.\n"
                "Fix: refresh the queue and select the finding again.",
            )
        if done is not None:
            done(result)

    def _selection_matches_loaded_detail(self, item_id, source) -> bool:
        """W2-005: a destructive action may land only on the finding whose
        detail is actually loaded and identical to the current selection."""
        detail = self.state.queue.detail
        if detail is None:
            return False
        return (
            detail.get("id") == item_id
            and detail.get("source") == source
            and (source, item_id)
            == (self.state.queue.selected_source, self.state.queue.selected_id)
        )

    @staticmethod
    def _same_finding(row: dict, item_id, source) -> bool:
        """CORE-012: exact canonical-identity match for removal/clearing --
        an id-only filter could delete a bystander row sharing the id."""
        return row["id"] == item_id and row.get("source") == source

    def approve(self, item_id, solution, mention_saipen, done=None, source=None) -> bool:
        if not solution or not solution.strip():
            self.post_status(
                "warn",
                "Approve & build draft: enter your solution first.",
            )
            return False
        if not self._selection_matches_loaded_detail(item_id, source):
            # W2-005: the detail on screen is not this finding's -- acting
            # anyway could approve the wrong record.
            self.post_status(
                "warn",
                "Approve & build draft: the loaded detail does not match the selected "
                "finding. Select it again to load its details, then approve.",
            )
            return False
        if not self._begin("approve"):
            return False
        self.post_status("info", "Building draft...")

        def work():
            kwargs = {"id": item_id, "solution": solution, "mention_saipen": mention_saipen}
            if source is not None:
                kwargs["source"] = source
            return self.bridge.dispatch("approve", **kwargs)

        def apply(result):
            if result.ok:
                self.state.queue.draft = result.data.get("draft")
                self.state.queue.items = [
                    i
                    for i in self.state.queue.items
                    if not self._same_finding(i, item_id, source)
                ]
                # W2-004: only clear the selection if the user hasn't moved
                # to a different finding while this approval was in flight.
                if (
                    self.state.queue.selected_id == item_id
                    and self.state.queue.selected_source == source
                ):
                    self.state.queue.selected_source = None
                    self.state.queue.selected_id = None
                    self.state.queue.detail = None
                self.post_status(
                    "success",
                    "Draft created. Nothing was posted.",
                )
            else:
                # Failure preserves user state: the finding, solution, mention
                # and draft remain untouched for the user to fix.
                self.post_status(
                    "error",
                    f"Cause: {result.error}\nEffect: the draft was not built.\n"
                    "Fix: correct the reported cause, then approve again.",
                )

        self._run("approve", work, apply, done)
        return True

    def dismiss(self, item_id, done=None, source=None) -> bool:
        if not self._selection_matches_loaded_detail(item_id, source):
            # W2-005: never dismiss against a stale/absent detail.
            self.post_status(
                "warn",
                "Dismiss finding: the loaded detail does not match the selected "
                "finding. Select it again to load its details, then dismiss.",
            )
            return False
        if not self._begin("reject"):
            return False

        def work():
            kwargs = {"id": item_id}
            if source is not None:
                kwargs["source"] = source
            return self.bridge.dispatch("reject", **kwargs)

        def apply(result):
            if result.ok:
                self.state.queue.items = [
                    i
                    for i in self.state.queue.items
                    if not self._same_finding(i, item_id, source)
                ]
                # W2-004: do not destroy a newer selection made during the
                # dismissal.
                if (
                    self.state.queue.selected_id == item_id
                    and self.state.queue.selected_source == source
                ):
                    self.state.queue.selected_source = None
                    self.state.queue.selected_id = None
                    self.state.queue.detail = None
                self.post_status(
                    "info", f"Dismissed finding {item_id}. Nothing was posted."
                )
            else:
                # Failure preserves the finding detail panel.
                self.post_status(
                    "error",
                    f"Cause: {result.error}\nEffect: the finding was not dismissed.\n"
                    "Fix: refresh the queue and dismiss again.",
                )

        self._run("reject", work, apply, done)
        return True

    # -- reports -------------------------------------------------------

    def refresh_reports(self, done=None) -> bool:
        if not self._begin("report"):
            return False

        def work():
            return self.bridge.dispatch("report")

        def apply(result):
            if result.ok:
                self.state.reports.available = result.data.get("available", [])
                self.state.reports.last = result.data.get("last") or {}
                self.state.reports.refresh_failed = False
                self.post_status(
                    "info",
                    f"Reports refreshed: {len(self.state.reports.available)} available.",
                )
            else:
                self.state.reports.error = result.error
                self.state.reports.refresh_failed = True
                self.post_status(
                    "warn",
                    f"Refresh failed: showing previous list.\nCause: {result.error}\n"
                    "Effect: the report list still shows the previous snapshot.\n"
                    "Fix: try the refresh again.",
                )

        self._run("report", work, apply, done)
        return True

    def preview_report(self, path, done=None) -> bool:
        """Read one report file into the preview pane.

        Runs as its own operation (CORE-002): it acquires the busy gate like
        every other `_run` caller, so its completion never terminates an
        unrelated operation. A generation counter additionally discards a
        stale result that could overwrite a newer selection.

        W2-005: a click while another operation is busy is the LATEST
        selection -- it is queued and served once when the worker settles,
        so highlight and preview can never settle on different reports.
        """
        if not path:
            self.state.reports.preview = ""
            self.post_status(
                "error",
                "Cause: report path is empty.\nEffect: no preview to show.\n"
                "Fix: refresh reports and select one.",
            )
            if done is not None:
                done(None)
            return False
        if not self._begin("preview"):
            # W2-006: gate held by another operation -- store target plus
            # callback so the view is notified with the real result when the
            # worker settles, not with a fake None.
            self.state.reports.selected_path = path
            self.state.reports.pending_preview = path
            self.state.reports.pending_preview_cb = done
            return False
        self.state.reports.pending_preview = None
        self.state.reports.selected_path = path
        self.state.reports.preview = None
        self.state.reports.preview_gen += 1
        gen = self.state.reports.preview_gen
        target = Path(path)

        def work():
            text = target.read_text(encoding="utf-8")
            return Result(ok=True, verb="preview", data={"text": text})

        def apply(result):
            if gen != self.state.reports.preview_gen:
                return  # stale: a newer selection replaced this one
            if result.ok:
                self.state.reports.preview = result.data["text"]
            else:
                self.state.reports.preview = ""
                self.post_status(
                    "error",
                    f"Cause: {result.error}\nEffect: the report at {target} could not be read.\n"
                    "Fix: refresh reports and select an existing file.",
                )

        self._run("preview", work, apply, done)
        return True

    # -- monitor -------------------------------------------------------

    def run_monitor_cycle(self, cycles, interval, min_score, subreddits, limit, since_hours, done=None) -> bool:
        if not self._begin("watch"):
            return False
        self.post_status("info", "Monitor cycle running...")

        def work():
            return self.bridge.dispatch(
                "watch",
                cycles=cycles,
                interval_seconds=interval,
                min_score=min_score,
                subreddits=subreddits,
                limit=limit,
                since_hours=since_hours,
            )

        def apply(result):
            if result.ok:
                self.state.monitor.result = result.data
                self.state.monitor.last_cycle_at = (
                    result.data.get("monitor", {}).get("last_cycle_at")
                )
                self.post_status(
                    "info",
                    "Monitor cycle completed. Nothing was posted.",
                )
            else:
                self.state.monitor.error = result.error
                self.post_status(
                    "error",
                    f"Cause: {result.error}\nEffect: the monitor cycle did not finish.\n"
                    "Fix: correct the reported cause, then run the cycle again.",
                )

        self._run("watch", work, apply, done)
        return True

    # -- settings ------------------------------------------------------

    def validate_settings(self, overrides) -> tuple[bool, str]:
        try:
            validate_overrides(overrides)
        except Exception as exc:
            return False, str(exc)
        return True, ""

    def save_settings(self, overrides, config_path, done=None) -> bool:
        if not self._begin("save_settings"):
            return False

        def work():
            # CORE-007: the Settings form does not expose every persisted key
            # (report_retention_days, seen_ttl_days). Preserve those supported
            # keys from the existing file so saving an unrelated field does
            # not silently delete them. The in-memory config and the on-disk
            # document represent the same canonical set immediately after save.
            try:
                existing = load_overrides(config_path)
            except Exception:
                existing = {}
            merged = dict(overrides)
            for key in ("report_retention_days", "seen_ttl_days"):
                if key not in merged and key in existing:
                    merged[key] = existing[key]
            write_overrides(merged, config_path)
            applied = apply_overrides(merged)
            return Result(ok=True, verb="save_settings", data={"applied": applied})

        def apply(result):
            if result.ok:
                self.state.settings.saved = True
                self.state.settings.applied_keys = result.data["applied"]
                self.post_status(
                    "success",
                    f"Saved {config_path}\nApplied: {', '.join(result.data['applied'])}",
                )
            else:
                self.state.settings.saved = False
                self.post_status(
                    "error",
                    f"Cause: {result.error}\nEffect: the settings were not saved.\n"
                    "Fix: correct the invalid field, then save again.",
                )

        self._run("save_settings", work, apply, done)
        return True

    # -- thread / browser / shutdown -----------------------------------

    def open_thread(self, url) -> bool:
        """Open a validated HTTPS Reddit permalink, only after an explicit
        press. Uses real hostname rules, not substring checks."""
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or host not in _APPROVED_REDDIT_HOSTS or not parsed.path.startswith("/r/"):
            self.post_status(
                "error",
                f"Cause: {url!r} is not a recognised Reddit permalink.\n"
                "Effect: the thread was not opened.\n"
                "Fix: check the permalink and try again.",
            )
            return False
        try:
            _open_browser(url)
        except Exception as exc:
            self.post_status(
                "error",
                f"Cause: the browser could not be opened: {exc}.\n"
                "Effect: the thread was not opened.\n"
                "Fix: check your default browser settings and try again.",
            )
            return False
        self.post_status("info", f"Opened {url} in the default browser.")
        return True

    def in_flight_worker(self):
        """The live worker thread, or None. Read-only snapshot for an async
        shutdown poller -- never joins."""
        return self._worker

    def begin_shutdown(self) -> None:
        """Stop accepting new work WITHOUT blocking the caller (PERF-006).

        Queued results are discarded from now on; the in-flight worker is
        left running so the Tk mainloop keeps pumping events while it
        settles. The App polls `in_flight_worker()` via `root.after` and
        destroys when the thread finishes or the bounded deadline expires.

        W2-005: we do NOT force the operation state to IDLE here -- a
        genuinely in-flight worker still owns that state, and overwriting it
        would make lifecycle tracking lie about what is actually running.
        New operations are refused by `_begin` checking `_closed`.
        """
        self._closed = True
        event = getattr(self.bridge, "cancel_event", None)
        if event is not None:
            # Cooperative stoppoint: the watch/monitor loop checks this
            # BETWEEN cycles, never mid-write, so an atomic commit finishes.
            event.set()

    def shutdown(self, wait_seconds: float = 10.0) -> None:
        """Blocking variant of `begin_shutdown`, kept for callers that are
        not the Tk UI thread (tests, headless tools).

        The single in-flight worker is given a bounded chance to finish so
        its local side effects (seen marks, review transitions, report or
        config writes) complete atomically rather than being cut at an
        arbitrary point. If it does not finish in time it remains a daemon
        thread and dies with the process -- state files are atomic, so an
        interrupted write never corrupts them.

        W2-005: after the worker settles (or the deadline hits), drain any
        pending result so the operation state reflects the true terminal
        status instead of staying RUNNING.
        """
        self.begin_shutdown()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(wait_seconds)
        # W2-005: drain pending results unconditionally so the state settles
        # after the worker finishes; `drain_results` skips when `_closed` but
        # a shutdown-phase drain is intentional cleanup, not stray UI work.
        while True:
            try:
                fn = self._result_queue.get_nowait()
            except queue.Empty:
                return
            try:
                fn()
            except Exception:
                pass