import os
import time
import uuid

import pytest

from saipet.bridge import Bridge
from saipet.jsonio import InterProcessLock
from saipet.monitor import MonitorState, run_monitor
from saipet.notify import ConsoleNotifier, Notification, Notifier
from saipet.sources.base import Candidate, FixtureSource

NOW = 1_800_000_000.0
_RELEVANT_BODY = "Lost context, session state, handoff, resume, checkpoint, deterministic protocol."


class _Collector(Notifier):
    def __init__(self):
        self.sent: list[Notification] = []

    def send(self, notification):
        self.sent.append(notification)


def _candidate(candidate_id):
    return Candidate(
        source="fixture",
        id=candidate_id,
        title=f"agent keeps losing context {candidate_id}",
        body=_RELEVANT_BODY,
        permalink=f"https://reddit.com/r/testsub/{candidate_id}",
        subreddit="testsub",
        created_utc=NOW - 3600,
    )


@pytest.fixture(autouse=True)
def _notify_everything_queued(monkeypatch):
    """These tests are about the loop, not the notify bar (T-017 owns that),
    so the bar is dropped to 0 and every queued item counts as a finding."""
    monkeypatch.setattr("saipet.config.NOTIFY_MIN_SCORE", 0)


@pytest.fixture
def make_bridge(tmp_path, monkeypatch):
    monkeypatch.setattr("saipet.policy.SUBREDDIT_ALLOWLIST", {"testsub"})
    monkeypatch.setattr("saipet.config.SUBREDDIT_ALLOWLIST", {"testsub"})

    def _make(batches):
        """`batches` is a list of candidate lists, one per fetch call."""
        remaining = list(batches)

        def _factory(*_args):
            return FixtureSource(remaining.pop(0) if remaining else [])

        return Bridge(
            report_dir=tmp_path / "runs",
            seen_path=tmp_path / "seen.json",
            source_factory=_factory,
            now_fn=lambda: NOW,
        )

    return _make


def test_three_bounded_cycles_notify_and_sleep(make_bridge):
    bridge = make_bridge([[_candidate("a")], [_candidate("b")], [_candidate("c")]])
    sink = _Collector()
    sleeps: list[float] = []

    state = run_monitor(
        bridge,
        notifier=sink,
        interval_seconds=900,
        cycles=3,
        sleep_fn=sleeps.append,
        now_fn=lambda: NOW,
    )

    assert state.cycles == 3
    assert state.notified_total == 3
    assert [n.data["id"] for n in sink.sent] == ["a", "b", "c"]
    assert sleeps == [900, 900]  # no nap after the final cycle


def test_a_single_cycle_does_not_sleep_at_all(make_bridge):
    """`--cycles 1` that naps first feels broken."""
    sleeps: list[float] = []

    run_monitor(
        make_bridge([[_candidate("a")]]),
        cycles=1,
        sleep_fn=sleeps.append,
        now_fn=lambda: NOW,
    )

    assert sleeps == []


def test_an_already_seen_thread_is_not_notified_twice(make_bridge):
    """The seen-gate is the dedup: the same candidate returned on cycle 2
    never reaches the queue, so the monitor needs no store of its own."""
    bridge = make_bridge([[_candidate("a")], [_candidate("a")]])
    sink = _Collector()

    state = run_monitor(
        bridge, notifier=sink, cycles=2, sleep_fn=lambda _s: None, now_fn=lambda: NOW
    )

    assert state.notified_total == 1
    assert len(sink.sent) == 1


def test_a_quiet_cycle_notifies_nothing(make_bridge):
    sink = _Collector()

    state = run_monitor(
        make_bridge([[]]), notifier=sink, cycles=1, sleep_fn=lambda _s: None, now_fn=lambda: NOW
    )

    assert state.cycles == 1
    assert state.notified_total == 0
    assert sink.sent == []


def test_every_finding_carries_the_permalink_a_human_needs(make_bridge):
    sink = _Collector()

    run_monitor(
        make_bridge([[_candidate("a")]]),
        notifier=sink,
        cycles=1,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )

    assert sink.sent[0].data["permalink"] == "https://reddit.com/r/testsub/a"
    assert sink.sent[0].kind == "finding"


def test_the_state_object_is_the_callers_and_gets_updated_in_place(make_bridge):
    """A driving agent holds this while the loop is still running."""
    state = MonitorState()

    returned = run_monitor(
        make_bridge([[_candidate("a")]]),
        cycles=1,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
        state=state,
    )

    assert returned is state
    assert state.last_cycle_at == NOW
    assert state.as_dict()["cycles"] == 1


def test_scout_args_reach_the_bridge(make_bridge):
    bridge = make_bridge([[_candidate("a")]])
    seen_args = {}
    original = bridge.dispatch

    def _record(verb, **args):
        if verb == "scout":
            seen_args.update(args)
        return original(verb, **args)

    bridge.dispatch = _record

    run_monitor(
        bridge,
        cycles=1,
        scout_args={"limit": 7, "subreddits": ["testsub"]},
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )

    assert seen_args == {"limit": 7, "subreddits": ["testsub"]}


def test_no_notifier_is_a_legal_run(make_bridge):
    state = run_monitor(
        make_bridge([[_candidate("a")]]), cycles=1, sleep_fn=lambda _s: None, now_fn=lambda: NOW
    )
    assert state.notified_total == 1


def test_the_console_sink_works_end_to_end(make_bridge):
    lines: list[str] = []

    run_monitor(
        make_bridge([[_candidate("a")]]),
        notifier=ConsoleNotifier(print_fn=lines.append),
        cycles=1,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )

    assert len(lines) == 1
    assert "agent keeps losing context a" in lines[0]


# CORE-003: durable-status persistence is not a fatal failure point ------------


def test_a_failed_status_write_does_not_kill_the_loop(make_bridge, monkeypatch, tmp_path):
    """CORE-003: _write_status sat outside the per-cycle error handling, so
    one failed status write (full disk, permission glitch, failed replace)
    propagated and terminated an unattended monitor. The loop must survive,
    keep cycling, expose the error on the state, and recover the record."""
    from saipet import monitor as mon

    status_file = tmp_path / "monitor-status.json"
    seen_payloads: list[dict] = []
    calls = {"n": 0}
    real_write = mon.atomic_write_json

    def flaky(path, data):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated status disk failure")
        real_write(path, data)
        seen_payloads.append(dict(data))

    monkeypatch.setattr(mon, "atomic_write_json", flaky)

    state = run_monitor(
        make_bridge([[]]),
        cycles=3,
        heartbeat_every=0,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
        status_path=str(status_file),
    )

    assert state.cycles == 3  # the OSError never escaped
    # Three per-cycle records + one final stopped record (W2-002).
    assert calls["n"] == 4
    # Cycle 1's failure is observable in cycle 2's persisted record...
    assert seen_payloads[0]["status_write_error"].startswith("OSError:")
    # ...and the successful rewrite restores a clean record.
    assert seen_payloads[-1]["status_write_error"] == ""
    assert state.status_write_error == ""
    import json as _json

    final = _json.loads(status_file.read_text(encoding="utf-8"))
    assert final["cycles"] == 3
    assert final["lifecycle"] == "stopped"
    assert final["next_cycle_at"] is None
    assert final["status_write_error"] == ""


# CORE-004 / W2-004: durable delivery outbox + notifier-independent results ----


class _Broken(Notifier):
    def send(self, notification):
        raise OSError("sink down")


def test_a_failed_finding_still_reaches_the_cycle_result(make_bridge):
    """W2-004: discovery is not coupled to delivery. A qualifying finding
    belongs in the structured result even when its notification failed."""
    state = run_monitor(
        make_bridge([[_candidate("a")]]),
        notifier=_Broken(),
        cycles=1,
        min_score=0,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )

    assert [i["id"] for i in state.last_run_findings] == ["a"]
    assert state.notified_total == 0
    assert state.notify_failed_total == 1


def test_heartbeat_failures_are_accounted_too(make_bridge):
    """W2-004: heartbeat/degraded/error sends went through _send_quietly with
    the Delivery discarded -- a dead sink looked healthy at the monitor
    level. Every send accounts now."""
    state = run_monitor(
        make_bridge([[]]),
        notifier=_Broken(),
        cycles=1,
        heartbeat_every=1,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )

    assert state.notify_failed_total >= 1
    assert "sink down" in state.last_notify_error


def test_outbox_retries_across_restart_and_clears_on_success(make_bridge, tmp_path):
    """CORE-004: a transient sink failure used to lose the alert forever --
    seen-state suppressed rediscovery and nothing retried the delivery. The
    obligation survives the process and drains on the next run."""
    from saipet.outbox import DeliveryOutbox

    outbox_path = tmp_path / "notify" / "feed.outbox.json"
    outbox = DeliveryOutbox(outbox_path)

    first = run_monitor(
        make_bridge([[_candidate("a")]]),
        notifier=_Broken(),
        cycles=1,
        min_score=0,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
        outbox=outbox,
    )
    assert first.notified_total == 0
    pending = DeliveryOutbox(outbox_path).obligations()
    assert [(e["source"], e["id"]) for e in pending] == [("fixture", "a")]
    # The obligation carries enough to redeliver without re-scouting.
    assert pending[0]["item"]["id"] == "a"

    # Restart: a healthy sink drains the stale obligation on cycle start.
    second = run_monitor(
        make_bridge([[]]),
        notifier=None,  # NullNotifier delivers
        cycles=1,
        min_score=0,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
        outbox=DeliveryOutbox(outbox_path),
    )
    assert second.notified_total == 1
    assert DeliveryOutbox(outbox_path).obligations() == []


def test_retry_never_duplicates_an_already_delivered_sink(make_bridge, tmp_path):
    """CORE-004, multi-sink granularity: only the FAILED sink retries; the
    healthy half of a partial delivery is not sent twice."""
    from saipet.notify import ConsoleNotifier, Delivery, FileNotifier, MultiNotifier
    from saipet.outbox import DeliveryOutbox

    calls = {"file": 0}

    class SpyFile(FileNotifier):
        def send(self, notification):
            calls["file"] += 1
            return super().send(notification)

    class MoodyConsole(ConsoleNotifier):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.fail = True

        def send(self, notification):
            if self.fail:
                return Delivery(delivered=False, failures=(("MoodyConsole", "nope"),))
            return super().send(notification)

    outbox_path = tmp_path / "feed.outbox.json"
    feed = tmp_path / "feed.jsonl"

    # Round 1: file accepts, console refuses.
    moody = MoodyConsole(print_fn=lambda _l: None)
    run_monitor(
        make_bridge([[_candidate("a")]]),
        notifier=MultiNotifier(SpyFile(feed), moody),
        cycles=1,
        min_score=0,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
        outbox=DeliveryOutbox(outbox_path),
    )
    assert calls["file"] == 1
    entry = DeliveryOutbox(outbox_path).obligations()[0]
    assert entry["confirmed"] == ["SpyFile"]
    assert any(name == "MoodyConsole" for name, _r in entry.get("failed", []))

    # Round 2: console recovers. The file sink must NOT be called again.
    recovered = MoodyConsole(print_fn=lambda _l: None)
    recovered.fail = False
    second = run_monitor(
        make_bridge([[]]),
        notifier=MultiNotifier(SpyFile(feed), recovered),
        cycles=1,
        min_score=0,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
        outbox=DeliveryOutbox(outbox_path),
    )
    assert calls["file"] == 1, "healthy sink was redelivered"
    assert second.notified_total == 1
    assert DeliveryOutbox(outbox_path).obligations() == []


# CORE-010 / W2-002: status wiring + lifecycle ---------------------------------


def test_success_history_survives_a_later_failure(tmp_path, monkeypatch):
    """W2-002: last_success_at was derived from current health -- one failed
    cycle erased the memory of every good one."""
    monkeypatch.setattr("saipet.policy.SUBREDDIT_ALLOWLIST", {"testsub"})
    monkeypatch.setattr("saipet.config.SUBREDDIT_ALLOWLIST", {"testsub"})

    calls = {"n": 0}

    def factory(*_a):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("source blew up")
        return FixtureSource([])

    bridge = Bridge(
        report_dir=tmp_path / "runs",
        seen_path=tmp_path / "seen.json",
        source_factory=factory,
        now_fn=lambda: NOW,
    )
    state = run_monitor(
        bridge,
        cycles=2,
        heartbeat_every=0,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )

    assert state.cycles == 2
    assert state.health == "failed"
    assert state.last_success_at is not None  # cycle 1's success remembered


def test_stopped_record_and_liveness_reading(make_bridge, tmp_path):
    """W2-002: a bounded run publishes a final stopped record; a reader
    trusts lifecycle+PID, never a future next_cycle_at."""
    from saipet.bridge import Bridge as B

    status_file = tmp_path / "monitor-status.json"
    run_monitor(
        make_bridge([[_candidate("a")]]),
        cycles=1,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
        status_path=str(status_file),
    )

    import json as _json
    import os as _os

    raw = _json.loads(status_file.read_text(encoding="utf-8"))
    assert raw["lifecycle"] == "stopped"
    assert raw["next_cycle_at"] is None

    fresh = B(status_path=str(status_file), now_fn=lambda: NOW)
    record = fresh._durable_monitor_status()
    assert record["live"] is False  # stopped is stopped, even with our own live PID


def test_running_record_with_dead_pid_reads_not_live(tmp_path):
    """W2-002: a terminated daemon's leftover running-record must not read
    healthy/live to a fresh Bridge."""
    import json as _json
    import subprocess
    import sys

    from saipet.bridge import Bridge as B

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    dead_pid = proc.pid
    proc.terminate()
    proc.wait(10)

    status_file = tmp_path / "monitor-status.json"
    future = NOW + 10_000
    status_file.write_text(
        _json.dumps(
            {
                "pid": dead_pid,
                "lifecycle": "running",
                "health": "healthy",
                "next_cycle_at": future,
            }
        ),
        encoding="utf-8",
    )
    record = Bridge(status_path=str(status_file), now_fn=lambda: NOW)._durable_monitor_status()
    assert record["live"] is False


# PERF-001: the daemon keeps no lifetime findings collector ---------------------


class _StopLoop(Exception):
    pass


def test_daemon_mode_retains_no_lifetime_findings_list(make_bridge):
    """PERF-001: run_findings was allocated once and appended forever in
    cycles=None mode -- a second unbounded collector beside the capped
    history (1000 cycles -> tracker.findings 200 but last_run_findings 1000).
    Daemon mode now retains only the bounded history."""
    from saipet.monitor import MonitorState

    state = MonitorState()
    calls = {"n": 0}

    def _die_after_two_sleeps(_seconds):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise _StopLoop

    with pytest.raises(_StopLoop):
        run_monitor(
            make_bridge([[_candidate("c0")], [_candidate("c1")]]),
            cycles=None,
            min_score=0,
            sleep_fn=_die_after_two_sleeps,
            now_fn=lambda: NOW,
            state=state,
        )

    # Both cycles' deliveries live only in the bounded diagnostic history.
    assert [i["id"] for i in state.findings] == ["c0", "c1"]
    # No lifetime return collector is retained in daemon mode.
    assert state.last_run_findings == []


def test_a_bounded_run_returns_exactly_its_own_findings(make_bridge):
    """PERF-001 guardrail: bounded runs still return every finding that call
    produced."""
    batches = [[_candidate(f"c{i}")] for i in range(5)]
    state = run_monitor(
        make_bridge(batches),
        cycles=5,
        min_score=0,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )
    assert [item["id"] for item in state.last_run_findings] == [
        "c0",
        "c1",
        "c2",
        "c3",
        "c4",
    ]


# PERF-006: cooperative cancellation between cycles -----------------------------


def test_cancel_fn_stops_the_loop_between_cycles(make_bridge):
    """PERF-006: a shutting-down GUI sets the bridge cancel event; the loop
    honours it at the cycle boundary -- never mid-write -- so even an
    unbounded daemon winds down cleanly."""
    calls = {"n": 0}

    def _cancel_after_first():
        calls["n"] += 1
        return calls["n"] > 1  # False on the first check, True afterwards

    state = run_monitor(
        make_bridge([[_candidate("a")]]),
        cycles=None,
        min_score=0,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
        cancel_fn=_cancel_after_first,
    )

    assert state.cycles == 1  # stopped at the boundary, not mid-cycle
    assert state.notified_total == 1


# T-028: single-instance monitor lock ---------------------------------------


def _monitor_args(tmp_path, lock_path):
    return [
        "--fixture",
        "--cycles",
        "1",
        "--limit",
        "5",
        "--lock-file",
        str(lock_path),
        "--notify-file",
        str(tmp_path / "feed.jsonl"),
        "--status-file",
        str(tmp_path / "status.json"),
        "--report-dir",
        str(tmp_path / "runs"),
    ]


def test_monitor_starts_when_the_lock_is_free(tmp_path, monkeypatch):
    """T-028: with no competing holder the daemon claims the singleton and
    releases it on a clean bounded exit."""
    import saipet.monitor as monitor

    monkeypatch.chdir(tmp_path)
    lock_path = tmp_path / "mon.lock"
    state = monitor.main(_monitor_args(tmp_path, lock_path))
    assert isinstance(state, MonitorState)
    # The lock (stored at `.<name>.lock`, jsonio._lock_path_for) must not
    # linger after the run ends.
    assert not lock_path.with_name(f".{lock_path.name}.lock").exists()


def test_monitor_refuses_when_another_instance_holds_the_lock(tmp_path, monkeypatch):
    """T-028: a live holder (here, the test process itself) makes a second
    start refuse immediately rather than share seen.json / the feed / inbox."""
    import saipet.monitor as monitor

    monkeypatch.chdir(tmp_path)
    lock_path = tmp_path / "mon.lock"
    holder = InterProcessLock(lock_path)
    holder.__enter__()
    try:
        with pytest.raises(SystemExit):
            monitor.main(_monitor_args(tmp_path, lock_path))
    finally:
        holder.__exit__(None, None, None)


def test_monitor_reclaims_the_lock_from_a_dead_holder(tmp_path, monkeypatch):
    """T-028: a lock left behind by a dead PID (crashed process) is reclaimed,
    not treated as a live peer."""
    import saipet.monitor as monitor

    monkeypatch.chdir(tmp_path)
    lock_path = tmp_path / "mon.lock"
    actual = lock_path.with_name(f".{lock_path.name}.lock")
    # A stale lock whose recorded PID cannot be alive.
    dead_pid = os.getpid() + 1_000_000
    actual.write_text(f"{dead_pid} {uuid.uuid4().hex}\n", encoding="utf-8")
    stale = time.time() - 10
    os.utime(actual, (stale, stale))
    state = monitor.main(_monitor_args(tmp_path, lock_path))
    assert isinstance(state, MonitorState)
    assert not actual.exists()


# T-031: config hot-reload between cycles -------------------------------------


def test_editing_config_between_cycles_changes_behaviour(tmp_path, monkeypatch):
    """T-031: the monitor re-reads saipet.config.json each cycle, so lowering
    notify_min_score mid-run makes a previously-below-bar finding notify on the
    NEXT cycle, without a restart."""
    import json as _json
    import time as _time

    from saipet import config as _cfg
    from saipet.bridge import Bridge
    from saipet.monitor import run_monitor
    from saipet.notify import FileNotifier
    from saipet.sources.base import Candidate, FixtureSource

    monkeypatch.chdir(tmp_path)
    original = _cfg.NOTIFY_MIN_SCORE
    monkeypatch.setattr("saipet.policy.SUBREDDIT_ALLOWLIST", {"x"})
    monkeypatch.setattr("saipet.config.SUBREDDIT_ALLOWLIST", {"x"})
    try:
        config_path = tmp_path / "saipet.config.json"
        config_path.write_text(_json.dumps({"notify_min_score": 100}))

        class _Rotating(FixtureSource):
            _n = 0

            def __init__(self):
                super().__init__([])

            def fetch(self, limit=25):
                _Rotating._n += 1
                return [
                    Candidate(
                        source="fixture",
                        id=f"c{_Rotating._n}",
                        title="lost context ai agent pipeline protocol",
                        body="",
                        permalink="",
                        subreddit="x",
                        created_utc=_time.time(),
                    )
                ]

        bridge = Bridge(source_factory=lambda targets, symptoms: _Rotating())
        notifier = FileNotifier(tmp_path / "feed.jsonl")

        calls = {"n": 0}

        def _reload_hook(_seconds):
            calls["n"] += 1
            if calls["n"] == 1:  # after cycle 1: drop the bar to 0
                config_path.write_text(_json.dumps({"notify_min_score": 0}))

        state = run_monitor(
            bridge,
            notifier=notifier,
            cycles=2,
            min_score=None,  # force live config.NOTIFY_MIN_SCORE read
            sleep_fn=_reload_hook,
            now_fn=_time.time,
            config_path=config_path,
        )

        # Cycle 1 kept the ~68-score candidate (review band, under the 100 bar)
        # out of the notify set; after the edit cycle 2's 0 bar let it through.
        # Exactly one notification total.
        assert state.notified_total == 1
        # The reload hook fires on the single inter-cycle sleep (2 cycles -> 1
        # sleep), and its edit took effect on cycle 2.
        assert calls["n"] == 1
        assert state.config_reload_error == ""
    finally:
        _cfg.NOTIFY_MIN_SCORE = original


def test_invalid_config_between_cycles_is_observed_not_fatal(tmp_path, monkeypatch):
    """T-031: a config file that no longer parses is recorded on the state and
    leaves the previous (valid) config in force -- the daemon does not crash or
    half-apply it."""
    import json as _json
    import time as _time

    from saipet import config as _cfg
    from saipet.bridge import Bridge
    from saipet.monitor import run_monitor
    from saipet.notify import FileNotifier
    from saipet.sources.base import Candidate, FixtureSource

    monkeypatch.chdir(tmp_path)
    original = _cfg.NOTIFY_MIN_SCORE
    monkeypatch.setattr("saipet.policy.SUBREDDIT_ALLOWLIST", {"x"})
    monkeypatch.setattr("saipet.config.SUBREDDIT_ALLOWLIST", {"x"})
    try:
        config_path = tmp_path / "saipet.config.json"
        config_path.write_text(_json.dumps({"notify_min_score": 0}))

        class _Rotating(FixtureSource):
            _n = 0

            def __init__(self):
                super().__init__([])

            def fetch(self, limit=25):
                _Rotating._n += 1
                return [
                    Candidate(
                        source="fixture",
                        id=f"c{_Rotating._n}",
                        title="lost context ai agent pipeline protocol",
                        body="",
                        permalink="",
                        subreddit="x",
                        created_utc=_time.time(),
                    )
                ]

        bridge = Bridge(source_factory=lambda targets, symptoms: _Rotating())
        notifier = FileNotifier(tmp_path / "feed.jsonl")

        def _break_hook(_seconds):
            # First sleep: corrupt the file; never fix it.
            config_path.write_text("{not valid json")

        state = run_monitor(
            bridge,
            notifier=notifier,
            cycles=2,
            min_score=None,
            sleep_fn=_break_hook,
            now_fn=_time.time,
            config_path=config_path,
        )

        # The valid cycle-1 config (bar 0) is retained; the corrupt file is
        # observed, not fatal. Findings still notify under the kept config.
        assert state.config_reload_error != ""
        assert state.notified_total == 2
    finally:
        _cfg.NOTIFY_MIN_SCORE = original

