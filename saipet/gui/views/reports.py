"""Screen 4 -- Reports. Explicit refresh, plain-text preview, no external apps."""

from pathlib import Path
import tkinter as tk

from saipet.gui import theme
from saipet.gui.theme import BACKGROUND, TEXT_SECONDARY
from saipet.gui.widgets import (
    GoldenGroup,
    RaisedButton,
    SectionHeader,
    SimpleList,
    SunkenText,
)


class ReportsView(tk.Frame):
    def __init__(self, master, controller):
        super().__init__(master, bg=BACKGROUND)
        self.controller = controller
        self.state = controller.state.reports

        SectionHeader(self, "Reports").pack(fill="x", anchor="w", pady=(0, 2))
        tk.Label(
            self,
            text="Scout run reports from the runs directory. Preview is plain text.",
            font=theme.META_FONT,
            fg=TEXT_SECONDARY,
            bg=BACKGROUND,
            anchor="w",
        ).pack(fill="x", anchor="w", pady=(0, theme.SECTION_GAP))

        summary = tk.Frame(self, bg=BACKGROUND)
        summary.pack(fill="x", pady=(0, theme.SECTION_GAP))
        self._summary_label = tk.Label(
            summary,
            text="Available: 0",
            font=theme.BODY_FONT,
            fg=theme.TEXT_PRIMARY,
            bg=BACKGROUND,
            anchor="w",
        )
        self._summary_label.pack(side="left")
        self._refresh_button = RaisedButton(
            summary, "Refresh reports", command=self._on_refresh, width=130
        )
        self._refresh_button.pack(side="right")

        self._list = SimpleList(self, height=4, on_select=self._on_select)
        self._list.pack(fill="both", expand=True, pady=(0, theme.SECTION_GAP))

        preview = GoldenGroup(self, "Preview")
        preview.pack(fill="x")
        self._preview = SunkenText(preview.body, height=10, readonly=True)
        self._preview.pack(fill="x")

    def _on_refresh(self) -> None:
        accepted = self.controller.refresh_reports(done=self._refresh_done)
        if accepted:
            self._refresh_button.set_enabled(False)
            self._refresh_button.set_text("Refreshing...")

    def _refresh_done(self, result) -> None:
        self._refresh_button.set_enabled(True)
        self._refresh_button.set_text("Refresh reports")
        rows = []
        for path in self.state.available:
            name = Path(path).name
            stamp = name.replace(".jsonl", "").replace(".md", "")
            rows.append({"id": path, "text": f"{name}  ({stamp})"})
        self._list.set_rows(rows)
        last = (self.state.last or {}).get("jsonl") or (self.state.last or {}).get("markdown")
        last_txt = f"    Last report: {last}" if last else ""
        stale = "    Refresh failed: showing previous list." if self.state.refresh_failed else ""
        self._summary_label.config(
            text=f"Available: {len(self.state.available)}{last_txt}{stale}"
        )

    def _on_select(self, path: str) -> None:
        self.controller.preview_report(path, done=self._preview_done)

    def _preview_done(self, _result) -> None:
        if self.state.preview is not None:
            self._preview.set_text(self.state.preview or "(empty report)")

    def primary_action(self):
        if not self.controller.state.is_busy():
            self._on_refresh()
