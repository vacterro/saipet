import json

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
    assert state.notified == 1
    assert list((tmp_path / "runs").glob("*.jsonl"))  # the run report
    feed = (tmp_path / "notifications.jsonl").read_text(encoding="utf-8").splitlines()
    kinds = [json.loads(line)["kind"] for line in feed]
    assert kinds == ["finding", "heartbeat"]
    assert (tmp_path / "inbox.md").exists()


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


def test_an_empty_allowlist_is_called_out_rather_than_looking_quiet(tmp_path, monkeypatch):
    monkeypatch.setattr("saipet.config.SUBREDDIT_ALLOWLIST", set())
    monkeypatch.chdir(tmp_path)
    lines: list[str] = []

    main(["--cycles", "1"], print_fn=lines.append, sleep_fn=lambda _s: None, now_fn=lambda: NOW)

    assert any("no subreddits to watch" in line for line in lines)


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
