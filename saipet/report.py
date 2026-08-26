"""Persist what a scout run found.

A `ReviewQueue` lives in memory and dies with the process, so until now
every scored candidate was lost the moment the terminal closed. Two files
per run, because two different readers need them:

- `<stem>.jsonl` -- one JSON object per queued item, for anything that
  parses (a later agent, a diff between runs, a spreadsheet);
- `<stem>.md` -- the same run in a few lines a human actually reads.

Both are written even when a run queued nothing. An empty report is a
result -- "the scout ran and found nothing" and "the scout never ran" are
different facts, and a missing file cannot tell them apart.

Report identity is collision-safe (CORE-006): two runs in the same second
get distinct stems (`<stamp>-2`, `<stamp>-3`, ...) instead of silently
overwriting one another. The stem itself is reserved by exclusive creation
of the final `.jsonl` path (CORE-001), so concurrent processes cannot race
an exists() probe into sharing one stem. A report pair commits
transactionally (W2-011): both halves are staged to private temp files,
fsynced, then renamed; discovery lists only stems where both halves exist,
so a crash mid-commit never advertises a partial run as complete.
"""

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from saipet.textutil import markdown_escape, safe_link_destination

DEFAULT_REPORT_DIR = "runs"


@dataclass(frozen=True)
class ReportPaths:
    jsonl: Path
    markdown: Path


def _stamp(now: float) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))


def _reserve_stem(report_dir: Path, stamp: str) -> str:
    """Reserve a readable timestamp stem across processes (CORE-001).

    The reservation IS the exclusive creation of the final `.jsonl` path:
    whoever wins `O_CREAT | O_EXCL` owns the stem outright, and every loser
    moves on to `-2`, `-3`, ... The kernel arbitrates in one syscall, so no
    exists()-probe TOCTOU window exists. The empty placeholder becomes the
    real file at publication (`os.replace` over it); a run that dies before
    publication leaves an orphan half that discovery never lists (W2-011)
    and retention later prunes.
    """
    candidate = stamp
    n = 1
    while True:
        try:
            fd = os.open(
                report_dir / f"{candidate}.jsonl",
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
        except FileExistsError:
            n += 1
            candidate = f"{stamp}-{n}"
            continue
        os.close(fd)
        return candidate


def _record(item) -> dict:
    candidate = item.candidate
    return {
        "source": candidate.source,
        "id": candidate.id,
        "subreddit": candidate.subreddit,
        "title": candidate.title,
        "permalink": candidate.permalink,
        "created_utc": candidate.created_utc,
        "score": item.relevance_score,
        "band": item.band,
        "signals": item.signals,
    }


def _stage_text(path: Path, text: str) -> None:
    """Write a fully-flushed, fsynced temp file ready to be renamed."""
    with path.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def write_report(
    items: list,
    directory: str | Path = DEFAULT_REPORT_DIR,
    failures: list | None = None,
    now_fn=time.time,
) -> ReportPaths:
    """Write both files for one run and return where they landed.

    `failures` is `RedditSource.last_failures` -- subreddits that could not
    be read. They belong in the report for the same reason the CLI warns
    about them: a run that reached nine subreddits out of ten is not the
    same run as one that reached all ten, and the item list alone cannot
    show the difference.

    Raises on any write failure BEFORE the pair is published; an incomplete
    run never leaves a discoverable report behind (W2-011).
    """
    report_dir = Path(directory)
    report_dir.mkdir(parents=True, exist_ok=True)
    stem = _reserve_stem(report_dir, _stamp(now_fn()))
    jsonl_path = report_dir / f"{stem}.jsonl"
    md_path = report_dir / f"{stem}.md"
    # Private staging names (CORE-001): even two writers that somehow held
    # one stem could not collide each other's temp files mid-rename.
    unique = f"{os.getpid()}-{uuid.uuid4().hex}"
    jsonl_tmp = report_dir / f".{stem}.{unique}.jsonl.tmp"
    md_tmp = report_dir / f".{stem}.{unique}.md.tmp"

    ranked = sorted(items, key=lambda i: -i.relevance_score)

    # W2-007 + PERF-007: stream both halves line by line in ranked order --
    # no report-sized strings are ever held in memory -- and every piece of
    # external text is Markdown-escaped, with link destinations validated,
    # so a crafted title cannot forge extra links or structure. Byte output
    # is identical to the previous join-based renderer.
    published_jsonl = False
    try:
        jsonl_tmp_handle = jsonl_tmp.open("w", encoding="utf-8")
        md_tmp_handle = md_tmp.open("w", encoding="utf-8")
        with jsonl_tmp_handle as jh, md_tmp_handle as mh:
            mh.write(f"# scout run {stem}\n\n{len(ranked)} candidate(s) queued for review.\n\n")
            for item in ranked:
                record = _record(item)
                jh.write(json.dumps(record, ensure_ascii=False) + "\n")
                c = item.candidate
                title = markdown_escape(c.title)
                permalink = safe_link_destination(c.permalink)
                subreddit = markdown_escape(str(c.subreddit or "?"))
                if permalink:
                    # Bare parentheses destination when possible -- byte-
                    # identical to the historical renderer -- falling back to
                    # the <...> form only for URLs containing spaces/parens.
                    dest = (
                        f"<{permalink}>"
                        if any(ch in permalink for ch in " ()")
                        else permalink
                    )
                    mh.write(
                        f"- **[{markdown_escape(item.band)}]** "
                        f"{item.relevance_score:.0f} -- [{title}]({dest}) (r/{subreddit})\n"
                    )
                else:
                    mh.write(
                        f"- **[{markdown_escape(item.band)}]** "
                        f"{item.relevance_score:.0f} -- {title} (r/{subreddit})\n"
                    )
            if failures:
                mh.write("\n## Not fetched\n\n")
                for sub, error_text in failures:
                    mh.write(f"- {markdown_escape(sub)}: {markdown_escape(error_text)}\n")
            for handle in (jh, mh):
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(jsonl_tmp, jsonl_path)
        published_jsonl = True
        os.replace(md_tmp, md_path)
    except BaseException:
        for tmp in (jsonl_tmp, md_tmp):
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
        if not published_jsonl:
            try:
                jsonl_path.unlink()
            except FileNotFoundError:
                pass
        raise

    return ReportPaths(jsonl_path, md_path)


def list_complete_reports(directory: str | Path = DEFAULT_REPORT_DIR) -> list[Path]:
    """The `.jsonl` paths of runs where both halves of the pair exist.

    Orphaned halves from an interrupted commit are deliberately not listed:
    an incomplete run is not evidence of a completed one.
    """
    report_dir = Path(directory)
    pairs = []
    for jsonl in sorted(report_dir.glob("*.jsonl")):
        if jsonl.with_suffix(".md").exists():
            pairs.append(jsonl)
    return pairs


def append_daily_findings(
    directory: str | Path,
    items: list[dict],
    now: float,
) -> Path | None:
    """Append one cycle's finding item-views to the day's findings feed.

    T-025: one findings-YYYY-MM-DD.jsonl per day instead of a report pair per
    cycle, so an unattended monitor does not accumulate thousands of files.
    Returns the file path, or None when there was nothing to append.
    """
    if not items:
        return None
    report_dir = Path(directory)
    report_dir.mkdir(parents=True, exist_ok=True)
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    path = report_dir / f"findings-{day}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        for item in items:
            handle.write(json.dumps({"at": now, **item}, ensure_ascii=False) + "\n")
    return path


def prune_reports(
    directory: str | Path,
    max_age_days: float,
    now_fn=time.time,
) -> int:
    """Delete report pairs (and any orphaned halves) older than the retention
    window. Returns the number of files removed. Purely best-effort tidy: a
    file that cannot be read or removed is skipped, never fatal.
    """
    report_dir = Path(directory)
    if not report_dir.exists():
        return 0
    cutoff = now_fn() - max_age_days * 86400
    removed = 0
    for jsonl in report_dir.glob("*.jsonl"):
        if jsonl.name.startswith("findings-"):
            continue  # daily findings feed is separate from report pairs
        try:
            if jsonl.stat().st_mtime < cutoff:
                md = jsonl.with_suffix(".md")
                jsonl.unlink()
                removed += 1
                if md.exists():
                    md.unlink()
                    removed += 1
        except OSError:
            continue
    return removed