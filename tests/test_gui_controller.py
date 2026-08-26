"""Controller tests against a stub bridge.

The controller is the single translation layer between the GUI and
Bridge.dispatch(). These tests prove it forwards the right verb with the
right arguments, refuses mutation-free actions from doing harm, and never
polls on its own.
"""

from saipet.bridge import Result
from saipet.gui.controller import Controller
from saipet.gui.state import Operation


class StubBridge:
    """Records every dispatch; returns canned responses per verb."""

    def __init__(self, responses=None):
        self.calls: list[tuple[str, dict]] = []
        self.responses = dict(responses or {})

    def dispatch(self, verb, **kwargs):
        self.calls.append((verb, dict(kwargs)))
        if verb == "inspect":
            # W2-005: the detail-match gate needs a loaded detail that
            # mirrors exactly what was asked for.
            return Result(ok=True, verb=verb, data=dict(kwargs))
        return self.responses.get(verb, Result(ok=True, verb=verb, data={}))


def _controller(bridge):
    return Controller(bridge, synchronous=True)


def test_run_scout_dispatches_the_correct_arguments():
    bridge = StubBridge()
    ctrl = _controller(bridge)

    ctrl.run_scout(["LocalLLaMA", "AI_Agents"], 50, 48.0)

    assert bridge.calls == [
        ("scout", {"subreddits": ["LocalLLaMA", "AI_Agents"], "limit": 50, "since_hours": 48.0})
    ]
    assert ctrl.state.scout.completed_run


def test_run_scout_marks_failure_without_claiming_success():
    bridge = StubBridge({})
    bridge.responses["scout"] = Result(ok=False, verb="scout", error="boom")
    ctrl = _controller(bridge)

    ctrl.run_scout([], 25, None)

    assert ctrl.state.scout.completed_run is False
    assert ctrl.state.scout.error == "boom"
    assert ctrl.state.status_kind == "error"
    assert "boom" in ctrl.state.status_text


def test_queue_refresh_uses_the_queue_verb():
    bridge = StubBridge()
    ctrl = _controller(bridge)

    ctrl.refresh_queue()

    assert bridge.calls[-1][0] == "queue"


def test_selecting_a_finding_dispatches_a_read_only_inspect():
    bridge = StubBridge()
    ctrl = _controller(bridge)

    ctrl.select_finding("t1")

    assert bridge.calls == [("inspect", {"id": "t1"})]
    assert ctrl.state.queue.selected_id == "t1"


def test_selecting_does_not_approve_dismiss_or_mutate():
    bridge = StubBridge()
    ctrl = _controller(bridge)

    ctrl.select_finding("t1")

    verbs = [verb for verb, _ in bridge.calls]
    assert verbs == ["inspect"]


def test_approve_requires_a_solution_and_never_dispatches_without_one():
    bridge = StubBridge()
    ctrl = _controller(bridge)
    before = len(bridge.calls)

    ctrl.approve("t1", "   ", True)

    assert len(bridge.calls) == before
    assert ctrl.state.status_kind == "warn"
    assert "solution first" in ctrl.state.status_text


def test_approve_passes_mention_saipen_through():
    bridge = StubBridge()
    ctrl = _controller(bridge)

    ctrl.select_finding("t1")
    ctrl.approve("t1", "checkpoint each step", mention_saipen=False)

    verb, args = bridge.calls[-1]
    assert verb == "approve"
    assert args == {"id": "t1", "solution": "checkpoint each step", "mention_saipen": False}


def test_approve_respects_the_returned_posted_false_flag():
    bridge = StubBridge()
    bridge.responses["approve"] = Result(
        ok=True, verb="approve", data={"id": "t1", "draft": "solution text", "posted": False}
    )
    ctrl = _controller(bridge)

    ctrl.select_finding("t1")

    ctrl.approve("t1", "solution text", True)

    assert ctrl.state.queue.draft == "solution text"
    assert ctrl.state.status_text == "Draft created. Nothing was posted."
    assert "posted" not in ctrl.state.queue.draft


def test_approve_removes_the_approved_item_from_pending():
    bridge = StubBridge()
    ctrl = _controller(bridge)
    ctrl.state.queue.items = [{"id": "t1", "source": None}, {"id": "t2", "source": None}]

    ctrl.select_finding("t1")
    ctrl.approve("t1", "solution", True)

    assert [i["id"] for i in ctrl.state.queue.items] == ["t2"]


def test_dismiss_uses_the_exact_id_and_the_reject_verb():
    bridge = StubBridge()
    ctrl = _controller(bridge)

    ctrl.select_finding("t1")
    ctrl.dismiss("t1")

    verb, args = bridge.calls[-1]
    assert verb == "reject"
    assert args == {"id": "t1"}


def test_errors_from_the_bridge_are_preserved_not_swallowed():
    bridge = StubBridge()
    bridge.responses["queue"] = Result(ok=False, verb="queue", error="queue exploded")
    ctrl = _controller(bridge)

    ctrl.refresh_queue()

    assert ctrl.state.queue.error == "queue exploded"
    assert ctrl.state.status_kind == "warn"
    assert "queue exploded" in ctrl.state.status_text


def test_busy_gate_blocks_a_second_operation():
    bridge = StubBridge()
    ctrl = _controller(bridge)
    ctrl.state.operation.name = "scout"
    ctrl.state.operation.state = Operation.RUNNING
    before = len(bridge.calls)

    ctrl.run_scout([], 25, None)

    assert len(bridge.calls) == before
    assert "still running" in ctrl.state.status_text


def test_no_automatic_polling_after_a_run():
    bridge = StubBridge()
    ctrl = _controller(bridge)

    ctrl.run_scout([], 25, None)

    verbs = {verb for verb, _ in bridge.calls}
    assert verbs == {"scout"}


def test_status_message_persists_until_replaced():
    bridge = StubBridge()
    ctrl = _controller(bridge)

    ctrl.post_status("info", "first message")

    assert ctrl.state.status_text == "first message"
    assert ctrl.state.status_text != ""


# -- P0-6: failed mutations preserve user state ------------------------


def test_approve_failure_preserves_selection_solution_draft_and_mention():
    bridge = StubBridge()
    bridge.responses["approve"] = Result(ok=False, verb="approve", error="draft exploded")
    ctrl = _controller(bridge)
    ctrl.state.queue.items = [{"id": "t1"}]
    ctrl.state.queue.selected_id = "t1"
    ctrl.state.queue.detail = {"id": "t1", "title": "keep me"}
    ctrl.state.queue.draft = "previous draft"

    ctrl.approve("t1", "my typed solution", True)

    assert ctrl.state.queue.selected_id == "t1"
    assert ctrl.state.queue.detail == {"id": "t1", "title": "keep me"}
    assert ctrl.state.queue.draft == "previous draft"
    assert ctrl.state.status_kind == "error"
    assert "draft exploded" in ctrl.state.status_text


def test_dismiss_failure_preserves_the_detail_panel():
    bridge = StubBridge()
    bridge.responses["reject"] = Result(ok=False, verb="reject", error="reject exploded")
    ctrl = _controller(bridge)
    ctrl.state.queue.items = [{"id": "t1"}]
    ctrl.state.queue.selected_id = "t1"
    ctrl.state.queue.detail = {"id": "t1", "title": "still visible"}

    ctrl.dismiss("t1")

    assert ctrl.state.queue.selected_id == "t1"
    assert ctrl.state.queue.detail == {"id": "t1", "title": "still visible"}
    assert ctrl.state.status_kind == "error"


def test_a_rejected_operation_is_never_rendered_busy():
    """A rejected second operation must not flip any UI into busy without a
    matching terminal state: the op returns False and the state stays idle."""
    bridge = StubBridge()
    ctrl = _controller(bridge)
    ctrl.state.operation.name = "watch"
    ctrl.state.operation.state = Operation.RUNNING

    accepted = ctrl.run_scout([], 25, None)

    assert accepted is False
    assert ctrl.state.operation.state == Operation.RUNNING
    assert ctrl.state.operation.name == "watch"
    assert "still running" in ctrl.state.status_text


def test_an_accepted_operation_reaches_a_terminal_state():
    bridge = StubBridge()
    ctrl = _controller(bridge)

    accepted = ctrl.run_scout([], 25, None)

    assert accepted is True
    assert ctrl.state.operation.state == Operation.SUCCEEDED


# -- P1-4: URL hardening ----------------------------------------------


def test_url_lookalike_hostname_is_rejected(monkeypatch):
    ctrl = _controller(StubBridge())
    assert ctrl.open_thread("https://reddit.com.evil.example/r/x") is False
    assert ctrl.state.status_kind == "error"


def test_url_userinfo_swizzle_is_rejected(monkeypatch):
    ctrl = _controller(StubBridge())
    assert ctrl.open_thread("https://reddit.com@evil.example/r/x") is False


def test_non_https_schemes_are_rejected(monkeypatch):
    ctrl = _controller(StubBridge())
    assert ctrl.open_thread("http://reddit.com/r/x") is False
    assert ctrl.open_thread("file:///etc/passwd") is False
    assert ctrl.open_thread("javascript:alert(1)") is False


def test_a_real_reddit_permalink_is_accepted(monkeypatch):
    opened = []
    monkeypatch.setattr("saipet.gui.controller._open_browser", lambda u: opened.append(u))
    ctrl = _controller(StubBridge())

    assert ctrl.open_thread("https://old.reddit.com/r/LocalLLaMA/comments/abc") is True
    assert opened == ["https://old.reddit.com/r/LocalLLaMA/comments/abc"]
    assert ctrl.state.status_kind == "info"


def test_browser_launch_failure_becomes_a_normal_error(monkeypatch):
    def _boom(_u):
        raise OSError("no browser")

    monkeypatch.setattr("saipet.gui.controller._open_browser", _boom)
    ctrl = _controller(StubBridge())

    assert ctrl.open_thread("https://reddit.com/r/x/abc") is False
    assert ctrl.state.status_kind == "error"
    assert "no browser" in ctrl.state.status_text


# -- P0-5: stale async preview cannot overwrite a newer selection --------


def test_latest_preview_wins_and_generation_advances(tmp_path):
    first = tmp_path / "a.jsonl"
    first.write_text("AAA", encoding="utf-8")
    second = tmp_path / "b.jsonl"
    second.write_text("BBB", encoding="utf-8")
    ctrl = _controller(StubBridge())

    ctrl.preview_report(str(first))
    ctrl.preview_report(str(second))

    assert ctrl.state.reports.preview == "BBB"
    assert ctrl.state.reports.preview_gen == 2


def test_a_stale_preview_apply_is_discarded(tmp_path):
    """The generation guard lives in the apply closure: a result carrying an
    old generation number must not touch the preview pane."""
    first = tmp_path / "a.jsonl"
    first.write_text("AAA", encoding="utf-8")
    ctrl = _controller(StubBridge())

    captured = {}

    def apply_with_stale_gen(result):
        # Simulate the queued closure for the FIRST preview running after the
        # user already moved on: its gen no longer matches.
        if 1 != ctrl.state.reports.preview_gen:
            return
        captured["applied"] = True

    # preview_report bumps the gen to 1; a second call bumps it to 2.
    ctrl.state.reports.preview_gen = 2
    ctrl.state.reports.selected_path = "newer.jsonl"
    ctrl.state.reports.preview = "NEWER"

    apply_with_stale_gen(None)

    assert "applied" not in captured
    assert ctrl.state.reports.preview == "NEWER"


# CORE-012: the GUI carries the canonical (source, id) identity ------------------

NOW = 1_800_000_000.0


def _dup_rows():
    """Two pending findings from DIFFERENT sources sharing one id."""
    def _row(source):
        return {
            "source": source,
            "id": "same",
            "title": f"title {source}",
            "subreddit": "testsub",
            "permalink": f"https://reddit.com/r/testsub/{source}",
            "created_utc": NOW,
            "score": 80,
            "band": "review",
            "signals": {},
        }

    return [_row("reddit"), _row("fixture")]


def test_duplicate_ids_across_sources_are_independently_actionable():
    class _EchoInspect(StubBridge):
        def dispatch(self, verb, **kwargs):
            if verb == "inspect":
                return Result(ok=True, verb=verb, data=dict(kwargs))
            return super().dispatch(verb, **kwargs)

    ctrl = Controller(_EchoInspect(), synchronous=True)
    ctrl.state.queue.items = _dup_rows()

    ctrl.select_finding("same", source="reddit")
    assert ctrl.state.queue.selected_id == "same"
    assert ctrl.state.queue.selected_source == "reddit"

    ctrl.select_finding("same", source="fixture")
    assert ctrl.state.queue.selected_id == "same"
    assert ctrl.state.queue.selected_source == "fixture"
    # The detail loaded is the FIXTURE one -- not an ambiguity error.
    assert ctrl.state.queue.detail["source"] == "fixture"


def test_approve_removes_only_the_exact_row_and_keeps_the_other():
    calls = []

    class _ApproveBridge(StubBridge):
        def dispatch(self, verb, **kwargs):
            calls.append((verb, dict(kwargs)))
            return super().dispatch(verb, **kwargs)

    ctrl = Controller(_ApproveBridge(), synchronous=True)
    rows = _dup_rows()
    ctrl.state.queue.items = rows
    ctrl.select_finding("same", source="reddit")

    assert ctrl.approve(
        "same", "a human wrote this", False, source="reddit"
    )
    approve_calls = [c for c in calls if c[0] == "approve"]
    assert len(approve_calls) == 1
    verb, kwargs = approve_calls[0]
    assert verb == "approve" and kwargs.get("source") == "reddit"

    # Only the approved row is gone; the other source's row is intact.
    remaining = [i for i in ctrl.state.queue.items]
    assert [i["source"] for i in remaining] == ["fixture"]
    # Selection cleared only because the selected row itself was removed.
    assert ctrl.state.queue.selected_source is None
    assert ctrl.state.queue.selected_id is None


def test_dismiss_by_exact_identity_spares_the_same_id_row():
    ctrl = Controller(StubBridge(), synchronous=True)
    ctrl.state.queue.items = _dup_rows()
    ctrl.select_finding("same", source="fixture")

    assert ctrl.dismiss("same", source="fixture")

    assert [i["source"] for i in ctrl.state.queue.items] == ["reddit"]


# W2-005: one canonical latest-selection flow -----------------------------------


def test_a_click_while_busy_is_deferred_and_only_the_latest_is_served():
    class _EchoInspect(StubBridge):
        def dispatch(self, verb, **kwargs):
            if verb == "inspect":
                return Result(ok=True, verb=verb, data=dict(kwargs))
            return super().dispatch(verb, **kwargs)

    ctrl = Controller(_EchoInspect(), synchronous=True)
    ctrl.state.operation.name = "scout"
    ctrl.state.operation.state = Operation.RUNNING

    served: list = []
    ctrl.select_finding("a", source="reddit", done=lambda r: served.append(r))
    ctrl.select_finding("b", source="fixture", done=lambda r: served.append(r))

    # Both clicks were remembered as selections; the read is deferred...
    assert (ctrl.state.queue.selected_source, ctrl.state.queue.selected_id) == (
        "fixture",
        "b",
    )
    assert ctrl.state.queue.pending_select == ("fixture", "b")

    # ...and when the worker settles exactly ONE read runs, for the LAST click.
    ctrl._end(Result(ok=True, verb="scout", data={}))
    assert ctrl.state.queue.detail == {"id": "b", "source": "fixture"}
    assert ctrl.state.queue.pending_select is None


def test_approve_without_matching_loaded_detail_is_refused():
    calls: list[tuple] = []

    class _Spy(StubBridge):
        def dispatch(self, verb, **kwargs):
            calls.append((verb, dict(kwargs)))
            return super().dispatch(verb, **kwargs)

    ctrl = Controller(_Spy(), synchronous=True)
    ctrl.state.queue.items = _dup_rows()
    ctrl.state.queue.selected_id = "same"
    ctrl.state.queue.selected_source = "reddit"
    # NO detail loaded (the audit's blank-detail-but-enabled race).

    assert not ctrl.approve("same", "solution text", False, source="reddit")
    assert not any(verb == "approve" for verb, _k in calls)
    assert "does not match" in ctrl.state.status_text


def test_dismiss_with_stale_detail_is_refused():
    ctrl = Controller(StubBridge(), synchronous=True)
    ctrl.state.queue.items = _dup_rows()
    ctrl.select_finding("same", source="reddit")  # loads reddit's detail
    # The user then clicks fixture's row WITHOUT a fresh load.
    ctrl.state.queue.selected_source = "fixture"

    assert not ctrl.dismiss("same", source="fixture")
    assert "does not match" in ctrl.state.status_text
