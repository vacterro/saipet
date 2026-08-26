"""Real Tk integration tests: actual widgets, actual geometry, real bridge.

Unit tests can all pass while Tk layout is broken. These instantiate the real
App, pump the event loop, and assert on visible widget bounds. They are the
regression net for the 640x480 contract, widget ownership, busy-state
lifecycle, async races, persistence and shutdown.

Each test creates and destroys real Tk windows. On a machine with no display
the whole module skips. Windows Tk can briefly fail to re-initialise after
many rapid create/destroy cycles, so root creation retries once after a GC
pass.
"""

import gc
import time
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace

import pytest

from saipet import config
from saipet.bridge import Bridge, Result
from saipet.gui.app import App
from saipet.gui.widgets import FindingList, GoldenScrollbar, SunkenText
from saipet.sources.base import Candidate, FixtureSource

NOW = 1_800_000_000.0
_RELEVANT_BODY = "Lost context, session state, handoff, resume, checkpoint, deterministic protocol."


def _new_root():
    try:
        return tk.Tk()
    except tk.TclError:
        gc.collect()
        time.sleep(0.1)
        return tk.Tk()


def _tk_available() -> bool:
    try:
        root = tk.Tk()
        root.destroy()
        return True
    except tk.TclError:
        return False


pytestmark = pytest.mark.skipif(not _tk_available(), reason="no display / tkinter unavailable")


@pytest.fixture(autouse=True)
def _restore_config(monkeypatch):
    """App.load_config applies overrides in place; undo them, and allow the
    test subreddit through the default-deny policy."""
    saved = (
        list(config.SYMPTOMS),
        set(config.SUBREDDIT_ALLOWLIST),
        dict(config.WEIGHTS),
        config.GATE_IGNORE_BELOW,
        config.GATE_PRIORITIZE_AT,
        config.MAX_AGE_HOURS,
    )
    monkeypatch.setattr("saipet.policy.SUBREDDIT_ALLOWLIST", {"testsub"})
    monkeypatch.setattr("saipet.config.SUBREDDIT_ALLOWLIST", {"testsub"})
    yield
    config.SYMPTOMS[:] = saved[0]
    config.SUBREDDIT_ALLOWLIST.clear()
    config.SUBREDDIT_ALLOWLIST.update(saved[1])
    config.WEIGHTS.clear()
    config.WEIGHTS.update(saved[2])
    config.GATE_IGNORE_BELOW = saved[3]
    config.GATE_PRIORITIZE_AT = saved[4]
    config.MAX_AGE_HOURS = saved[5]


def _candidate(candidate_id):
    return Candidate(
        source="fixture",
        id=candidate_id,
        title=f"agent loses context {candidate_id}",
        body=_RELEVANT_BODY,
        permalink=f"https://reddit.com/r/testsub/{candidate_id}",
        subreddit="testsub",
        created_utc=NOW - 3600,
    )


class _SlowSource(FixtureSource):
    """A fixture source that takes long enough to reliably overlap with the
    next UI action, for race/shutdown tests."""

    def fetch(self, limit=25):
        time.sleep(0.25)
        return super().fetch(limit)


def _make_bridge(tmp_path, candidates=None, review=True, source_cls=FixtureSource):
    return Bridge(
        report_dir=tmp_path / "runs",
        seen_path=tmp_path / "seen.json",
        review_path=(tmp_path / "review-state.json") if review else None,
        source_factory=lambda *_: source_cls(candidates or []),
        now_fn=lambda: NOW,
    )


@pytest.fixture
def tk_root():
    root = _new_root()
    root.update()
    yield root
    try:
        root.destroy()
    except tk.TclError:
        pass
    gc.collect()
    time.sleep(0.02)


@pytest.fixture
def app_factory(tmp_path):
    """Build Apps on isolated tmp state and guarantee they are destroyed."""
    apps = []

    def _make(candidates=None, bridge=None, config_path=None, review=True, source_cls=FixtureSource):
        if bridge is None:
            bridge = _make_bridge(tmp_path, candidates, review=review, source_cls=source_cls)
        cfg = config_path if config_path is not None else str(tmp_path / "gui.cfg.json")
        app = App(root=_new_root(), bridge=bridge, config_path=cfg)
        apps.append(app)
        return app

    yield _make
    for app in apps:
        try:
            app.close()
        except Exception:
            pass
    gc.collect()
    time.sleep(0.05)


def _pump(root, times=8, delay=0.01):
    for _ in range(times):
        root.update()
        time.sleep(delay)


def _wait_idle(app, timeout=6):
    deadline = time.time() + timeout
    while app.controller.state.is_busy() and time.time() < deadline:
        app.root.update()
        time.sleep(0.01)
    app.root.update()


def _key(keysym):
    return SimpleNamespace(keysym=keysym)


# GUI-001 -----------------------------------------------------------------


def test_gui_001_app_instantiates_and_destroys_cleanly(app_factory):
    app = app_factory()
    app.root.update_idletasks()
    app.close()
    assert True


# GUI-002 -----------------------------------------------------------------


def test_gui_002_no_horizontal_overflow_at_640x480(app_factory):
    app = app_factory()
    app.root.geometry("640x480")
    app.root.update()
    for key in ("scout", "queue", "monitor", "reports", "settings"):
        app.show_tab(key)
        app.root.update()
        content_w = app._content.winfo_width()
        widest = 0
        for child in app._content.winfo_children():
            if child.winfo_manager():
                widest = max(widest, child.winfo_reqwidth())
        assert widest <= content_w + 1, f"{key} overflows: {widest} > {content_w}"


# GUI-003 -----------------------------------------------------------------


def test_gui_003_primary_actions_stay_within_visible_bounds(app_factory):
    app = app_factory()
    app.root.geometry("640x480")
    app.root.update()
    buttons = {
        "scout": app._screens["scout"]._run_button,
        "queue": app._screens["queue"]._refresh_button,
        "monitor": app._screens["monitor"]._run_button,
        "reports": app._screens["reports"]._refresh_button,
        "settings": app._screens["settings"]._save_button,
    }
    for key, button in buttons.items():
        app.show_tab(key)
        app.root.update()
        # Settings lives in an in-view scroller; scroll to the action first.
        scroller = getattr(app._screens[key], "_scroller", None)
        if scroller is not None:
            scroller.canvas.yview_moveto(1.0)
            app.root.update()
        app.root.update_idletasks()
        left = button.winfo_rootx()
        top = button.winfo_rooty()
        right = left + button.winfo_width()
        bottom = top + button.winfo_height()
        root = app.root
        assert left >= root.winfo_rootx(), f"{key} off-left"
        assert top >= root.winfo_rooty(), f"{key} off-top"
        assert right <= root.winfo_rootx() + root.winfo_width(), f"{key} off-right"
        assert bottom <= root.winfo_rooty() + root.winfo_height(), f"{key} off-bottom"


# GUI-004 -----------------------------------------------------------------


def test_gui_004_status_region_visible_and_readable(app_factory):
    app = app_factory()
    app.root.geometry("640x480")
    app.root.update()
    status = app._status
    root = app.root
    assert status.winfo_rooty() + status.winfo_height() <= root.winfo_rooty() + root.winfo_height()
    assert status.winfo_rootx() >= root.winfo_rootx()
    status.set_message("status line", "info")
    app.root.update()
    assert "status line" in status.get_message()


# GUI-005 -----------------------------------------------------------------


def test_gui_005_labels_appear_before_their_input(app_factory):
    app = app_factory()
    app.root.update()
    pairs = [
        app._screens["scout"]._limit,
        app._screens["scout"]._since,
        app._screens["monitor"]._min_score,
        app._screens["settings"]._gate_ignore,
        app._screens["settings"]._max_age,
    ]
    for lf in pairs:
        assert lf.field.master is lf, "field must be owned by its LabeledField"
    app.root.update_idletasks()
    for lf in pairs:
        assert lf.label.winfo_rooty() <= lf.field.winfo_rooty(), lf.label.cget("text")


# GUI-006 -----------------------------------------------------------------


def test_gui_006_scrollbar_does_not_inflate_parent(tk_root):
    tk_root.update_idletasks()
    bare = tk.Text(tk_root, height=3, width=30)
    bare_height = bare.winfo_reqheight()
    st = SunkenText(tk_root, height=3)
    st_height = st.winfo_reqheight()
    sb = GoldenScrollbar(tk_root)
    sb_height = sb.winfo_reqheight()

    assert sb_height <= 4, f"scrollbar requests {sb_height}px height"
    # The scrollable area stays close to a bare Text of the same line count.
    assert st_height < bare_height + 40


# GUI-007 -----------------------------------------------------------------


def test_gui_007_button_press_does_not_change_outer_geometry(tk_root):
    from saipet.gui.widgets import RaisedButton

    button = RaisedButton(tk_root, "Run scout", width=150)
    button.pack()
    tk_root.update_idletasks()
    normal = (button.winfo_reqwidth(), button.winfo_reqheight())
    button._on_press()
    tk_root.update_idletasks()
    pressed = (button.winfo_reqwidth(), button.winfo_reqheight())
    button._on_release()
    tk_root.update_idletasks()
    released = (button.winfo_reqwidth(), button.winfo_reqheight())

    assert pressed == normal
    assert released == normal


# GUI-008 -----------------------------------------------------------------


class _GatedSource(FixtureSource):
    """Blocks its fetch on an event so a test can hold a worker in flight."""

    def __init__(self, candidates, gate):
        super().__init__(candidates)
        self._gate = gate

    def fetch(self, limit=25):
        self._gate.wait(5)
        return super().fetch(limit)


def test_gui_008_cross_tab_busy_never_permanently_disables(app_factory, tmp_path):
    import threading

    gate = threading.Event()
    bridge = _make_bridge(tmp_path, [_candidate("t1")],
                          source_cls=lambda c: _GatedSource(c, gate))
    app = app_factory(bridge=bridge)
    app.root.update()
    scout_view = app._screens["scout"]
    monitor_view = app._screens["monitor"]

    scout_view._on_run()
    assert app.controller.state.is_busy()

    app.show_tab("monitor")
    app.root.update()
    monitor_view._on_run()  # rejected: scout is still running

    assert app.controller.state.operation.name == "scout"
    assert monitor_view._run_button.get_text() == "Run one monitor cycle"

    gate.set()  # let the blocked scout finish
    _wait_idle(app)
    app.root.update()

    assert monitor_view._run_button.is_enabled() is True
    assert monitor_view._run_button.get_text() == "Run one monitor cycle"


# GUI-009 -----------------------------------------------------------------


def test_gui_009_rapid_selection_detail_matches_highlight(app_factory):
    app = app_factory(candidates=[_candidate("a"), _candidate("b")])
    app.root.update()
    scout_view = app._screens["scout"]
    scout_view._subreddits.set_text("testsub")
    scout_view._on_run()
    _wait_idle(app)

    queue_view = app._screens["queue"]
    queue_view._on_refresh()
    _wait_idle(app)
    assert queue_view._list.count() == 2

    queue_view._list._on_select("a")
    app.root.update()
    title_a = queue_view._title_label.cget("text")
    queue_view._list._on_select("b")
    app.root.update()

    assert queue_view.state.selected_id == "b"
    assert queue_view._title_label.cget("text") == _candidate("b").title
    assert title_a == _candidate("a").title


# GUI-010 -----------------------------------------------------------------


def test_gui_010_approve_failure_preserves_typed_solution(app_factory, tmp_path):
    class _FailApprove(Bridge):
        def dispatch(self, verb, **kwargs):
            if verb == "approve":
                return Result(ok=False, verb="approve", error="draft exploded")
            return super().dispatch(verb, **kwargs)

    failing = _FailApprove(
        report_dir=tmp_path / "runs",
        seen_path=tmp_path / "seen.json",
        review_path=None,
        source_factory=lambda *_: FixtureSource([_candidate("t1")]),
        now_fn=lambda: NOW,
    )
    app = app_factory(bridge=failing)
    app.root.update()
    scout_view = app._screens["scout"]
    scout_view._subreddits.set_text("testsub")
    scout_view._on_run()
    _wait_idle(app)

    queue_view = app._screens["queue"]
    queue_view._on_refresh()
    _wait_idle(app)
    queue_view._list._on_select(("fixture", "t1"))
    app.root.update()
    queue_view._solution.set_text("my carefully typed solution")
    queue_view._update_action_state()

    queue_view._on_approve()
    _wait_idle(app)

    assert queue_view._solution.get_text() == "my carefully typed solution"
    assert queue_view.state.selected_id == "t1"
    assert queue_view._title_label.cget("text") != ""
    assert app.controller.state.status_kind == "error"


# GUI-011 -----------------------------------------------------------------


def test_gui_011_reject_failure_preserves_selected_finding(app_factory, tmp_path):
    class _FailReject(Bridge):
        def dispatch(self, verb, **kwargs):
            if verb == "reject":
                return Result(ok=False, verb="reject", error="reject exploded")
            return super().dispatch(verb, **kwargs)

    failing = _FailReject(
        report_dir=tmp_path / "runs",
        seen_path=tmp_path / "seen.json",
        review_path=None,
        source_factory=lambda *_: FixtureSource([_candidate("t1")]),
        now_fn=lambda: NOW,
    )
    app = app_factory(bridge=failing)
    app.root.update()
    scout_view = app._screens["scout"]
    scout_view._subreddits.set_text("testsub")
    scout_view._on_run()
    _wait_idle(app)

    queue_view = app._screens["queue"]
    queue_view._on_refresh()
    _wait_idle(app)
    queue_view._list._on_select(("fixture", "t1"))
    app.root.update()
    title_before = queue_view._title_label.cget("text")

    queue_view._on_dismiss()
    _wait_idle(app)

    assert queue_view.state.selected_id == "t1"
    assert queue_view._title_label.cget("text") == title_before
    assert queue_view._meta_label.cget("text") != "Select a finding to see its details."


# GUI-012 -----------------------------------------------------------------


def test_gui_012_saved_settings_survive_app_restart(app_factory, tmp_path):
    cfg = tmp_path / "gui.cfg.json"
    first = app_factory(config_path=str(cfg))
    settings = first._screens["settings"]
    settings._max_age.set(36)
    settings._on_save()
    _wait_idle(first)
    assert first.controller.state.settings.saved is True
    first.close()

    second = app_factory(config_path=str(cfg))
    assert float(second._screens["settings"]._max_age.get()) == 36.0
    assert config.MAX_AGE_HOURS == 36


# GUI-013 -----------------------------------------------------------------


def test_gui_013_invalid_persisted_config_shows_error_without_partial_apply(app_factory, tmp_path):
    cfg = tmp_path / "gui.cfg.json"
    cfg.write_text("{broken json", encoding="utf-8")

    app = app_factory(config_path=str(cfg))
    app.root.update()

    status = app.controller.state.status_text
    assert "invalid" in status.lower()
    assert "not applied" in status.lower()
    assert app._screens["settings"]._max_age.get() == str(config.MAX_AGE_HOURS)


# GUI-015 -----------------------------------------------------------------


def test_gui_015_browser_launch_error_is_handled(app_factory, monkeypatch):
    monkeypatch.setattr("saipet.gui.controller._open_browser", lambda _u: (_ for _ in ()).throw(OSError("no browser")))
    app = app_factory(candidates=[_candidate("t1")])
    app.root.update()
    scout_view = app._screens["scout"]
    scout_view._subreddits.set_text("testsub")
    scout_view._on_run()
    _wait_idle(app)

    queue_view = app._screens["queue"]
    queue_view._on_refresh()
    _wait_idle(app)
    queue_view._list._on_select(("fixture", "t1"))
    app.root.update()

    queue_view._on_open()

    assert app.controller.state.status_kind == "error"
    assert "no browser" in app.controller.state.status_text


# GUI-016 -----------------------------------------------------------------


def test_gui_016_arrow_navigation_keeps_selection_visible(tk_root):
    rows = [
        {"id": f"r{i}", "score": 90, "band": "priority", "subreddit": "s", "title": f"item {i}"}
        for i in range(50)
    ]
    widget = FindingList(tk_root, height=4, on_select=None)
    widget.pack()
    widget.set_rows(rows)
    tk_root.update()
    widget._canvas.focus_set()

    widget._on_key(_key("End"))
    tk_root.update()

    viewport = widget._canvas.winfo_height()
    visible_top = widget._canvas.canvasy(0)
    visible_bottom = widget._canvas.canvasy(viewport)
    row_top = widget._active * 18
    row_bottom = row_top + 18
    assert widget._active == 49
    assert visible_top <= row_top, "active row scrolled above the viewport"
    assert row_bottom <= visible_bottom + 1, "active row scrolled below the viewport"

    widget._on_key(_key("Home"))
    tk_root.update()
    assert widget._active == 0
    assert widget._canvas.canvasy(0) == 0


# GUI-017 -----------------------------------------------------------------


def test_gui_017_close_during_every_async_operation(app_factory):
    operations = []

    def _scout_app():
        return app_factory(candidates=[_candidate("t1")], source_cls=_SlowSource)

    app = _scout_app()
    app._screens["scout"]._on_run()
    operations.append(app)

    for app in operations:
        app.close()  # must not raise
        app.controller.drain_results()  # late results are discarded, not delivered

    # Monitor cycle too (bounded, slow-ish).
    app = _scout_app()
    app.show_tab("monitor")
    app.root.update()
    app._screens["monitor"]._on_run()
    app.close()
    app.controller.drain_results()

    # Settings save.
    app = _scout_app()
    app.show_tab("settings")
    app.root.update()
    app._screens["settings"]._on_save()
    app.close()
    app.controller.drain_results()

    assert True


# GUI-018 -----------------------------------------------------------------


def test_gui_018_pending_review_item_survives_restart_via_app(app_factory, tmp_path):
    app = app_factory(candidates=[_candidate("t1")])
    scout_view = app._screens["scout"]
    scout_view._subreddits.set_text("testsub")
    scout_view._on_run()
    _wait_idle(app)
    app.close()

    second = app_factory(candidates=[])
    queue_view = second._screens["queue"]
    queue_view._on_refresh()
    _wait_idle(second)
    assert queue_view._list.count() == 1
    assert queue_view.state.items[0]["id"] == "t1"


# PERF-006: closing must never freeze the UI thread -----------------------------


def test_gui_perf_close_returns_to_the_loop_within_budget(app_factory):
    """PERF-006: close() used to join the worker synchronously on the Tk
    thread -- up to the 10s deadline of visible freeze mid-fetch. close() now
    returns immediately; the after-poller destroys once the worker settles."""
    import threading as _threading

    class _SlowBridge(Bridge):
        def dispatch(self, verb, **kwargs):
            if verb == "scout":
                time.sleep(2.0)
            return super().dispatch(verb, **kwargs)

    app = app_factory(candidates=[])
    base = app.controller.bridge

    slow = _SlowBridge(
        report_dir=base.report_dir,
        seen_path=base.seen.path,
        review_path=None,
        source_factory=lambda *_a: FixtureSource([_candidate("t1")]),
        now_fn=lambda: NOW,
    )
    slow.cancel_event = _threading.Event()
    app.controller.bridge = slow

    assert app.controller.run_scout(subreddits=["testsub"], limit=25, since_hours=None)
    time.sleep(0.05)  # the worker is now inside the 2s scout

    t0 = time.monotonic()
    app.close()
    elapsed = time.monotonic() - t0
    assert elapsed < 0.5, f"close() blocked the UI thread for {elapsed:.3f}s"

    # Drive the shutdown poller by hand: update() services paint/input but
    # not `after` timers, so a real mainloop normally drives these steps.
    deadline = time.monotonic() + 8
    destroyed = False
    while time.monotonic() < deadline:
        try:
            app._await_worker()
            alive = bool(app.root.winfo_exists())
        except tk.TclError:
            alive = False
        if not alive:
            destroyed = True
            break
        time.sleep(0.05)
    assert destroyed, "window was never destroyed"
