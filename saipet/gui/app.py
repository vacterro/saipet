"""The root window: header, tab strip, content, shared status region.

One window, deterministic navigation, nothing happens on open except the
initial message (and a deliberate persisted-config load). Screens are built
once at startup and shown/hidden on tab switch, so nothing reflows after the
first draw.
"""

import os
import tempfile
import threading
import time
import tkinter as tk
from pathlib import Path

from saipet.bridge import Bridge
from saipet.gui import theme
from saipet.gui.controller import Controller
from saipet.gui.theme import BACKGROUND, TEXT_SECONDARY
from saipet.gui.views.monitor import MonitorView
from saipet.gui.views.queue import QueueView
from saipet.gui.views.reports import ReportsView
from saipet.gui.views.scout import ScoutView
from saipet.gui.views.settings import SettingsView
from saipet.gui.widgets import StatusRegion, TabStrip
from saipet.jsonio import StateFileError
from saipet.runtime_config import DEFAULT_CONFIG_PATH

TABS = ["Scout", "Queue", "Monitor", "Reports", "Settings"]
TAB_TO_VIEW = {tab.lower(): tab for tab in TABS}


class App:
    def __init__(self, root=None, bridge=None, config_path=None):
        self.root = root if root is not None else tk.Tk()
        self._config_path = config_path if config_path is not None else DEFAULT_CONFIG_PATH
        self._closing = False
        self._poll_after_id = None
        self._poll_delay_index = 0

        self.root.title("SAIPET")
        self.root.geometry(f"{theme.DEFAULT_W}x{theme.DEFAULT_H}")
        self.root.minsize(theme.SCREEN_MIN_W, theme.SCREEN_MIN_H)
        self.root.configure(bg=BACKGROUND)
        self.root.option_add("*font", theme.BODY_FONT)
        self.root.option_add("*foreground", theme.PALETTE["textPrimary"])

        if bridge is None:
            # CORE-006: apply the persisted runtime config BEFORE constructing
            # any config-dependent store, so SeenStore captures the configured
            # SEEN_TTL_DAYS (and friends) instead of the shipped default. The
            # error is surfaced again by controller.load_config below.
            self._preapply_config(self._config_path)
            bridge, self._startup_errors = self._build_bridge()
        else:
            self._startup_errors = []
        self.controller = Controller(bridge)
        self.controller.on_status = self._on_status
        self.controller.on_activity = self._kick_poll
        # PERF-006: the cooperative cancel signal begin_shutdown() sets; the
        # watch loop honours it between cycles.
        bridge.cancel_event = threading.Event()
        # Persisted configuration is loaded (and surfaced) so the Settings
        # screen prefills the values the scout will actually use.
        self._config_error = self.controller.load_config(self._config_path)

        self._build_header()
        self._tabs = TabStrip(self.root, TABS, on_change=self.show_tab)
        self._tabs.pack(fill="x", padx=theme.MARGIN, pady=(0, theme.SECTION_GAP))

        self._content = tk.Frame(self.root, bg=BACKGROUND)
        self._content.pack(fill="both", expand=True, padx=theme.MARGIN, pady=(0, theme.SECTION_GAP))

        self._screens: dict[str, tk.Frame] = {}
        self._build_screens()

        self._status = StatusRegion(self.root, lines=3)
        self._status.pack(fill="x", padx=theme.MARGIN, pady=(0, theme.MARGIN))

        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Control-r>", self._shortcut_run)

        startup = [e for e in ([self._config_error] if self._config_error else []) + self._startup_errors if e]
        if startup:
            self.controller.post_status("error", "\n\n".join(startup))
        else:
            self.controller.post_status("info", "Ready. Posting is disabled.")
        self.show_tab("scout")

    def _preapply_config(self, path: str) -> None:
        """CORE-006: apply persisted runtime config before any store is built,
        so config-dependent stores (SeenStore's TTL) start from the configured
        values. Errors are surfaced later by controller.load_config; a bad
        config must not block bridge construction with defaults."""
        from saipet.runtime_config import ConfigError, apply_overrides, load_overrides

        try:
            apply_overrides(load_overrides(path))
        except ConfigError:
            pass

    def _build_bridge(self):
        """Create the GUI's Bridge with durable review state. Corrupt state
        files are reported loudly and never silently converted to empty, and
        a corrupt review store blocks review mutations (CORE-001) instead of
        silently degrading to a session that marks things seen without ever
        making them durable.

        W2-003: seen-state and review-state initialization is classified
        independently so a seen-only corruption does not get misreported as
        review corruption (and vice versa).

        PERF-006: each store is constructed exactly ONCE and passed to the
        Bridge, so startup opens SeenStore/ReviewStore a single time instead
        of classifying them and then reopening identical instances.
        """
        from saipet.store import SeenStore
        from saipet.review_store import ReviewStore

        errors = []
        seen_path = "seen.json"
        review_path = "review-state.json"
        tmp_seen = Path(tempfile.gettempdir()) / f"saipet-seen-{os.getpid()}.json"

        # Classify each component independently so the error message always
        # names the CORRECT component, not whichever one happened to raise
        # first inside Bridge.__init__.
        seen_err = ""
        seen_store = None
        try:
            seen_store = SeenStore(seen_path)
        except StateFileError as exc:
            seen_err = str(exc)
        if seen_err:
            # Fresh temporary seen history; the corrupt one is untouched.
            seen_store = SeenStore(tmp_seen)

        review_err = ""
        review_store = None
        if Path(review_path).exists():
            try:
                review_store = ReviewStore(review_path)
            except StateFileError as exc:
                review_err = str(exc)

        if seen_err and review_err:
            # Both corrupt: use a temp seen, drop review mutations.
            bridge = Bridge(seen_store=seen_store, review_store=None)
            bridge.review_state_error = (
                f"review-state.json is corrupt ({review_err})"
            )
            errors.append(
                f"Cause: seen state is corrupt: {seen_err}\n"
                "Effect: the dedup history could not be loaded; a fresh history was started.\n"
                "Fix: correct or remove seen.json."
            )
            errors.append(
                f"Cause: review state is corrupt: {review_err}\n"
                "Effect: pending review items could not be restored; the corrupt file "
                "was left untouched, and review mutations are disabled until it is fixed.\n"
                "Fix: correct or remove review-state.json and restart."
            )
        elif seen_err:
            # Seen corrupt, review OK: temp seen + real review.
            bridge = Bridge(seen_store=seen_store, review_store=review_store)
            errors.append(
                f"Cause: seen state is corrupt: {seen_err}\n"
                "Effect: the dedup history could not be loaded; a fresh history was started.\n"
                "Fix: correct or remove seen.json."
            )
        elif review_err:
            # Seen OK, review corrupt: real seen + no review mutations.
            bridge = Bridge(seen_store=seen_store, review_store=None)
            bridge.review_state_error = (
                f"review-state.json is corrupt ({review_err})"
            )
            errors.append(
                f"Cause: review state is corrupt: {review_err}\n"
                "Effect: pending review items could not be restored; the corrupt file "
                "was left untouched, and review mutations are disabled until it is fixed.\n"
                "Fix: correct or remove review-state.json and restart."
            )
        else:
            bridge = Bridge(seen_store=seen_store, review_store=review_store)

        return bridge, errors

    # -- worker-result poller ------------------------------------------

    _POLL_DELAYS = (15, 30, 60, 100)  # ms, back off while the queue stays empty

    def _kick_poll(self) -> None:
        """Start (or keep) the drain poller. It runs only while an operation
        is active or a result is queued -- never as idle refresh. Empty polls
        back off so a long network operation does not burn main-loop
        wakeups at near frame rate (PERF-004)."""
        if self._poll_after_id is None and not self._closing:
            self._poll_delay_index = 0
            self._poll_after_id = self.root.after(self._POLL_DELAYS[0], self._poll_results)

    def _poll_results(self) -> None:
        self._poll_after_id = None
        if self._closing:
            return
        self.controller.drain_results()
        if self.controller.state.is_busy() or not self.controller._result_queue.empty():
            if self.controller._result_queue.empty():
                self._poll_delay_index = min(
                    self._poll_delay_index + 1, len(self._POLL_DELAYS) - 1
                )
            else:
                self._poll_delay_index = 0
            self._poll_after_id = self.root.after(
                self._POLL_DELAYS[self._poll_delay_index], self._poll_results
            )

    # -- construction ---------------------------------------------------

    def _build_header(self) -> None:
        header = tk.Frame(self.root, bg=BACKGROUND)
        header.pack(fill="x", padx=theme.MARGIN, pady=(theme.MARGIN, 4))
        tk.Label(
            header,
            text="SAIPET",
            font=theme.TITLE_FONT,
            fg=theme.TEXT_PRIMARY,
            bg=BACKGROUND,
        ).pack(side="left")
        tk.Label(
            header,
            text="  Read-only internet scout",
            font=theme.META_FONT,
            fg=TEXT_SECONDARY,
            bg=BACKGROUND,
        ).pack(side="left", pady=(8, 0))
        tk.Label(
            header,
            text="Posting: disabled",
            font=theme.META_FONT,
            fg=TEXT_SECONDARY,
            bg=BACKGROUND,
        ).pack(side="right", pady=(8, 0))

    def _build_screens(self) -> None:
        kwargs = {
            "scout": {"navigate": self.show_tab},
            "settings": {"on_saved": self._on_settings_saved, "config_path": self._config_path},
        }
        builders = {
            "scout": ScoutView,
            "queue": QueueView,
            "monitor": MonitorView,
            "reports": ReportsView,
            "settings": SettingsView,
        }
        for key, builder in builders.items():
            view = builder(self._content, self.controller, **kwargs.get(key, {}))
            self._screens[key] = view

    # -- navigation -----------------------------------------------------

    def show_tab(self, name: str) -> None:
        key = name.lower()
        if key not in self._screens:
            return
        self.controller.state.active_tab = key
        for k, frame in self._screens.items():
            if k == key:
                frame.pack(fill="both", expand=True)
            else:
                frame.pack_forget()
        self._tabs.set_active(TAB_TO_VIEW[key])

    def navigate(self, tab_key: str) -> None:
        """Explicit user-requested navigation from a view button."""
        self.show_tab(tab_key)

    # -- status ---------------------------------------------------------

    def _on_status(self, kind: str, text: str) -> None:
        self._status.set_message(text, kind)

    # -- keyboard -------------------------------------------------------

    def _shortcut_run(self, _event) -> None:
        key = self.controller.state.active_tab
        screen = self._screens.get(key)
        if screen is not None and hasattr(screen, "primary_action"):
            screen.primary_action()
        return "break"

    # -- settings callback ----------------------------------------------

    def _on_settings_saved(self) -> None:
        for key, screen in self._screens.items():
            if hasattr(screen, "set_allowlist"):
                screen.set_allowlist()
            if hasattr(screen, "set_config"):
                screen.set_config()

    # -- lifecycle ------------------------------------------------------

    def close(self) -> None:
        """Deterministic shutdown WITHOUT freezing the UI thread (PERF-006).

        The old close() joined the worker synchronously -- up to the full
        10-second deadline on the Tk thread, so closing mid-fetch froze the
        window for as long as the fetch had left. Now: stop the poller, ask
        the controller to stop accepting work (cooperatively signalling the
        bridge's cancel event), then poll the in-flight worker with
        `root.after` and destroy when it finishes or the bounded deadline
        expires. Late results are discarded by the controller either way.
        """
        if self._closing:
            return
        self._closing = True
        self._shutdown_started = time.monotonic()
        if self._poll_after_id is not None:
            try:
                self.root.after_cancel(self._poll_after_id)
            except tk.TclError:
                pass
            self._poll_after_id = None
        self.controller.begin_shutdown()
        self._await_worker()

    def _await_worker(self) -> None:
        worker = self.controller.in_flight_worker()
        settled = worker is None or not worker.is_alive()
        deadline_hit = (
            time.monotonic() - self._shutdown_started
        ) >= Controller.SHUTDOWN_DEADLINE_SECONDS
        if settled or deadline_hit:
            # Cancel this poller chain BEFORE destroying so no stray timer
            # fires against a dead interpreter state.
            if self._poll_after_id is not None:
                try:
                    self.root.after_cancel(self._poll_after_id)
                except tk.TclError:
                    pass
                self._poll_after_id = None
            try:
                self.root.destroy()
            except tk.TclError:
                pass
            return
        try:
            self._poll_after_id = self.root.after(50, self._await_worker)
        except tk.TclError:
            pass

    def mainloop(self) -> None:
        self.root.mainloop()


def run() -> None:
    App().mainloop()
