"""Screen 3 -- Monitor. One explicit bounded cycle, never a fake daemon.

The GUI does not re-implement the daemon: continuous unattended monitoring
belongs to `python -m saipet.monitor`. This screen runs one bounded cycle
through the same bridge `watch` verb and reports what came back. No daemon
is ever claimed to be alive without persistent evidence.
"""

import time
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


class MonitorView(tk.Frame):
    def __init__(self, master, controller):
        super().__init__(master, bg=BACKGROUND)
        self.controller = controller
        self.state = controller.state.monitor

        SectionHeader(self, "Monitor").pack(fill="x", anchor="w", pady=(0, 2))
        tk.Label(
            self,
            text="Run one bounded monitor cycle. A separate monitor process is not "
            "detected; this screen shows only the last explicit cycle.",
            font=theme.META_FONT,
            fg=TEXT_SECONDARY,
            bg=BACKGROUND,
            anchor="w",
            justify="left",
            wraplength=theme.SCREEN_MIN_W - 96,
        ).pack(fill="x", anchor="w", pady=(0, theme.SECTION_GAP))

        cfg = GoldenGroup(self, "Cycle")
        cfg.pack(fill="x", pady=(0, theme.SECTION_GAP))
        self._min_score = LabeledField(
            cfg.body, "Report findings at or above", SunkenEntry, {"width": 8}
        )
        self._min_score.pack(fill="x")
        self._min_score.set(config.NOTIFY_MIN_SCORE)
        self._min_score_edited = False
        self._min_score.field.entry.bind("<KeyRelease>", self._on_min_score_edited)
        tk.Label(
            cfg.body,
            text="One cycle scouts the configured allowlist and reports findings at or "
            "above the bar. For continuous monitoring use `python -m saipet.monitor`.",
            font=theme.META_FONT,
            fg=TEXT_SECONDARY,
            bg=theme.SURFACE_RAISED,
            anchor="w",
            justify="left",
            wraplength=theme.SCREEN_MIN_W - 96,
        ).pack(fill="x", anchor="w", pady=(theme.SECTION_GAP, 0))
        self._run_button = RaisedButton(
            cfg.body, "Run one monitor cycle", command=self._on_run, width=190
        )
        self._run_button.pack(anchor="w", pady=(theme.SECTION_GAP, 0))

        result_group = GoldenGroup(self, "Last monitor cycle")
        result_group.pack(fill="x")
        self._result = SunkenText(result_group.body, height=7, readonly=True)
        self._result.pack(fill="x")

    def _on_run(self) -> None:
        try:
            raw = self._min_score.get().strip()
            if self._min_score_edited:
                min_score = float(raw) if raw else float(config.NOTIFY_MIN_SCORE)
            else:
                # W2-005: an untouched field uses the current config so a
                # settings save applies without restart.
                min_score = None
        except ValueError:
            self.controller.post_status(
                "error",
                "Cause: the notify score is not a number.\n"
                "Effect: the monitor cycle did not start.\n"
                "Fix: enter a score between 0 and 100, then run the cycle again.",
            )
            return
        if min_score is not None and not 0 <= min_score <= 100:
            self.controller.post_status(
                "error",
                "Cause: the notify score must be between 0 and 100.\n"
                "Effect: the monitor cycle did not start.\n"
                "Fix: enter a score between 0 and 100, then run the cycle again.",
            )
            return

        accepted = self.controller.run_monitor_cycle(
            1, 0, min_score, None, 25, None, done=self._run_done
        )
        if accepted:
            self._set_busy(True)

    def _on_min_score_edited(self, _event=None) -> None:
        self._min_score_edited = True

    def set_config(self) -> None:
        """Re-sync the notify bar after a settings save unless the user
        deliberately edited this cycle's value (W2-005)."""
        if not self._min_score_edited:
            self._min_score.set(config.NOTIFY_MIN_SCORE)

    def _run_done(self, _result) -> None:
        self._set_busy(False)
        self._render_result()

    def _set_busy(self, busy: bool) -> None:
        self._run_button.set_enabled(not busy)
        self._run_button.set_text("Cycle running..." if busy else "Run one monitor cycle")
        self._min_score.field.entry.config(state="disabled" if busy else "normal")

    def _render_result(self) -> None:
        data = self.state.result or {}
        monitor = data.get("monitor") or {}
        last_at = monitor.get("last_cycle_at")
        last_txt = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(last_at)) if last_at else "never"
        lines = [
            f"Cycles run: {monitor.get('cycles', 0)}",
            f"Health: {monitor.get('health', 'healthy')}",
            f"Last cycle: {last_txt}",
            f"Queued total: {monitor.get('queued_total', 0)}   "
            f"Notified total: {monitor.get('notified_total', 0)}",
            f"Errors: {monitor.get('errors', 0)}",
        ]
        if monitor.get("last_error"):
            lines.append(f"Last error: {monitor['last_error']}")
        if monitor.get("last_degradation"):
            lines.append(f"Last degradation: {monitor['last_degradation']}")
        findings = data.get("findings") or []
        if findings:
            lines.append(f"Findings this cycle ({len(findings)}):")
            for item in findings:
                lines.append(
                    f"  [{item.get('band', '?')}] {item.get('score', 0):.0f} "
                    f"r/{item.get('subreddit') or '?'} -- {item.get('title', '')}"
                )
        self._result.set_text("\n".join(lines))

    def primary_action(self):
        if not self.controller.state.is_busy():
            self._on_run()
