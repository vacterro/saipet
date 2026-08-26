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


# W2-003: independent per-destination delivery ----------------------------------


def test_an_inbox_failure_after_a_successful_jsonl_append_is_partial(tmp_path):
    """W2-003: the inbox write failed AFTER the JSONL append had landed, but
    send() reported total failure -- and a naive retry duplicated the JSONL
    record. JSONL success is now retained and attributed separately."""
    import json as _json

    feed = tmp_path / "notifications.jsonl"
    bad_inbox = tmp_path / "inbox-is-a-directory"  # exists -> open() fails
    bad_inbox.mkdir()
    notifier = FileNotifier(feed, inbox_path=bad_inbox)

    first = notifier.send(finding(_ITEM, NOW))

    assert first.delivered is False
    assert any(name.endswith(".inbox") for name, _r in first.failures)
    # The durable feed holds exactly one record despite the failure...
    lines = feed.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert _json.loads(lines[0])["data"]["id"] == "t1"

    # ...and the retry writes ONLY the missing destination.
    good = FileNotifier(feed, inbox_path=tmp_path / "inbox.md")
    second = good.send(finding(_ITEM, NOW))
    assert second.delivered is True
    assert len(feed.read_text(encoding="utf-8").splitlines()) == 1  # no duplicate
    assert len((tmp_path / "inbox.md").read_text(encoding="utf-8").splitlines()) == 1


def test_resending_the_same_notification_does_not_duplicate_either_file(tmp_path):
    from saipet.notify import FileNotifier as _F

    notifier = _F(tmp_path / "n.jsonl")
    assert notifier.send(finding(_ITEM, NOW)).delivered
    assert notifier.send(finding(_ITEM, NOW)).delivered  # identical identity

    assert len(notifier.path.read_text(encoding="utf-8").splitlines()) == 1
    assert len(notifier.inbox_path.read_text(encoding="utf-8").splitlines()) == 1


def test_a_jsonl_only_failure_reports_the_feed_not_the_inbox(tmp_path):
    feed_dir = tmp_path / "feed-is-a-directory"
    feed_dir.mkdir()  # open-for-append on a directory fails
    notifier = FileNotifier(feed_dir, inbox_path=tmp_path / "inbox.md")

    result = notifier.send(finding(_ITEM, NOW))

    assert result.delivered is False
    assert any(name.endswith(".jsonl") for name, _r in result.failures)


# PERF-002: point-addressable completion store under a large backlog --------


def test_completion_store_is_point_addressable_under_large_backlog(tmp_path):
    """PERF-002: a long-lived monitor accumulates many completed identities.
    The completion store must be point-addressable: a novel send is a single
    point lookup/update, not a full-state read/rewrite proportional to H."""
    import sqlite3

    feed = tmp_path / "n.jsonl"
    notifier = FileNotifier(feed)
    # Simulate 50k prior completed notifications directly in the store.
    db = notifier._db_conn()
    db.executemany(
        "INSERT OR IGNORE INTO delivery (identity, jsonl_done, inbox_done) "
        "VALUES (?, 1, 1)",
        [(f"prior-{i:06d}",) for i in range(50_000)],
    )
    db.commit()

    # A novel notification: one point add, no full-state rewrite.
    novel = finding({"id": "brand-new", "subreddit": "x", "title": "t", "permalink": "https://r/x/bn"}, NOW)
    assert notifier.send(novel).delivered
    assert len(feed.read_text(encoding="utf-8").splitlines()) == 1
    assert len(notifier.inbox_path.read_text(encoding="utf-8").splitlines()) == 1

    # Resend the same novel notification -> still exactly one line each.
    assert notifier.send(novel).delivered
    assert len(feed.read_text(encoding="utf-8").splitlines()) == 1
    assert len(notifier.inbox_path.read_text(encoding="utf-8").splitlines()) == 1

    # The store holds the prior backlog plus the novel identity, untouched.
    total = db.execute("SELECT COUNT(*) FROM delivery").fetchone()[0]
    assert total == 50_001


def test_completion_store_survives_a_fresh_process(tmp_path):
    """PERF-002: the SQLite completion store is durable across a new
    FileNotifier instance (simulating a monitor restart)."""
    feed = tmp_path / "n.jsonl"
    FileNotifier(feed).send(finding(_ITEM, NOW))

    # A brand-new instance must see the prior completion and not duplicate.
    again = FileNotifier(feed)
    assert again.send(finding(_ITEM, NOW)).delivered
    assert len(feed.read_text(encoding="utf-8").splitlines()) == 1
    assert len(again.inbox_path.read_text(encoding="utf-8").splitlines()) == 1
