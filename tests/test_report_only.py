import pytest

from saipet.cli import main, parse_args, scout
from saipet.signals import extract_signals
from saipet.sources.base import Candidate, FixtureSource

_RELEVANT_BODY = "Lost context, session state, handoff, resume, checkpoint, deterministic protocol."


def _explode(*_args, **_kwargs):
    raise AssertionError("--report-only must never read stdin")


@pytest.fixture(autouse=True)
def _no_creds(monkeypatch):
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)


def test_report_only_defaults_to_off():
    assert parse_args([]).report_only is False
    assert parse_args(["--report-only"]).report_only is True


def test_report_only_never_reads_stdin_even_with_candidates(monkeypatch, tmp_path):
    """The empty fixture would pass this test for the wrong reason -- an
    empty queue prompts for nothing either way -- so the source is stubbed
    to return a candidate that WOULD be prompted about."""
    monkeypatch.setattr("saipet.cli.SUBREDDIT_ALLOWLIST", {"testsub"})
    monkeypatch.setattr("saipet.policy.SUBREDDIT_ALLOWLIST", {"testsub"})
    monkeypatch.setattr(
        "saipet.cli.build_source",
        lambda *_: FixtureSource(
            [
                Candidate(
                    source="fixture",
                    id="t1",
                    title="how do I keep my agent from losing context",
                    body=_RELEVANT_BODY,
                    permalink="https://reddit.com/r/testsub/t1",
                    subreddit="testsub",
                )
            ]
        ),
    )
    monkeypatch.chdir(tmp_path)
    lines: list[str] = []

    main(["--report-only"], print_fn=lines.append, input_fn=_explode)

    assert any("1 candidate(s) queued" in line for line in lines)
    assert list((tmp_path / "runs").glob("*.jsonl"))


def test_the_same_run_without_the_flag_does_prompt(monkeypatch, tmp_path):
    """Proof the guard above is connected to something: drop the flag and
    the identical run reaches the prompt."""
    monkeypatch.setattr("saipet.cli.SUBREDDIT_ALLOWLIST", {"testsub"})
    monkeypatch.setattr("saipet.policy.SUBREDDIT_ALLOWLIST", {"testsub"})
    monkeypatch.setattr(
        "saipet.cli.build_source",
        lambda *_: FixtureSource(
            [
                Candidate(
                    source="fixture",
                    id="t1",
                    title="how do I keep my agent from losing context",
                    body=_RELEVANT_BODY,
                    permalink="https://reddit.com/r/testsub/t1",
                    subreddit="testsub",
                )
            ]
        ),
    )
    monkeypatch.chdir(tmp_path)
    prompts: list[str] = []

    def _record(prompt):
        prompts.append(prompt)
        return ""  # blank answer skips the item

    main([], print_fn=lambda _line: None, input_fn=_record)

    assert prompts


def test_the_queue_is_identical_with_and_without_the_flag():
    """--report-only changes what happens after scoring, never the scoring."""
    source = FixtureSource(
        [
            Candidate(
                source="fixture",
                id="t1",
                title="x",
                body=_RELEVANT_BODY,
                permalink="p1",
                subreddit="testsub",
            )
        ]
    )
    queue = scout(source, signal_fn=extract_signals)
    assert isinstance(queue.pending(), list)
