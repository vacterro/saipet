"""Output sanitization: untrusted internet text at the sink boundary.

Reddit titles carry user input, not trusted prose. A newline in a title
currently forges a second NOTIFY line on stdout; ANSI escapes and control
characters do the same to the inbox and the JSONL feed's human rendering.
The console and inbox sinks sanitize; the JSONL feed keeps raw text so an
agent can still read the original.
"""

import json

import pytest

from saipet.notify import (
    ConsoleNotifier,
    FileNotifier,
    _escape_markdown,
    _strip_untrusted,
    _validate_permalink,
    error,
    finding,
    heartbeat,
)

NOW = 1_800_000_000.0


def test_a_title_with_a_newline_stays_one_console_line():
    """The reported defect: a newline in the title forges a second NOTIFY
    line on stdout."""
    item = {
        "id": "t1",
        "subreddit": "testsub",
        "title": "agent\nloses context",
        "permalink": "https://reddit.com/r/testsub/t1",
        "score": 85.0,
        "band": "priority",
    }
    lines: list[str] = []
    ConsoleNotifier(print_fn=lines.append).send(finding(item, NOW))

    assert len(lines) == 1
    assert "\n" not in lines[0]
    assert "agent loses context" in lines[0]


def test_an_ansi_sequence_stays_out_of_console_output():
    item = {
        "id": "t1",
        "subreddit": "testsub",
        "title": "how to \x1b[31mredact\x1b[0m this",
        "permalink": "https://reddit.com/r/testsub/t1",
        "score": 70.0,
        "band": "relevant",
    }
    lines: list[str] = []
    ConsoleNotifier(print_fn=lines.append).send(finding(item, NOW))

    assert len(lines) == 1
    assert "\x1b" not in lines[0]
    assert "redact" in lines[0]


def test_a_control_character_stays_out_of_console_output():
    item = {
        "id": "t1",
        "subreddit": "testsub",
        "title": "before\x07after",
        "permalink": "https://reddit.com/r/testsub/t1",
        "score": 70.0,
        "band": "relevant",
    }
    lines: list[str] = []
    ConsoleNotifier(print_fn=lines.append).send(finding(item, NOW))

    assert len(lines) == 1
    assert "\x07" not in lines[0]


def test_raw_text_survives_intact_in_the_jsonl_feed(tmp_path):
    item = {
        "id": "t1",
        "subreddit": "testsub",
        "title": "agent\nloses context",
        "permalink": "https://reddit.com/r/testsub/t1",
        "score": 85.0,
        "band": "priority",
    }
    file_sink = FileNotifier(path=tmp_path / "notifications.jsonl")
    file_sink.send(finding(item, NOW))

    record = json.loads(file_sink.path.read_text(encoding="utf-8").strip())
    assert record["data"]["title"] == "agent\nloses context"
    file_sink.path.unlink(missing_ok=True)


def test_markdown_metacharacters_are_escaped_in_the_inbox(tmp_path):
    item = {
        "id": "t1",
        "subreddit": "testsub",
        "title": "**bold** and *italic* and `code`",
        "permalink": "https://reddit.com/r/testsub/t1",
        "score": 85.0,
        "band": "priority",
    }
    notifier = FileNotifier(tmp_path / "n.jsonl")
    notifier.send(finding(item, NOW))

    inbox = notifier.inbox_path.read_text(encoding="utf-8")
    # The asterisks should be escaped so markdown does not render them.
    assert "\\*\\*bold\\*\\*" in inbox
    assert "\\*italic\\*" in inbox
    assert "\\`code\\`" in inbox


def test_non_https_permalink_is_dropped_from_the_inbox(tmp_path):
    item = {
        "id": "t1",
        "subreddit": "testsub",
        "title": "some thread",
        "permalink": "javascript:alert(1)",
        "score": 85.0,
        "band": "priority",
    }
    notifier = FileNotifier(tmp_path / "n.jsonl")
    notifier.send(finding(item, NOW))

    inbox = notifier.inbox_path.read_text(encoding="utf-8")
    assert "javascript:" not in inbox


def test_a_missing_permalink_is_harmless(tmp_path):
    item = {
        "id": "t1",
        "subreddit": "testsub",
        "title": "no link here",
        "score": 85.0,
        "band": "priority",
    }
    notifier = FileNotifier(tmp_path / "n.jsonl")
    notifier.send(finding(item, NOW))

    inbox = notifier.inbox_path.read_text(encoding="utf-8")
    assert "<" not in inbox


def test_stripping_leaves_legible_words_intact():
    assert _strip_untrusted("clean title") == "clean title"
    assert _strip_untrusted("tab\there") == "tab here"
    assert _strip_untrusted("back\rbefore") == "back before"
    assert _strip_untrusted("  padded  ") == "padded"


def test_escaping_preserves_safe_characters():
    assert _escape_markdown("plain text") == "plain text"
    assert _escape_markdown("no specials") == "no specials"


def test_validate_permalink_accepts_just_https():
    assert _validate_permalink("https://reddit.com/r/testsub/t1") == "https://reddit.com/r/testsub/t1"
    assert _validate_permalink("http://reddit.com/r/testsub/t1") is None
    assert _validate_permalink("javascript:alert(1)") is None
    assert _validate_permalink("") is None
    assert _validate_permalink(None) is None
    assert _validate_permalink("not-a-url") is None
