"""Persist what a scout run found.

A `ReviewQueue` lives in memory and dies with the process, so until now
every scored candidate was lost the moment the terminal closed. Two files
per run, because two different readers need them:

- `<stamp>.jsonl` -- one JSON object per queued item, for anything that
  parses (a later agent, a diff between runs, a spreadsheet);
- `<stamp>.md` -- the same run in a few lines a human actually reads.

Both are written even when a run queued nothing. An empty report is a
result -- "the scout ran and found nothing" and "the scout never ran" are
different facts, and a missing file cannot tell them apart.
"""

import json
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_REPORT_DIR = "runs"


@dataclass(frozen=True)
class ReportPaths:
    jsonl: Path
    markdown: Path


def _stamp(now: float) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))


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
    """
    report_dir = Path(directory)
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = _stamp(now_fn())
    paths = ReportPaths(report_dir / f"{stamp}.jsonl", report_dir / f"{stamp}.md")

    ranked = sorted(items, key=lambda i: -i.relevance_score)

    with paths.jsonl.open("w", encoding="utf-8") as handle:
        for item in ranked:
            handle.write(json.dumps(_record(item), ensure_ascii=False) + "\n")

    lines = [f"# scout run {stamp}", "", f"{len(ranked)} candidate(s) queued for review.", ""]
    for item in ranked:
        candidate = item.candidate
        lines.append(
            f"- **[{item.band}]** {item.relevance_score:.0f} -- "
            f"[{candidate.title}]({candidate.permalink}) (r/{candidate.subreddit or '?'})"
        )
    if failures:
        lines += ["", "## Not fetched", ""]
        lines += [f"- {subreddit}: {error}" for subreddit, error in failures]
    paths.markdown.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return paths
