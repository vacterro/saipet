"""GUI-only state.

This is presentation state -- which tab is open, which row is selected, what
the last explicit result was -- not a second copy of SAIPET's business logic.
The single source of business truth stays in Bridge / the core; this file
only remembers what the human is looking at.

The operation lifecycle is owned here too, but only as the observable model:
the Controller is the only writer. A view reads `state.operation` to render
busy/terminal UI; it never flips it itself.
"""


class Operation:
    """The one controller-owned operation lifecycle.

    Exactly one operation may run at a time. `state` moves IDLE -> RUNNING
    -> SUCCEEDED/FAILED and is written only by the Controller, so there is
    no path where a control enters busy UI without a matching terminal
    state.
    """

    IDLE = "idle"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"

    def __init__(self):
        self.name: str | None = None
        self.id: int = 0  # monotonic, per-operation
        self.state: str = self.IDLE
        self.result = None
        self.error: str = ""

    @property
    def running(self) -> bool:
        return self.state == self.RUNNING


class _Slot:
    """One screen's result state. Everything starts unset and is filled only
    by explicit user actions."""

    def __init__(self):
        self.result = None
        self.error = None
        self.message = None
        self.message_kind = "info"


class ScoutState(_Slot):
    def __init__(self):
        super().__init__()
        self.completed_run = False  # a successful run whose result is on screen


class QueueState(_Slot):
    def __init__(self):
        super().__init__()
        self.items: list[dict] = []
        self.selected_id: str | None = None
        # CORE-012: the selection is the canonical (source, id) pair -- ids
        # alone collide across sources.
        self.selected_source: str | None = None
        self.detail: dict | None = None
        # W2-006: the latest (source, id) clicked while an operation owned
        # the bridge, together with its callback. Served exactly once when
        # the worker settles.
        self.pending_select: tuple[str | None, str] | None = None
        self.pending_select_cb = None
        self.draft: str | None = None  # the last built draft, kept visible
        self.snapshot_stale = False  # a refresh failed; the list is previous data


class MonitorState2(_Slot):
    def __init__(self):
        super().__init__()
        self.last_cycle_at = None


class ReportsState(_Slot):
    def __init__(self):
        super().__init__()
        self.available: list[str] = []
        self.last: dict = {}
        self.selected_path: str | None = None
        self.preview: str | None = None
        self.preview_gen = 0  # monotonic; stale preview results are discarded
        self.refresh_failed = False  # a refresh failed; the list is previous data
        # W2-006: the latest report clicked while busy, together with its
        # callback. Previewed at settle so the view receives the real result.
        self.pending_preview: str | None = None
        self.pending_preview_cb = None


class SettingsState(_Slot):
    def __init__(self):
        super().__init__()
        self.saved = False
        self.applied_keys: list[str] = []


class GuiState:
    """The whole GUI's presentation state, one place, nothing duplicated."""

    def __init__(self):
        self.active_tab = "scout"
        self.operation = Operation()
        self.status_text = ""
        self.status_kind = "info"
        self.scout = ScoutState()
        self.queue = QueueState()
        self.monitor = MonitorState2()
        self.reports = ReportsState()
        self.settings = SettingsState()

    def is_busy(self) -> bool:
        return self.operation.running
