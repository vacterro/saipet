import json

from saipet.report import (
    append_daily_findings,
    list_complete_reports,
    prune_reports,
    write_report,
)
from saipet.review import ReviewItem
from saipet.sources.base import Candidate

NOW = 1_800_000_000.0  # 2027-01-15T08:00:00Z


def _candidate(candidate_id):
    return Candidate(
        source="fixture",
        id=candidate_id,
        title="agent keeps losing context between sessions",
        body="Lost context, session state, handoff, resume, checkpoint, deterministic protocol.",
        permalink=f"https://reddit.com/r/testsub/{candidate_id}",
        subreddit="testsub",
        created_utc=NOW - 3600,
    )


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


def test_append_daily_findings_writes_one_line_per_item(tmp_path):
    item = {"id": "t1", "source": "fixture", "subreddit": "testsub", "title": "x",
            "score": 88, "band": "priority"}
    path = append_daily_findings(tmp_path, [item], NOW)

    assert path.name == "findings-2027-01-15.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["at"] == NOW
    assert record["id"] == "t1"


def test_append_daily_findings_with_no_items_returns_none(tmp_path):
    assert append_daily_findings(tmp_path, [], NOW) is None


def test_prune_reports_removes_only_old_pairs(tmp_path):
    import os
    import time as _time

    old = tmp_path / "20250101T000000Z.jsonl"
    old.write_text("{}", encoding="utf-8")
    (tmp_path / "20250101T000000Z.md").write_text("", encoding="utf-8")
    old_time = _time.time() - 30 * 86400
    os.utime(old, (old_time, old_time))
    os.utime(tmp_path / "20250101T000000Z.md", (old_time, old_time))
    fresh = write_report([], directory=tmp_path, now_fn=_time.time)

    removed = prune_reports(tmp_path, max_age_days=14, now_fn=_time.time)

    assert removed == 2
    assert not old.exists()
    assert fresh.jsonl.exists() and fresh.markdown.exists()


# CORE-001: cross-process stem reservation ------------------------------------

_MP_NOW = 1_893_456_000.0  # 2030-01-01T00:00:00Z


def _core001_worker(directory_str, tag, out_q):
    """Spawn-safe worker: two processes, one forced timestamp."""
    try:
        from pathlib import Path

        paths = write_report(
            [_item(f"{tag}-item", 50)],
            directory=Path(directory_str),
            now_fn=lambda: _MP_NOW,
        )
        out_q.put((tag, paths.jsonl.name, paths.markdown.name))
    except Exception as exc:  # pragma: no cover - reported, not hidden
        out_q.put((tag, f"ERROR {exc!r}", ""))


def test_two_processes_with_one_timestamp_reserve_distinct_stems(tmp_path):
    """CORE-001: stem choice was an exists() probe and every writer shared
    temp names -- synchronized same-second writers clobbered each other.
    Reservation is exclusive creation of the final path, arbitrated by the
    kernel in one syscall."""
    import multiprocessing as mp

    context = mp.get_context("spawn")
    out_q = context.Queue()
    procs = [
        context.Process(target=_core001_worker, args=(str(tmp_path), tag, out_q))
        for tag in ("a", "b")
    ]
    for proc in procs:
        proc.start()
    results = [out_q.get(timeout=60) for _ in procs]
    for proc in procs:
        proc.join(30)
        assert proc.exitcode == 0

    jsonl_names = sorted(name for _, name, _ in results)
    md_names = sorted(name for _, _, name in results)
    assert all(not name.startswith("ERROR") for name in jsonl_names)
    assert len(set(jsonl_names)) == 2, f"stems collided: {jsonl_names}"
    assert {name.replace(".jsonl", ".md") for name in jsonl_names} == set(md_names)

    # Each pair carries only its own writer's item -- no misattribution.
    seen_ids = []
    for name in jsonl_names:
        record = json.loads((tmp_path / name).read_text(encoding="utf-8").strip())
        seen_ids.append(record["id"])
    assert sorted(seen_ids) == ["a-item", "b-item"]

    # No staging litter; both pairs discoverable.
    assert not list(tmp_path.glob(".*.tmp"))
    assert len(list_complete_reports(tmp_path)) == 2


def test_every_committed_seen_identity_has_a_matching_durable_report(tmp_path, monkeypatch):
    """CORE-001 invariant at the Bridge seam: the non-durable scout path is
    allowed to mark a candidate seen only after its report pair is durable,
    so every identity in seen.json must appear in a report on disk."""
    from saipet.bridge import Bridge
    from saipet.sources.base import FixtureSource

    monkeypatch.setattr("saipet.policy.SUBREDDIT_ALLOWLIST", {"testsub"})
    monkeypatch.setattr("saipet.config.SUBREDDIT_ALLOWLIST", {"testsub"})

    bridge = Bridge(
        report_dir=tmp_path / "runs",
        seen_path=tmp_path / "seen.json",
        source_factory=lambda *_: FixtureSource([_candidate("c1")]),
        now_fn=lambda: NOW,
    )
    result = bridge.dispatch("scout", subreddits=["testsub"])
    assert result.ok and result.data["queued"] == 1

    # Seen-state is SQLite-backed (source, id); the identity must be marked.
    assert bridge.seen.has("fixture", "c1")

    report_records = []
    for jsonl_path in list_complete_reports(tmp_path / "runs"):
        report_records.extend(
            json.loads(line)
            for line in jsonl_path.read_text(encoding="utf-8").splitlines()
        )
    assert any(
        rec["source"] == "fixture" and rec["id"] == "c1" for rec in report_records
    )


# W2-007 + PERF-007: Markdown hygiene and streamed staging ----------------------


def test_crafted_titles_cannot_forge_extra_links_or_structure(tmp_path):
    """W2-007: untrusted titles used to be interpolated raw into
    [title](permalink) -- 'normal](https://evil.example)[spoof' produced a
    second clickable link. Titles/labels/failures are escaped now and the
    destination is validated."""
    attacker = Candidate(
        source="reddit",
        id="x1",
        title="normal](https://evil.example)[spoof](https://evil.example)",
        body="body",
        permalink="https://reddit.com/r/testsub/x1",
        subreddit="testsub](https://evil.example)",
        created_utc=NOW - 3600,
    )
    paths = write_report(
        [_item("a1", 90), ReviewItem(candidate=attacker, relevance_score=80,
         band="review", draft="", signals={}, discovered=NOW)],
        directory=tmp_path,
        now_fn=lambda: NOW,
    )
    md = paths.markdown.read_text(encoding="utf-8")

    # Exactly two links, both pointing at reddit.com -- the forged ones died.
    assert md.count("](http") == 2
    assert "evil.example](" not in md
    # No injected headings/list entries from title text.
    assert "\n#" not in md.replace("\n## Not fetched", "")


def test_a_non_https_permalink_renders_as_plain_text_not_a_link(tmp_path):
    bad = Candidate(
        source="reddit",
        id="b1",
        title="t",
        body="body",
        permalink="javascript:alert(1)",
        subreddit="testsub",
        created_utc=NOW - 3600,
    )
    item = ReviewItem(candidate=bad, relevance_score=80, band="review",
                      draft="", signals={}, discovered=NOW)
    paths = write_report([item], directory=tmp_path, now_fn=lambda: NOW)
    md = paths.markdown.read_text(encoding="utf-8")
    assert "](javascript" not in md


def test_streaming_output_is_byte_identical_to_the_join_based_renderer(tmp_path):
    """PERF-007 guardrail: streaming replaced whole-string staging; bytes on
    disk must be exactly what the previous renderer produced."""
    items = [_item("low", 61), _item("high", 92)]
    failures = [("dead", "RuntimeError: 403")]
    paths = write_report(items, directory=tmp_path, failures=failures,
                         now_fn=lambda: NOW)

    ranked = sorted(items, key=lambda i: -i.relevance_score)
    expected_lines = ["# scout run 20270115T080000Z", "",
                      f"{len(ranked)} candidate(s) queued for review.", ""]
    for item in ranked:
        c = item.candidate
        expected_lines.append(
            f"- **[{item.band}]** {item.relevance_score:.0f} -- "
            f"[{c.title}]({c.permalink}) (r/{c.subreddit or '?'})"
        )
    expected_lines += ["", "## Not fetched", ""]
    expected_lines += [f"- {s}: {e}" for s, e in failures]
    expected_md = "\n".join(expected_lines) + "\n"

    assert paths.markdown.read_text(encoding="utf-8") == expected_md
    from saipet.report import _record as _record_fn

    expected_jsonl = "".join(
        json.dumps(_record_fn(item), ensure_ascii=False) + "\n"
        for item in ranked
    )
    assert paths.jsonl.read_text(encoding="utf-8") == expected_jsonl
