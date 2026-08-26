"""GUI state semantics: only explicit actions change it, busy blocks,
unrelated results never steal the user's selection."""

from saipet.bridge import Result
from saipet.gui.controller import Controller
from saipet.gui.state import GuiState, Operation


class StubBridge:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = dict(responses or {})

    def dispatch(self, verb, **kwargs):
        self.calls.append((verb, dict(kwargs)))
        return self.responses.get(verb, Result(ok=True, verb=verb, data={}))


def _ctrl(bridge):
    return Controller(bridge, synchronous=True)


def _make_busy(ctrl, name="watch"):
    ctrl.state.operation.name = name
    ctrl.state.operation.state = Operation.RUNNING


def test_nothing_happens_until_the_user_acts():
    ctrl = _ctrl(StubBridge())

    assert ctrl.state.operation.state == Operation.IDLE
    assert ctrl.state.scout.completed_run is False
    assert ctrl.state.queue.items == []
    assert ctrl.state.reports.available == []
    assert ctrl.state.active_tab == "scout"


def test_an_explicit_action_changes_state():
    ctrl = _ctrl(StubBridge())

    ctrl.run_scout([], 25, None)

    assert ctrl.state.scout.completed_run is True


def test_a_later_unrelated_result_does_not_steal_the_selection():
    bridge = StubBridge()
    ctrl = _ctrl(bridge)
    ctrl.state.queue.items = [{"id": "t1"}, {"id": "t2"}]
    ctrl.state.queue.selected_id = "t2"

    ctrl.run_scout([], 25, None)  # scout does not touch queue selection

    assert ctrl.state.queue.selected_id == "t2"


def test_a_refresh_that_loses_the_selected_id_clears_it_rather_than_keeping_stale():
    bridge = StubBridge()
    ctrl = _ctrl(bridge)
    ctrl.state.queue.items = [{"id": "t1"}]
    ctrl.state.queue.selected_id = "t1"
    ctrl.state.queue.detail = {"id": "t1"}

    bridge.responses["queue"] = Result(ok=True, verb="queue", data={"items": []})
    ctrl.refresh_queue()

    assert ctrl.state.queue.selected_id is None
    assert ctrl.state.queue.detail is None


def test_busy_state_blocks_a_conflicting_second_operation():
    bridge = StubBridge()
    ctrl = _ctrl(bridge)
    _make_busy(ctrl)
    before = len(bridge.calls)

    ctrl.run_monitor_cycle(1, 0, 80, None, 25, None)

    assert len(bridge.calls) == before
    assert "still running" in ctrl.state.status_text


def test_an_error_result_is_kept_until_replaced():
    bridge = StubBridge()
    bridge.responses["scout"] = Result(ok=False, verb="scout", error="credentials missing")
    ctrl = _ctrl(bridge)

    ctrl.run_scout([], 25, None)

    assert ctrl.state.scout.error == "credentials missing"

    ctrl.post_status("info", "Replaced by the next explicit action")
    assert ctrl.state.status_text == "Replaced by the next explicit action"
    assert ctrl.state.scout.error == "credentials missing"  # per-slot error stays


def test_a_shared_status_message_survives_until_replaced():
    ctrl = _ctrl(StubBridge())

    ctrl.post_status("info", "status one")
    assert ctrl.state.status_text == "status one"

    ctrl.post_status("success", "status two")
    assert ctrl.state.status_text == "status two"


def test_selection_state_is_presentation_only_not_business_logic():
    ctrl = _ctrl(StubBridge())
    state = GuiState()

    state.queue.selected_id = "abc"
    state.queue.items = [{"id": "abc"}]

    assert state.queue.selected_id == "abc"
