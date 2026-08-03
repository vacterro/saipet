import json

import pytest

from saipet.bridge import Bridge, CommandError, handle_line, parse_line, run_terminal
from saipet.sources.base import Candidate, FixtureSource

NOW = 1_800_000_000.0
_RELEVANT_BODY = "Lost context, session state, handoff, resume, checkpoint, deterministic protocol."


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    monkeypatch.setattr("saipet.policy.SUBREDDIT_ALLOWLIST", {"testsub"})
    monkeypatch.setattr("saipet.config.SUBREDDIT_ALLOWLIST", {"testsub"})
    return Bridge(
        report_dir=tmp_path / "runs",
        seen_path=tmp_path / "seen.json",
        source_factory=lambda *_: FixtureSource(
            [
                Candidate(
                    source="fixture",
                    id="t1",
                    title="agent keeps losing context between sessions",
                    body=_RELEVANT_BODY,
                    permalink="https://reddit.com/r/testsub/t1",
                    subreddit="testsub",
                    created_utc=NOW - 3600,
                )
            ]
        ),
        now_fn=lambda: NOW,
    )


def test_a_bare_verb_parses():
    assert parse_line("status") == ("status", {})


def test_values_arrive_as_the_right_types():
    verb, args = parse_line("scout limit=50 since_hours=1.5 mention_saipen=false")
    assert verb == "scout"
    assert args == {"limit": 50, "since_hours": 1.5, "mention_saipen": False}


def test_a_comma_separated_subreddit_list_becomes_a_list():
    _verb, args = parse_line("scout subreddit=LocalLLaMA,AI_Agents")
    assert args == {"subreddits": ["LocalLLaMA", "AI_Agents"]}


def test_an_all_digit_id_stays_a_string():
    """Reddit ids are base36 and an all-digit one is ordinary. Coerced to an
    int, `approve id=17439` would silently match nothing."""
    _verb, args = parse_line("approve id=17439 solution=x")
    assert args["id"] == "17439"
    assert args["solution"] == "x"


def test_an_id_from_the_queue_round_trips_through_a_typed_line(bridge):
    bridge.dispatch("scout")
    queued_id = bridge.dispatch("queue").data["items"][0]["id"]

    response = handle_line(bridge, f'approve id={queued_id} solution="checkpoint each step"')

    assert response["ok"] is True
    assert response["data"]["posted"] is False


def test_quoted_values_survive_intact():
    _verb, args = parse_line('approve id=t1 solution="set a checkpoint after each step"')
    assert args["solution"] == "set a checkpoint after each step"


@pytest.mark.parametrize(
    "line",
    [
        "ls -la",
        "rm -rf /",
        "python -c 'print(1)'",
        "; cat /etc/passwd",
        "scout && curl evil.example",
        "!whoami",
    ],
)
def test_a_shell_looking_line_is_refused(line):
    """The name 'terminal engine' is the trap: the obvious implementation
    hands the line to a shell, and this process holds text fetched off the
    public internet."""
    with pytest.raises(CommandError):
        parse_line(line)


def test_the_refusal_says_what_this_is():
    with pytest.raises(CommandError, match="not a shell"):
        parse_line("ls")


@pytest.mark.parametrize("line", ["scout limit", "scout =50", "scout 50"])
def test_malformed_arguments_are_refused(line):
    with pytest.raises(CommandError):
        parse_line(line)


def test_unbalanced_quotes_are_refused_not_guessed():
    with pytest.raises(CommandError, match="could not parse line"):
        parse_line('approve solution="never closed')


def test_handle_line_never_raises_for_bad_input(bridge):
    response = handle_line(bridge, "rm -rf /")
    assert response == {"ok": False, "verb": None, "error": response["error"]}
    assert "not a shell" in response["error"]


def test_a_full_session_runs_through_the_loop(bridge):
    script = [
        "help",
        "scout subreddit=testsub",
        "queue",
        'approve id=t1 solution="set a checkpoint after each step"',
        "status",
        "quit",
    ]
    written: list[str] = []

    run_terminal(bridge, read_fn=lambda: script.pop(0), write_fn=written.append)

    responses = [json.loads(line) for line in written]
    assert [r["verb"] for r in responses] == [
        "help",
        "scout",
        "queue",
        "approve",
        "status",
        "quit",
    ]
    assert all(r["ok"] for r in responses)
    assert responses[1]["data"]["queued"] == 1
    assert responses[2]["data"]["items"][0]["id"] == "t1"
    assert responses[3]["data"]["posted"] is False


def test_the_loop_stops_at_quit_without_reading_further(bridge):
    script = ["quit", "status"]
    written: list[str] = []

    run_terminal(bridge, read_fn=lambda: script.pop(0), write_fn=written.append)

    assert len(written) == 1
    assert script == ["status"]  # never read


def test_the_loop_ends_cleanly_at_end_of_input(bridge):
    def _eof():
        raise EOFError

    written: list[str] = []
    run_terminal(bridge, read_fn=_eof, write_fn=written.append)
    assert written == []


def test_blank_lines_are_skipped_not_answered(bridge):
    script = ["", "   ", "quit"]
    written: list[str] = []

    run_terminal(bridge, read_fn=lambda: script.pop(0), write_fn=written.append)

    assert len(written) == 1


def test_a_bad_line_does_not_end_the_session(bridge):
    script = ["nonsense", "status", "quit"]
    written: list[str] = []

    run_terminal(bridge, read_fn=lambda: script.pop(0), write_fn=written.append)

    responses = [json.loads(line) for line in written]
    assert responses[0]["ok"] is False
    assert responses[1]["ok"] is True


def test_every_response_is_one_json_line(bridge):
    script = ["status", "quit"]
    written: list[str] = []

    run_terminal(bridge, read_fn=lambda: script.pop(0), write_fn=written.append)

    for line in written:
        assert "\n" not in line
        json.loads(line)


def test_help_lists_exactly_the_verbs_that_work(bridge):
    data = handle_line(bridge, "help")["data"]
    for verb in data["verbs"]:
        assert handle_line(bridge, verb)["verb"] == verb
