"""Screen 5 -- Settings. Explicit validate/save, atomic write, never a secret."""

import os
import tkinter as tk

from saipet import config
from saipet.gui import theme
from saipet.gui.theme import BACKGROUND, TEXT_SECONDARY
from saipet.gui.widgets import (
    GoldenGroup,
    LabeledField,
    RaisedButton,
    ScrollableFrame,
    SectionHeader,
    SunkenEntry,
    SunkenText,
)
from saipet.runtime_config import DEFAULT_CONFIG_PATH

_WEIGHT_LABELS = {
    "problem_match": "Problem match",
    "audience_fit": "Audience fit",
    "workflow_fit": "Workflow fit",
    "protocol_fit": "Protocol fit",
    "already_solved": "Already solved (penalty)",
}


class SettingsView(tk.Frame):
    def __init__(self, master, controller, on_saved=None, config_path=None):
        super().__init__(master, bg=BACKGROUND)
        self.controller = controller
        self.state = controller.state.settings
        self._on_saved = on_saved
        self._config_path = config_path if config_path is not None else DEFAULT_CONFIG_PATH

        SectionHeader(self, "Settings").pack(fill="x", anchor="w", pady=(0, 2))
        tk.Label(
            self,
            text="Edits the real SAIPET configuration. Nothing is applied until you save.",
            font=theme.META_FONT,
            fg=TEXT_SECONDARY,
            bg=BACKGROUND,
            anchor="w",
        ).pack(fill="x", anchor="w", pady=(0, theme.SECTION_GAP))

        # The form is genuinely taller than the 640x480 viewport, so it lives
        # in a deliberate in-view vertical scroller rather than being clipped.
        self._scroller = ScrollableFrame(self)
        self._scroller.pack(fill="both", expand=True)
        body = self._scroller.inner

        columns = tk.Frame(body, bg=BACKGROUND)
        columns.pack(fill="both", expand=True)
        left = tk.Frame(columns, bg=BACKGROUND)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, theme.SECTION_GAP))
        right = tk.Frame(columns, bg=BACKGROUND)
        right.grid(row=0, column=1, sticky="nsew")
        columns.columnconfigure(0, weight=1)
        columns.columnconfigure(1, weight=1)

        # -- left column --------------------------------------------------
        source = GoldenGroup(left, "Source")
        source.pack(fill="x", pady=(0, theme.SECTION_GAP))
        tk.Label(
            source.body,
            text="Subreddit allowlist (one per line)",
            font=theme.BODY_FONT,
            fg=theme.TEXT_PRIMARY,
            bg=theme.SURFACE_RAISED,
            anchor="w",
        ).pack(fill="x", anchor="w")
        self._allowlist = SunkenText(source.body, height=4, readonly=False)
        self._allowlist.pack(fill="x")
        self._allowlist.set_text("\n".join(sorted(config.SUBREDDIT_ALLOWLIST)))

        gates = GoldenGroup(left, "Gates")
        gates.pack(fill="x", pady=(0, theme.SECTION_GAP))
        self._gate_ignore = LabeledField(
            gates.body, "Ignore findings below", SunkenEntry, {"width": 6}
        )
        self._gate_ignore.pack(fill="x")
        self._gate_ignore.set(config.GATE_IGNORE_BELOW)
        self._gate_prioritize = LabeledField(
            gates.body, "Prioritize findings at", SunkenEntry, {"width": 6}
        )
        self._gate_prioritize.pack(fill="x", pady=(theme.SECTION_GAP, 0))
        self._gate_prioritize.set(config.GATE_PRIORITIZE_AT)
        self._notify_min = LabeledField(
            gates.body, "Notify minimum", SunkenEntry, {"width": 6}
        )
        self._notify_min.pack(fill="x", pady=(theme.SECTION_GAP, 0))
        self._notify_min.set(config.NOTIFY_MIN_SCORE)

        freshness = GoldenGroup(left, "Freshness")
        freshness.pack(fill="x")
        self._max_age = LabeledField(
            freshness.body, "Maximum age (hours)", SunkenEntry, {"width": 6}
        )
        self._max_age.pack(fill="x")
        self._max_age.set(config.MAX_AGE_HOURS)

        # -- right column --------------------------------------------------
        matching = GoldenGroup(right, "Matching")
        matching.pack(fill="x", pady=(0, theme.SECTION_GAP))
        tk.Label(
            matching.body,
            text="Symptoms (one per line)",
            font=theme.BODY_FONT,
            fg=theme.TEXT_PRIMARY,
            bg=theme.SURFACE_RAISED,
            anchor="w",
        ).pack(fill="x", anchor="w")
        self._symptoms = SunkenText(matching.body, height=4, readonly=False)
        self._symptoms.pack(fill="x")
        self._symptoms.set_text("\n".join(config.SYMPTOMS))

        scoring = GoldenGroup(right, "Scoring")
        scoring.pack(fill="x")
        self._weights: dict[str, SunkenEntry] = {}
        first = True
        for name, label in _WEIGHT_LABELS.items():
            row = tk.Frame(scoring.body, bg=theme.SURFACE_RAISED)
            row.pack(fill="x", pady=(0 if first else 4, 0))
            first = False
            tk.Label(
                row,
                text=label,
                font=theme.META_FONT,
                fg=theme.TEXT_PRIMARY,
                bg=theme.SURFACE_RAISED,
                anchor="w",
                width=22,
            ).pack(side="left", anchor="w")
            entry = SunkenEntry(row, width=6)
            entry.pack(side="right")
            entry.set(config.WEIGHTS[name])
            self._weights[name] = entry

        # -- bottom ---------------------------------------------------------
        bottom = tk.Frame(body, bg=BACKGROUND)
        bottom.pack(fill="x", pady=(theme.SECTION_GAP, 0))
        creds = GoldenGroup(bottom, "Reddit credentials")
        creds.pack(fill="x", pady=(0, theme.SECTION_GAP))
        self._creds_label = tk.Label(
            creds.body,
            text="",
            font=theme.META_FONT,
            fg=theme.TEXT_PRIMARY,
            bg=theme.SURFACE_RAISED,
            anchor="w",
            justify="left",
        )
        self._creds_label.pack(fill="x", anchor="w")
        self._render_credentials()

        self._validate_button = RaisedButton(
            bottom, "Validate settings", command=self._on_validate, width=130
        )
        self._validate_button.pack(side="left")
        self._save_button = RaisedButton(
            bottom, "Save settings", command=self._on_save, width=120
        )
        self._save_button.pack(side="left", padx=(theme.SECTION_GAP, 0))

    # -- build overrides -------------------------------------------------

    def _build_overrides(self) -> tuple[dict | None, str]:
        """Read the form into a validated-ready overrides dict. Returns
        (overrides, "") or (None, error-message)."""
        lines = self._allowlist.get_text().splitlines()
        allowlist = [line.strip() for line in lines if line.strip()]
        symptom_lines = self._symptoms.get_text().splitlines()
        symptoms = [line.strip() for line in symptom_lines if line.strip()]

        weights = {}
        for name, entry in self._weights.items():
            raw = entry.get().strip()
            if not raw:
                return None, f"Weight '{name}' is empty -- enter a number."
            try:
                value = float(raw)
            except ValueError:
                return None, f"Weight '{name}' is not a number: {raw!r}."
            weights[name] = value

        def num(label: str, raw: str) -> tuple[float | None, str]:
            if not raw:
                return None, f"{label} is empty -- enter a number."
            try:
                value = float(raw)
            except ValueError:
                return None, f"{label} is not a number: {raw!r}."
            return value, ""

        numbers = {}
        for key, label, field in (
            ("gate_ignore_below", "Ignore findings below", self._gate_ignore),
            ("gate_prioritize_at", "Prioritize findings at", self._gate_prioritize),
            ("notify_min_score", "Notify minimum", self._notify_min),
            ("max_age_hours", "Maximum age (hours)", self._max_age),
        ):
            value, err = num(label, field.get().strip())
            if err:
                return None, err
            numbers[key] = value

        overrides = {
            "symptoms": symptoms,
            "subreddit_allowlist": allowlist,
            "weights": weights,
            "gate_ignore_below": numbers["gate_ignore_below"],
            "gate_prioritize_at": numbers["gate_prioritize_at"],
            "notify_min_score": numbers["notify_min_score"],
            "max_age_hours": numbers["max_age_hours"],
        }
        return overrides, ""

    def _on_validate(self) -> None:
        overrides, err = self._build_overrides()
        if err:
            self.controller.post_status(
                "error",
                f"Cause: {err}\nEffect: the settings were not validated.\nFix: correct the invalid field and validate again.",
            )
            return
        ok, verr = self.controller.validate_settings(overrides)
        if ok:
            self.controller.post_status("info", "Settings are valid.")
        else:
            self.controller.post_status(
                "error",
                f"Cause: {verr}\nEffect: the settings are not valid.\nFix: correct the invalid field and validate again.",
            )

    def _on_save(self) -> None:
        overrides, err = self._build_overrides()
        if err:
            self.controller.post_status(
                "error",
                f"Cause: {err}\nEffect: the settings were not saved.\nFix: correct the invalid field and save again.",
            )
            return
        accepted = self.controller.save_settings(
            overrides, self._config_path, done=self._save_done
        )
        if accepted:
            self._save_button.set_enabled(False)
            self._validate_button.set_enabled(False)

    def _save_done(self, result) -> None:
        self._save_button.set_enabled(True)
        self._validate_button.set_enabled(True)
        if result.ok and self._on_saved is not None:
            self._on_saved()

    def _render_credentials(self) -> None:
        cid = "configured" if os.environ.get("REDDIT_CLIENT_ID") else "missing"
        csec = "configured" if os.environ.get("REDDIT_CLIENT_SECRET") else "missing"
        self._creds_label.config(
            text=f"Client ID: {cid}    Client secret: {csec}\n"
            "Credentials are read from the environment only. The secret is never shown or stored."
        )

    def primary_action(self):
        if not self.controller.state.is_busy():
            self._on_save()
