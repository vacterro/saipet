"""Screen 2 -- Queue. The human-review screen: list, detail, draft, dismiss."""

import tkinter as tk

from saipet.gui import theme
from saipet.gui.theme import BACKGROUND, TEXT_SECONDARY
from saipet.gui.widgets import (
    GoldenCheck,
    GoldenGroup,
    FindingList,
    RaisedButton,
    SectionHeader,
    SunkenText,
)


def _format_meta(detail: dict) -> str:
    created = detail.get("created_utc")
    created_txt = ""
    if created:
        import time

        created_txt = f" created {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(created))}"
    signals = detail.get("signals") or {}
    signal_txt = "  ".join(
        f"{name}: {value:.0f}" for name, value in sorted(signals.items())
    )
    return (
        f"score {detail.get('score', 0):.0f}  {detail.get('band', '?')}  "
        f"r/{detail.get('subreddit') or '?'}  id {detail.get('id', '?')}  "
        f"{detail.get('permalink', '')}{created_txt}\n{signal_txt}"
    )


class QueueView(tk.Frame):
    def __init__(self, master, controller):
        super().__init__(master, bg=BACKGROUND)
        self.controller = controller
        self.state = controller.state.queue

        SectionHeader(self, "Queue").pack(fill="x", anchor="w", pady=(0, 2))
        tk.Label(
            self,
            text="Review findings, write a human solution, build a draft. Nothing is posted.",
            font=theme.META_FONT,
            fg=TEXT_SECONDARY,
            bg=BACKGROUND,
            anchor="w",
        ).pack(fill="x", anchor="w", pady=(0, theme.SECTION_GAP))

        summary = tk.Frame(self, bg=BACKGROUND)
        summary.pack(fill="x", pady=(0, theme.SECTION_GAP))
        self._summary_label = tk.Label(
            summary,
            text="Pending: 0",
            font=theme.BODY_FONT,
            fg=theme.TEXT_PRIMARY,
            bg=BACKGROUND,
            anchor="w",
        )
        self._summary_label.pack(side="left")
        self._refresh_button = RaisedButton(
            summary, "Refresh queue", command=self._on_refresh, width=120
        )
        self._refresh_button.pack(side="right")

        self._list = FindingList(self, height=4, on_select=self._on_select)
        self._list.pack(fill="both", expand=True, pady=(0, theme.SECTION_GAP))

        detail = GoldenGroup(self, "Finding")
        detail.pack(fill="x", pady=(0, theme.SECTION_GAP))
        self._title_label = tk.Label(
            detail.body,
            text="",
            font=theme.BODY_FONT,
            fg=theme.TEXT_PRIMARY,
            bg=theme.SURFACE_RAISED,
            anchor="w",
        )
        self._title_label.pack(fill="x", anchor="w")
        self._meta_label = tk.Label(
            detail.body,
            text="Select a finding to see its details.",
            font=theme.META_FONT,
            fg=TEXT_SECONDARY,
            bg=theme.SURFACE_RAISED,
            anchor="w",
            justify="left",
        )
        self._meta_label.pack(fill="x", anchor="w")
        tk.Label(
            detail.body,
            text="Post body",
            font=theme.META_FONT,
            fg=TEXT_SECONDARY,
            bg=theme.SURFACE_RAISED,
            anchor="w",
        ).pack(fill="x", anchor="w", pady=(2, 1))
        self._body = SunkenText(detail.body, height=2, readonly=True)
        self._body.pack(fill="x")

        solution = GoldenGroup(self, "Your solution")
        solution.pack(fill="x", pady=(0, theme.SECTION_GAP))
        self._solution = SunkenText(solution.body, height=2, readonly=False)
        self._solution.pack(fill="x")
        self._solution.text.bind("<KeyRelease>", lambda _e: self._update_action_state())
        self._mention = GoldenCheck(solution.body, "Mention SAIPEN")
        self._mention.pack(anchor="w", pady=(4, 4))
        actions = tk.Frame(solution.body, bg=theme.SURFACE_RAISED)
        actions.pack(fill="x")
        self._approve_button = RaisedButton(
            actions, "Approve & build draft", command=self._on_approve, width=170
        )
        self._approve_button.pack(side="left")
        self._dismiss_button = RaisedButton(
            actions, "Dismiss finding", command=self._on_dismiss, width=130
        )
        self._dismiss_button.pack(side="left", padx=(theme.SECTION_GAP, 0))
        self._open_button = RaisedButton(
            actions, "Open thread", command=self._on_open, width=110
        )
        self._open_button.pack(side="left", padx=(theme.SECTION_GAP, 0))
        self._copy_button = RaisedButton(
            actions, "Copy draft", command=self._on_copy, width=100, enabled=False
        )
        self._copy_button.pack(side="left", padx=(theme.SECTION_GAP, 0))
        self._reason_label = tk.Label(
            solution.body,
            text="Select a finding first.",
            font=theme.META_FONT,
            fg=theme.TEXT_MUTED,
            bg=theme.SURFACE_RAISED,
            anchor="w",
        )
        self._reason_label.pack(fill="x", anchor="w", pady=(2, 0))

        draft = GoldenGroup(self, "Draft")
        draft.pack(fill="x")
        self._draft = SunkenText(draft.body, height=2, readonly=True)
        self._draft.pack(fill="x")

        self._update_action_state()

    # -- actions -------------------------------------------------------

    def _on_refresh(self) -> None:
        accepted = self.controller.refresh_queue(done=self._refresh_done)
        if accepted:
            self._refresh_button.set_enabled(False)
            self._refresh_button.set_text("Refreshing...")
        self._update_action_state()

    def _refresh_done(self, result) -> None:
        self._refresh_button.set_enabled(True)
        self._refresh_button.set_text("Refresh queue")
        stale = "    Refresh failed: showing previous data." if self.state.snapshot_stale else ""
        self._summary_label.config(text=f"Pending: {len(self.state.items)}{stale}")
        self._list.set_rows(self.state.items)
        self._update_action_state()

    def _on_select(self, key) -> None:
        """`key` is the list's canonical row identity: (source, id)."""
        if isinstance(key, tuple):
            source, item_id = key
        else:
            source, item_id = None, key
        self.controller.select_finding(item_id, done=self._detail_done, source=source)

    def _detail_done(self, _result) -> None:
        detail = self.state.detail
        if detail is None:
            self._title_label.config(text="")
            self._meta_label.config(text="Select a finding to see its details.")
            self._body.clear()
            return
        self._title_label.config(text=detail.get("title", ""))
        self._meta_label.config(text=_format_meta(detail))
        self._body.set_text(detail.get("body", "") or "")
        self._update_action_state()

    def _on_approve(self) -> None:
        item_id = self.state.selected_id
        solution = self._solution.get_text()
        if not item_id:
            return
        if not solution.strip():
            self.controller.post_status(
                "warn",
                "Approve & build draft: enter your solution first.",
            )
            return
        accepted = self.controller.approve(
            item_id,
            solution,
            self._mention.is_checked(),
            done=self._approve_done,
            source=self.state.selected_source,
        )
        if accepted:
            self._update_action_state()

    def _approve_done(self, result) -> None:
        if result.ok:
            # Success resets the fields that logically reset.
            self._draft.set_text(self.state.draft or "")
            self._solution.clear()
            if self.state.draft:
                self._copy_button.set_enabled(True)
            self._list.set_rows(self.state.items)
            self._summary_label.config(text=f"Pending: {len(self.state.items)}")
        # On failure everything stays: finding, solution, mention, draft.
        self._update_action_state()

    def _on_dismiss(self) -> None:
        item_id = self.state.selected_id
        if not item_id:
            return
        accepted = self.controller.dismiss(
            item_id, done=self._dismiss_done, source=self.state.selected_source
        )
        if accepted:
            self._update_action_state()

    def _dismiss_done(self, result) -> None:
        if result.ok:
            self._list.set_rows(self.state.items)
            self._summary_label.config(text=f"Pending: {len(self.state.items)}")
            self._title_label.config(text="")
            self._meta_label.config(text="Select a finding to see its details.")
            self._body.clear()
        # On failure the detail panel is preserved.
        self._update_action_state()

    def _on_open(self) -> None:
        detail = self.state.detail
        if not detail or not detail.get("permalink"):
            return
        self.controller.open_thread(detail["permalink"])

    def _on_copy(self) -> None:
        if not self.state.draft:
            return
        self.clipboard_clear()
        self.clipboard_append(self.state.draft)
        self.controller.post_status("info", "Draft copied to the clipboard. Nothing was posted.")

    # -- state ---------------------------------------------------------

    def _update_action_state(self) -> None:
        busy = self.controller.state.is_busy()
        has_selection = self.state.selected_id is not None
        has_solution = bool(self._solution.get_text().strip())
        self._approve_button.set_enabled(not busy and has_selection and has_solution)
        self._dismiss_button.set_enabled(not busy and has_selection)
        self._open_button.set_enabled(not busy and has_selection)
        self._reason_label.config(
            text=(
                ""
                if not busy and has_selection and has_solution
                else (
                    "Enter your solution first."
                    if has_selection and not has_solution
                    else "Select a finding first."
                )
            )
        )

    def on_text_change(self) -> None:
        self._update_action_state()

    def primary_action(self):
        if not self.controller.state.is_busy():
            self._on_refresh()
