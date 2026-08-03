import json

from saipet.report import write_report
from saipet.review import ReviewItem
from saipet.sources.base import Candidate

NOW = 1_800_000_000.0  # 2027-01-15T08:00:00Z


def _item(candidate_id, score, band="review"):
    return ReviewItem(
        candidate=Candidate(
            source="reddit",
            id=candidate_id,
            title=f"title {candidate_id}",
            body="body",
            permalink=f"https://reddit.com/r/testsub/{candidate_id}",
            subreddit="testsub",
            created_utc=NOW - 3600,
        ),
        relevance_score=score,
        band=band,
        draft="",
        signals={"problem_match": 40.0, "audience_fit": 20.0},
    )


def test_both_files_are_written_with_the_expected_fields(tmp_path):
    paths = write_report([_item("a1", 85, "priority")], directory=tmp_path, now_fn=lambda: NOW)

    assert paths.jsonl.exists() and paths.markdown.exists()
    record = json.loads(paths.jsonl.read_text(encoding="utf-8").strip())
    assert record == {
        "source": "reddit",
        "id": "a1",
        "subreddit": "testsub",
        "title": "title a1",
        "permalink": "https://reddit.com/r/testsub/a1",
        "created_utc": NOW - 3600,
        "score": 85,
        "band": "priority",
        "signals": {"problem_match": 40.0, "audience_fit": 20.0},
    }
    markdown = paths.markdown.read_text(encoding="utf-8")
    assert "title a1" in markdown
    assert "1 candidate(s) queued" in markdown


def test_the_filename_is_the_run_time_in_utc(tmp_path):
    paths = write_report([], directory=tmp_path, now_fn=lambda: NOW)
    assert paths.jsonl.name == "20270115T080000Z.jsonl"
    assert paths.markdown.name == "20270115T080000Z.md"


def test_an_empty_run_still_writes_a_report(tmp_path):
    """'Ran and found nothing' and 'never ran' are different facts, and a
    missing file cannot tell them apart."""
    paths = write_report([], directory=tmp_path, now_fn=lambda: NOW)

    assert paths.jsonl.read_text(encoding="utf-8") == ""
    assert "0 candidate(s) queued" in paths.markdown.read_text(encoding="utf-8")


def test_records_are_ranked_by_score(tmp_path):
    paths = write_report(
        [_item("low", 61), _item("high", 92), _item("mid", 75)],
        directory=tmp_path,
        now_fn=lambda: NOW,
    )

    ids = [json.loads(line)["id"] for line in paths.jsonl.read_text(encoding="utf-8").splitlines()]
    assert ids == ["high", "mid", "low"]


def test_unreachable_subreddits_are_recorded_too(tmp_path):
    paths = write_report(
        [], directory=tmp_path, failures=[("dead", "RuntimeError: 403")], now_fn=lambda: NOW
    )

    markdown = paths.markdown.read_text(encoding="utf-8")
    assert "## Not fetched" in markdown
    assert "dead: RuntimeError: 403" in markdown


def test_the_report_directory_is_created_on_demand(tmp_path):
    target = tmp_path / "deep" / "runs"
    write_report([], directory=target, now_fn=lambda: NOW)
    assert target.is_dir()


def test_main_writes_a_report_for_a_real_run(monkeypatch, tmp_path):
    from saipet.cli import main

    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    monkeypatch.chdir(tmp_path)
    lines: list[str] = []

    main([], print_fn=lines.append)

    written = list((tmp_path / "runs").iterdir())
    assert {p.suffix for p in written} == {".jsonl", ".md"}
    assert any(line.startswith("report: ") for line in lines)
