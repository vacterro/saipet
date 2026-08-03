import json

from saipet.notify import (
    ConsoleNotifier,
    FileNotifier,
    MultiNotifier,
    Notifier,
    NullNotifier,
    error,
    finding,
    heartbeat,
)

NOW = 1_800_000_000.0  # 2027-01-15T08:00:00Z

_ITEM = {
    "id": "t1",
    "subreddit": "testsub",
    "title": "agent keeps losing context between sessions",
    "permalink": "https://reddit.com/r/testsub/t1",
    "score": 85.0,
    "band": "priority",
}


def test_a_finding_carries_the_readable_line_and_the_whole_item():
    note = finding(_ITEM, NOW)

    assert note.kind == "finding"
    assert "[priority] 85 r/testsub -- agent keeps losing context" in note.text
    assert note.data["permalink"] == "https://reddit.com/r/testsub/t1"


def test_heartbeat_and_error_are_their_own_kinds():
    assert heartbeat(3, 0, NOW).kind == "heartbeat"
    assert "cycle 3: 0 new candidate(s)" in heartbeat(3, 0, NOW).text
    assert error("boom", 2, NOW).kind == "error"
    assert "cycle 2 failed: boom" in error("boom", 2, NOW).text


def test_console_notifier_writes_one_line():
    lines: list[str] = []
    ConsoleNotifier(print_fn=lines.append).send(finding(_ITEM, NOW))

    assert len(lines) == 1
    assert lines[0].startswith("NOTIFY 2027-01-15 08:00:00Z finding:")


def test_file_notifier_writes_both_files(tmp_path):
    notifier = FileNotifier(tmp_path / "notifications.jsonl")

    notifier.send(finding(_ITEM, NOW))

    record = json.loads(notifier.path.read_text(encoding="utf-8").strip())
    assert record["kind"] == "finding"
    assert record["data"]["id"] == "t1"
    inbox = notifier.inbox_path.read_text(encoding="utf-8")
    assert "**finding**" in inbox
    assert "<https://reddit.com/r/testsub/t1>" in inbox


def test_file_notifier_appends_rather_than_overwriting(tmp_path):
    """An inbox that overwrites itself is worse than none: it looks full
    while losing everything before the last write."""
    notifier = FileNotifier(tmp_path / "notifications.jsonl")

    notifier.send(finding(_ITEM, NOW))
    notifier.send(finding({**_ITEM, "id": "t2"}, NOW + 60))

    ids = [
        json.loads(line)["data"]["id"]
        for line in notifier.path.read_text(encoding="utf-8").splitlines()
    ]
    assert ids == ["t1", "t2"]
    assert len(notifier.inbox_path.read_text(encoding="utf-8").splitlines()) == 2


def test_file_notifier_survives_a_restart(tmp_path):
    FileNotifier(tmp_path / "n.jsonl").send(finding(_ITEM, NOW))
    FileNotifier(tmp_path / "n.jsonl").send(finding({**_ITEM, "id": "t2"}, NOW))

    assert len((tmp_path / "n.jsonl").read_text(encoding="utf-8").splitlines()) == 2


def test_file_notifier_creates_its_directory(tmp_path):
    notifier = FileNotifier(tmp_path / "deep" / "nested" / "n.jsonl")
    notifier.send(heartbeat(1, 0, NOW))
    assert notifier.path.exists()


def test_a_heartbeat_has_no_permalink_to_append(tmp_path):
    notifier = FileNotifier(tmp_path / "n.jsonl")
    notifier.send(heartbeat(1, 0, NOW))
    assert "<" not in notifier.inbox_path.read_text(encoding="utf-8")


def test_multi_notifier_reaches_every_sink(tmp_path):
    lines: list[str] = []
    file_sink = FileNotifier(tmp_path / "n.jsonl")

    MultiNotifier(ConsoleNotifier(print_fn=lines.append), file_sink).send(finding(_ITEM, NOW))

    assert len(lines) == 1
    assert file_sink.path.exists()


def test_one_dead_sink_does_not_cost_the_others_their_message(tmp_path):
    class _Broken(Notifier):
        def send(self, notification):
            raise OSError("disk on fire")

    lines: list[str] = []
    multi = MultiNotifier(_Broken(), ConsoleNotifier(print_fn=lines.append))

    multi.send(finding(_ITEM, NOW))

    assert lines  # the healthy sink still got it
    assert multi.failures == ["_Broken: OSError: disk on fire"]


def test_the_null_sink_drops_everything_without_complaining():
    NullNotifier().send(finding(_ITEM, NOW))
