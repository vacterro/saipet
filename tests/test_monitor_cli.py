import json
import os

import pytest

from saipet.monitor import build_notifier, main, parse_args
from saipet.notify import MultiNotifier
from saipet.sources.base import Candidate, FixtureSource

NOW = 1_800_000_000.0
_STRONG_BODY = (
    "My AI agent keeps losing context between sessions. The handoff has no checkpoint, "
    "so resume is guesswork. Is there a deterministic protocol or workflow for this?"
)


@pytest.fixture(autouse=True)
def _no_creds(monkeypatch):
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)


@pytest.fixture
def _one_finding(monkeypatch):
    # CORE-002: a non-fixture daemon run now requires credentials up front,
    # so this live-path fixture presents them.
    monkeypatch.setenv("REDDIT_CLIENT_ID", "id")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")
    monkeypatch.setattr("saipet.policy.SUBREDDIT_ALLOWLIST", {"testsub"})
    monkeypatch.setattr("saipet.config.SUBREDDIT_ALLOWLIST", {"testsub"})
    monkeypatch.setattr(
        "saipet.monitor.build_source",
        lambda *_: FixtureSource(
            [
                Candidate(
                    source="fixture",
                    id="t1",
                    title="agent keeps losing context",
                    body=_STRONG_BODY,
                    permalink="https://reddit.com/r/testsub/t1",
                    subreddit="testsub",
                    created_utc=NOW - 3600,
                )
            ]
        ),
    )


def test_defaults_are_a_sane_daemon():
    args = parse_args([])
    assert args.interval == 900
    assert args.cycles is None  # forever, which is the point
    assert args.heartbeat_every == 1  # on, unlike the library default
    assert args.quiet is False


@pytest.mark.parametrize(
    "argv",
    [
        ["--interval", "0"],
        ["--interval", "-5"],
        ["--cycles", "0"],
        ["--heartbeat-every", "-1"],
        ["--limit", "0"],
    ],
)
def test_nonsense_flags_are_refused_at_parse_time(argv):
    with pytest.raises(SystemExit):
        parse_args(argv)


def test_quiet_drops_the_console_sink_and_keeps_the_file(tmp_path):
    loud = build_notifier(str(tmp_path / "n.jsonl"), quiet=False)
    quiet = build_notifier(str(tmp_path / "n.jsonl"), quiet=True)

    assert isinstance(loud, MultiNotifier) and len(loud.sinks) == 2
    assert len(quiet.sinks) == 1


def test_one_cycle_writes_a_report_and_a_notification(tmp_path, monkeypatch, _one_finding):
    monkeypatch.chdir(tmp_path)
    lines: list[str] = []

    state = main(
        ["--cycles", "1", "--subreddit", "testsub"],
        print_fn=lines.append,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )

    assert state.cycles == 1
    assert state.notified_total == 1
    assert list((tmp_path / "runs").glob("*.jsonl"))  # the run report
    feed = (tmp_path / "notifications.jsonl").read_text(encoding="utf-8").splitlines()
    kinds = [json.loads(line)["kind"] for line in feed]
    assert kinds == ["finding", "heartbeat"]
    assert (tmp_path / "inbox.md").exists()


def test_an_empty_cycle_writes_no_report_pair(tmp_path, monkeypatch, _one_finding):
    """T-025: an unattended monitor on an empty cycle must not add 192 files
    a day. The fixture yields a finding on the first cycle, so run a second
    cycle that finds nothing and assert the report count is unchanged."""
    monkeypatch.chdir(tmp_path)
    lines: list[str] = []

    main(
        ["--cycles", "1", "--subreddit", "testsub"],
        print_fn=lines.append,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )
    reports_after_first = len(list((tmp_path / "runs").glob("*.jsonl")))
    assert reports_after_first == 1

    # Second run with an already-seen candidate -> an empty cycle.
    main(
        ["--cycles", "1", "--subreddit", "testsub"],
        print_fn=lines.append,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )

    assert len(list((tmp_path / "runs").glob("*.jsonl"))) == reports_after_first


def test_a_cycle_appends_to_the_daily_findings_feed(tmp_path, monkeypatch, _one_finding):
    """T-025: findings land in one findings-YYYY-MM-DD.jsonl per day, not one
    report pair per cycle."""
    monkeypatch.chdir(tmp_path)
    lines: list[str] = []

    main(
        ["--cycles", "1", "--subreddit", "testsub"],
        print_fn=lines.append,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )

    feed = tmp_path / "runs" / "findings-2027-01-15.jsonl"
    assert feed.exists()
    records = [json.loads(line) for line in feed.read_text(encoding="utf-8").splitlines()]
    assert records and records[0]["id"] == "t1"


# T-026: durable status record is written and readable by a fresh Bridge ----


def test_monitor_writes_status_and_a_fresh_bridge_reads_it(tmp_path, monkeypatch, _one_finding):
    """A monitor run writes the durable status file; a separate Bridge
    instance with the same status_path can answer "is the daemon alive"."""
    from saipet.bridge import Bridge as _B

    monkeypatch.chdir(tmp_path)
    status_file = tmp_path / "monitor-status.json"
    lines: list[str] = []

    main(
        ["--cycles", "1", "--subreddit", "testsub"],
        print_fn=lines.append,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )

    assert status_file.exists()
    raw = json.loads(status_file.read_text(encoding="utf-8"))
    assert raw["cycles"] == 1
    assert raw["health"] == "healthy"
    assert raw["pid"] == os.getpid()
    assert raw["cycle_completed_at"] == NOW
    assert raw["notifier_health"] == "healthy"

    fresh = _B(report_dir=tmp_path / "runs", seen_path=tmp_path / "seen.json",
               status_path=str(status_file), now_fn=lambda: NOW)
    durable = fresh._durable_monitor_status()
    assert durable is not None
    assert durable["cycles"] == 1
    assert durable["health"] == "healthy"


def test_the_console_sink_carries_the_finding_too(tmp_path, monkeypatch, _one_finding):
    monkeypatch.chdir(tmp_path)
    lines: list[str] = []

    main(
        ["--cycles", "1", "--subreddit", "testsub"],
        print_fn=lines.append,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )

    assert any("NOTIFY" in line and "finding" in line for line in lines)
    assert any(line.startswith("monitor: every 900s") for line in lines)


def test_quiet_keeps_findings_off_stdout(tmp_path, monkeypatch, _one_finding):
    monkeypatch.chdir(tmp_path)
    lines: list[str] = []

    main(
        ["--cycles", "1", "--subreddit", "testsub", "--quiet"],
        print_fn=lines.append,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )

    assert not any("NOTIFY" in line for line in lines)
    assert (tmp_path / "notifications.jsonl").exists()


def test_an_empty_allowlist_without_fixture_exits_nonzero(tmp_path, monkeypatch):
    monkeypatch.setattr("saipet.config.SUBREDDIT_ALLOWLIST", set())
    monkeypatch.setenv("REDDIT_CLIENT_ID", "x")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "y")
    monkeypatch.chdir(tmp_path)
    lines: list[str] = []

    with pytest.raises(SystemExit) as exit_info:
        main(["--cycles", "1"], print_fn=lines.append, sleep_fn=lambda _s: None, now_fn=lambda: NOW)

    assert exit_info.value.code != 0
    assert any("allowlist is empty" in line for line in lines)


def test_an_empty_allowlist_with_fixture_warns_and_runs(tmp_path, monkeypatch):
    monkeypatch.setattr("saipet.config.SUBREDDIT_ALLOWLIST", set())
    monkeypatch.chdir(tmp_path)
    lines: list[str] = []

    state = main(
        ["--cycles", "1", "--fixture"],
        print_fn=lines.append,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )

    assert state.cycles == 1
    assert any("--fixture with no subreddits" in line for line in lines)


def test_no_credentials_without_fixture_exits_nonzero(tmp_path, monkeypatch):
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    monkeypatch.setattr("saipet.config.SUBREDDIT_ALLOWLIST", {"testsub"})
    monkeypatch.chdir(tmp_path)
    lines: list[str] = []

    with pytest.raises(SystemExit) as exit_info:
        main(["--cycles", "1"], print_fn=lines.append, sleep_fn=lambda _s: None, now_fn=lambda: NOW)

    assert exit_info.value.code != 0
    assert any("no Reddit credentials" in line for line in lines)


def test_no_credentials_with_fixture_warns_and_runs(tmp_path, monkeypatch):
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    monkeypatch.setattr("saipet.config.SUBREDDIT_ALLOWLIST", {"testsub"})
    monkeypatch.chdir(tmp_path)
    lines: list[str] = []

    state = main(
        ["--cycles", "1", "--fixture"],
        print_fn=lines.append,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )

    assert state.cycles == 1


# CORE-002: source mode is decided exactly once --------------------------------


def test_no_credentials_with_explicit_subreddit_exits_nonzero(tmp_path, monkeypatch):
    """CORE-002: explicit --subreddit targets used to bypass the credential
    guard while build_source still picked the empty fixture -- a 'healthy'
    daemon finding nothing forever. A live-mode run without credentials is
    refused regardless of where its targets came from."""
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    monkeypatch.setattr("saipet.config.SUBREDDIT_ALLOWLIST", {"testsub"})
    monkeypatch.chdir(tmp_path)
    lines: list[str] = []

    with pytest.raises(SystemExit) as exit_info:
        main(
            ["--cycles", "1", "--subreddit", "testsub"],
            print_fn=lines.append,
            sleep_fn=lambda _s: None,
            now_fn=lambda: NOW,
        )

    assert exit_info.value.code != 0
    assert any("no Reddit credentials" in line for line in lines)


def test_no_credentials_with_fixture_and_explicit_subreddit_runs(tmp_path, monkeypatch):
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    monkeypatch.setattr("saipet.config.SUBREDDIT_ALLOWLIST", {"testsub"})
    monkeypatch.chdir(tmp_path)
    lines: list[str] = []

    state = main(
        ["--cycles", "1", "--fixture", "--subreddit", "testsub"],
        print_fn=lines.append,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )

    assert state.cycles == 1
    assert any(line.startswith("source: fixture") for line in lines)


def test_fixture_forces_the_empty_fixture_even_with_credentials(tmp_path, monkeypatch):
    """CORE-002: --fixture never reached build_source, so credentials forced
    RedditSource despite the explicit fixture request. The resolved factory
    must not consult the environment at all on a fixture run."""
    monkeypatch.setenv("REDDIT_CLIENT_ID", "id")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")
    monkeypatch.setattr("saipet.config.SUBREDDIT_ALLOWLIST", {"testsub"})

    def _boom(*_a):
        raise AssertionError("build_source must not run on an explicit fixture run")

    monkeypatch.setattr("saipet.monitor.build_source", _boom)
    monkeypatch.chdir(tmp_path)
    lines: list[str] = []

    state = main(
        ["--cycles", "1", "--fixture", "--subreddit", "testsub"],
        print_fn=lines.append,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )

    assert state.cycles == 1
    assert any(line.startswith("source: fixture") for line in lines)


def test_a_broken_config_file_exits_cleanly(tmp_path, monkeypatch):
    (tmp_path / "saipet.config.json").write_text('{"nope": 1}', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    lines: list[str] = []

    with pytest.raises(SystemExit) as exit_info:
        main(["--cycles", "1"], print_fn=lines.append)

    assert exit_info.value.code == 2
    assert any("config error: unknown config key 'nope'" in line for line in lines)


def test_the_notify_file_is_movable(tmp_path, monkeypatch, _one_finding):
    monkeypatch.chdir(tmp_path)

    main(
        ["--cycles", "1", "--subreddit", "testsub", "--notify-file", "feeds/alerts.jsonl"],
        print_fn=lambda _line: None,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )

    assert (tmp_path / "feeds" / "alerts.jsonl").exists()
    assert (tmp_path / "feeds" / "inbox.md").exists()


def test_the_daemon_wires_a_durable_outbox_next_to_the_feed(tmp_path, monkeypatch, _one_finding):
    """CORE-004: the shipped entrypoint persists delivery obligations, so a
    sink outage never costs an alert its retry across restarts."""
    from saipet.outbox import DeliveryOutbox

    monkeypatch.chdir(tmp_path)
    main(
        ["--cycles", "1", "--subreddit", "testsub"],
        print_fn=lambda _line: None,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )

    outbox_file = tmp_path / "notifications.outbox.json"
    assert outbox_file.exists()
    # The finding delivered successfully, so nothing stays pending in the
    # outbox.
    assert DeliveryOutbox(outbox_file).obligations() == []


def test_default_status_file_is_wired_to_a_default_bridge(tmp_path, monkeypatch, _one_finding):
    """CORE-010: the daemon wrote monitor-status.json but shipped readers
    built their Bridge without status_path -- production and consumption
    were disconnected. A default-construction Bridge now reads the one
    canonical default file."""
    import json as _json

    from saipet.bridge import Bridge

    monkeypatch.chdir(tmp_path)
    main(
        ["--cycles", "1", "--subreddit", "testsub"],
        print_fn=lambda _line: None,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )

    raw = _json.loads((tmp_path / "monitor-status.json").read_text(encoding="utf-8"))
    assert raw["cycles"] == 1

    fresh = Bridge(report_dir=tmp_path / "runs", seen_path=tmp_path / "seen.json",
                   now_fn=lambda: NOW)  # no status_path at all
    durable = fresh.dispatch("status").data["monitor_durable"]
    assert durable is not None
    assert durable["cycles"] == 1
