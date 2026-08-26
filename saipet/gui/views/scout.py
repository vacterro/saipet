"""Screen 1 -- Scout. Explicitly start a scouting run and read what happened."""

import tkinter as tk

from saipet import config
from saipet.gui import theme
from saipet.gui.theme import BACKGROUND, TEXT_SECONDARY
from saipet.gui.widgets import (
    GoldenGroup,
    LabeledField,
    RaisedButton,
    SectionHeader,
    SunkenEntry,
    SunkenText,
)

DEFAULT_LIMIT = 25


class ScoutView(tk.Frame):
    def __init__(self, master, controller, navigate=None):
        super().__init__(master, bg=BACKGROUND)
        self.controller = controller
        self.state = controller.state.scout
        self._navigate = navigate

        SectionHeader(self, "Scout").pack(fill="x", anchor="w", pady=(0, 2))
        tk.Label(
            self,
            text="Fetch, score and gate a run. Nothing is ever posted.",
            font=theme.META_FONT,
            fg=TEXT_SECONDARY,
            bg=BACKGROUND,
            anchor="w",
        ).pack(fill="x", anchor="w", pady=(0, theme.SECTION_GAP))

        targets = GoldenGroup(self, "Targets")
        targets.pack(fill="x", pady=(0, theme.SECTION_GAP))
        tk.Label(
            targets.body,
            text="Subreddits",
            font=theme.BODY_FONT,
            fg=theme.TEXT_PRIMARY,
            bg=theme.SURFACE_RAISED,
            anchor="w",
        ).pack(fill="x", anchor="w")
        self._subreddits = SunkenText(targets.body, height=3, readonly=False)
        self._subreddits.pack(fill="x")
        self._subreddits_note = tk.Label(
            targets.body,
            text="",
            font=theme.META_FONT,
            fg=TEXT_SECONDARY,
            bg=theme.SURFACE_RAISED,
            anchor="w",
        )
        self._subreddits_note.pack(fill="x", anchor="w", pady=(2, 0))
        self._sync_subreddits_note()

        run_group = GoldenGroup(self, "Run")
        run_group.pack(fill="x", pady=(0, theme.SECTION_GAP))
        self._limit = LabeledField(
            run_group.body, "Posts per subreddit", SunkenEntry, {"width": 8}
        )
        self._limit.pack(fill="x")
        self._limit.set(DEFAULT_LIMIT)
        self._since = LabeledField(
            run_group.body, "Maximum age (hours)", SunkenEntry, {"width": 8}
        )
        self._since.pack(fill="x", pady=(theme.SECTION_GAP, 0))
        self._since.set(config.MAX_AGE_HOURS)
        self._since_edited = False
        self._since.field.entry.bind("<KeyRelease>", self._on_since_edited)
        tk.Label(
            run_group.body,
            text="Enter 0 to disable the freshness window.",
            font=theme.META_FONT,
            fg=TEXT_SECONDARY,
            bg=theme.SURFACE_RAISED,
            anchor="w",
        ).pack(fill="x", anchor="w", pady=(2, theme.SECTION_GAP))

        self._run_button = RaisedButton(
            run_group.body,
            "Run scout",
            command=self._on_run,
            width=150,
        )
        self._run_button.pack(side="left", pady=(0, 0))
        self._view_queue_button = RaisedButton(
            run_group.body,
            "View queue",
            command=self._on_view_queue,
            width=110,
            enabled=False,
        )
        self._view_queue_button.pack(side="left", padx=(theme.SECTION_GAP, 0))

        result_group = GoldenGroup(self, "Last run")
        result_group.pack(fill="x")
        self._result = SunkenText(result_group.body, height=6, readonly=True)
        self._result.pack(fill="x")

    # -- helpers -------------------------------------------------------

    def _sync_subreddits_note(self) -> None:
        if not config.SUBREDDIT_ALLOWLIST:
            self._subreddits_note.config(text="No allowed subreddits configured.")
        else:
            self._subreddits_note.config(text="Leave empty to use the configured allowlist.")

    def set_allowlist(self) -> None:
        self._sync_subreddits_note()

    def _on_since_edited(self, _event=None) -> None:
        self._since_edited = True

    def set_config(self) -> None:
        """Re-sync the freshness field after a settings save, unless the user
        deliberately edited this run's value (W2-005)."""
        self._sync_subreddits_note()
        if not self._since_edited:
            self._since.set(config.MAX_AGE_HOURS)

    def _read_subreddits(self) -> list[str]:
        lines = [line.strip() for line in self._subreddits.get_text().splitlines()]
        return [line for line in lines if line]

    def _on_run(self) -> None:
        try:
            limit = int(self._limit.get().strip())
        except ValueError:
            self.controller.post_status(
                "error",
                "Cause: 'Posts per subreddit' is not a whole number.\n"
                "Effect: the scout run did not start.\n"
                "Fix: enter a whole number of at least 1, then run scout again.",
            )
            return
        if limit < 1:
            self.controller.post_status(
                "error",
                "Cause: 'Posts per subreddit' must be at least 1.\n"
                "Effect: the scout run did not start.\n"
                "Fix: raise the limit to at least 1, then run scout again.",
            )
            return
        try:
            since_raw = self._since.get().strip()
            if self._since_edited:
                since = float(since_raw) if since_raw else float(config.MAX_AGE_HOURS)
            else:
                # W2-005: an untouched field defers to the current config, so
                # a settings save applies without restart.
                since = None
        except ValueError:
            self.controller.post_status(
                "error",
                "Cause: 'Maximum age (hours)' is not a number.\n"
                "Effect: the scout run did not start.\n"
                "Fix: enter a number of hours (0 disables the window), then run scout again.",
            )
            return
        if since is not None and since < 0:
            self.controller.post_status(
                "error",
                "Cause: 'Maximum age (hours)' must not be negative.\n"
                "Effect: the scout run did not start.\n"
                "Fix: enter 0 or a positive number of hours, then run scout again.",
            )
            return

        accepted = self.controller.run_scout(
            self._read_subreddits(), limit, since, done=self._on_run_done
        )
        if accepted:
            self._set_busy(True)

    def _on_view_queue(self) -> None:
        if self._navigate is not None:
            self._navigate("queue")

    def _on_run_done(self, _result) -> None:
        self._set_busy(False)
        if self.state.completed_run:
            self._view_queue_button.set_enabled(True)
            self._render_result()
        else:
            self._view_queue_button.set_enabled(False)

    def _set_busy(self, busy: bool) -> None:
        self._run_button.set_enabled(not busy)
        self._run_button.set_text("Scout running..." if busy else "Run scout")
        self._subreddits.text.config(state="disabled" if busy else "normal")
        self._limit.field.entry.config(state="disabled" if busy else "normal")
        self._since.field.entry.config(state="disabled" if busy else "normal")

    def _render_result(self) -> None:
        data = self.state.result or {}
        fetch = data.get("fetch") or {}
        health = data.get("health", "healthy")
        lines = [
            f"Source: {data.get('source', '?')}",
            f"Targets: {fetch.get('attempted', 0)}",
            f"Succeeded: {fetch.get('succeeded', 0)}",
            f"Failed: {fetch.get('failed', 0) if fetch.get('failed') is not None else 0}",
            f"Queued: {data.get('queued', 0)}",
        ]
        report = data.get("report") or {}
        if report.get("jsonl"):
            lines.append(f"Report: {report['jsonl']}")
        failures = data.get("failures") or []
        if failures:
            lines.append("Not fetched:")
            for failure in failures:
                lines.append(f"  {failure.get('subreddit')}: {failure.get('error')}")
        if health == "failed":
            lines.append(
                "Every target failed. Check Reddit credentials and subreddit access, then run scout again."
            )
        elif health == "degraded":
            lines.append(
                "Some targets failed; the rest were searched. Check the failed subreddits and retry."
            )
        self._result.set_text("\n".join(lines))

    def primary_action(self):
        if not self.controller.state.is_busy():
            self._on_run()
