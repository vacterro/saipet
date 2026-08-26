"""Manual-loop entrypoint: fetch -> score -> gate -> draft -> human approve.

Never posts anything. The only output of an approval is printed text for
the human to paste into Reddit themselves.
"""

import argparse
import os
import time

from saipet import config
from saipet.config import SUBREDDIT_ALLOWLIST, SYMPTOMS
from saipet.draft import build_draft
from saipet.policy import is_subreddit_allowed
from saipet.report import DEFAULT_REPORT_DIR, write_report
from saipet.review import ReviewItem, ReviewQueue
from saipet.runtime_config import (
    DEFAULT_CONFIG_PATH,
    ConfigError,
    apply_overrides,
    load_overrides,
)
from saipet.scorer import gate, score
from saipet.signals import extract_signals
from saipet.sources.base import FixtureSource, Source
from saipet.sources.reddit import RedditSource
from saipet.store import SeenStore

DEFAULT_LIMIT = 25


def is_fresh(candidate, max_age_hours: float | None, now: float) -> bool:
    """A candidate is stale once it is older than `max_age_hours`.

    `created_utc == 0.0` means the source never supplied a timestamp (every
    FixtureSource candidate, for one): unknown age is not evidence of age,
    so those are kept. `max_age_hours=None` disables the window entirely.
    """
    if max_age_hours is None or not candidate.created_utc:
        return True
    return (now - candidate.created_utc) <= max_age_hours * 3600


def scout(
    source: Source,
    signal_fn,
    seen: SeenStore | None = None,
    persist=None,
    limit: int = DEFAULT_LIMIT,
    max_age_hours: float | None = None,
    now_fn=time.time,
) -> ReviewQueue:
    """`signal_fn(candidate) -> dict` computes the weighted signals for one
    candidate; scoring itself stays a pure function (scorer.py) so it's
    testable without a source at all.

    `seen`, if given, is checked for every candidate and durably marked for
    every one admitted to the queue -- and only those. Gate-dropped
    candidates stay reconsiderable, so changing the freshness window, the
    allowlist or the score thresholds can reconsider them (CORE-005).

    Durability ordering: `persist(queue, source)`, when supplied, runs BEFORE
    any seen mark, so a crash between "write the durable record" and "mark
    seen" never loses a finding (W2-001). The seen batch is one atomic write
    (PERF-001) that publishes to memory only after the disk write succeeds.

    The same `(source, id)` returned twice by one fetch is deduplicated
    within the run, so duplicates never enter the queue, the report or the
    monitor feed (W2-006).

    `max_age_hours` drops threads too old to be worth answering; `now_fn` is
    injectable so the window is testable without freezing the clock.
    """
    queue = ReviewQueue()
    now = now_fn()
    seen_this_run: set[tuple[str, str]] = set()
    admitted: list[tuple[str, str]] = []
    claimed: list[tuple[str, str]] = []
    # CORE-006: a store with `claim` admits atomically -- the check and the
    # mark are one SQLite statement, so two processes can never both win.
    claim = getattr(seen, "claim", None)
    for candidate in source.fetch(limit):
        identity = (candidate.source, candidate.id)
        if identity in seen_this_run:
            continue  # W2-006: same-run duplicate
        seen_this_run.add(identity)
        if seen is not None and seen.has(candidate.source, candidate.id):
            continue
        if not is_fresh(candidate, max_age_hours, now):
            continue
        if not is_subreddit_allowed(candidate.subreddit):
            continue
        signals = signal_fn(candidate)
        relevance_score = score(signals)
        band = gate(relevance_score)
        if band == "ignore":
            continue
        if claim is not None:
            # Claim AFTER the gates: gate-dropped candidates stay
            # reconsiderable because they were never claimed.
            if not claim(candidate.source, candidate.id):
                continue  # another process admitted it first
            claimed.append(identity)
        queue.add(
            ReviewItem(
                candidate=candidate,
                relevance_score=relevance_score,
                band=band,
                draft="",  # filled in only once a human supplies a real solution
                signals=dict(signals),
                discovered=now,
            )
        )
        admitted.append(identity)
    if persist is not None:
        try:
            persist(queue, source)
        except Exception:
            # W2-001 durability ordering: nothing durable landed, so the
            # claims are undone and every candidate stays reconsiderable.
            if claim is not None:
                for identity in claimed:
                    seen.release(*identity)
            raise
    # W2-001: the FINAL seen commit happens only after the durable record
    # (review store / report / outbox) landed. Provisional claims are now
    # promoted to finalized seen records; a crash before this point left the
    # candidate reconsiderable. A claim path with no persist callback (a
    # degenerate caller) finalizes directly -- its claim is the only dedup
    # record there is.
    if claim is not None and claimed:
        finalize = getattr(seen, "finalize_many", None)
        if finalize is not None:
            finalize(claimed)
    elif seen is not None and claim is None:
        seen.mark_many(admitted)
    return queue


def build_source(subreddits: list[str], symptoms: list[str]) -> Source:
    """Live Reddit when the user's own read credentials are in the
    environment, the empty fixture otherwise.

    The fallback is deliberate and documented in README.md: a missing
    credential is a normal state (nobody has registered a script app yet),
    not an error worth crashing a run over.
    """
    if os.environ.get("REDDIT_CLIENT_ID") and os.environ.get("REDDIT_CLIENT_SECRET"):
        return RedditSource(subreddits, symptoms)
    return FixtureSource([])


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="saipet",
        description=(
            "Read-only internet scout. Finds threads describing a real problem, "
            "scores them, and drafts a solve-first reply for a human to approve. "
            "Never posts anything."
        ),
    )
    parser.add_argument(
        "--subreddit",
        action="append",
        metavar="NAME",
        help="subreddit to search; repeatable. Defaults to the configured allowlist.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"max posts fetched per subreddit (default: {DEFAULT_LIMIT})",
    )
    parser.add_argument(
        "--since-hours",
        type=float,
        default=None,
        metavar="H",
        help=(
            "ignore threads older than H hours (default: config.MAX_AGE_HOURS, "
            "currently 168). Use 0 to disable the window."
        ),
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help=(
            "fetch, score, gate and write the report, then stop -- no prompts. "
            "For a scheduled or agent-driven run, which has no stdin to block on."
        ),
    )
    parser.add_argument(
        "--report-dir",
        default=DEFAULT_REPORT_DIR,
        metavar="DIR",
        help=f"where each run's .jsonl + .md report is written (default: {DEFAULT_REPORT_DIR})",
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        metavar="PATH",
        help=(
            "JSON file overriding symptoms, subreddit_allowlist, weights and the "
            f"gate thresholds (default: {DEFAULT_CONFIG_PATH}; absent = built-in defaults)"
        ),
    )
    args = parser.parse_args(argv)
    if args.since_hours is not None:
        import math
        if not math.isfinite(args.since_hours) or args.since_hours < 0:
            parser.error("--since-hours must be a finite number >= 0 (0 disables the window)")
    if args.limit < 1:
        parser.error("--limit must be at least 1")
    return args


def run_interactive(queue: ReviewQueue, input_fn=input, print_fn=print) -> None:
    """`input_fn`/`print_fn` are injectable so this loop is testable without
    faking stdin (RFC-style: no side channel a test has to fight)."""
    items = sorted(queue.pending(), key=lambda i: -i.relevance_score)
    for item in items:
        c = item.candidate
        print_fn(f"\n[{item.band.upper()}] score={item.relevance_score:.0f} {c.permalink}")
        print_fn(f"  {c.title}")
        solution = input_fn("  Solve-first reply (blank to skip): ").strip()
        if not solution:
            continue
        mention = input_fn("  Mention SAIPEN link? [Y/n] ").strip().lower() != "n"
        item.draft = build_draft(solution, mention_saipen=mention)
        print_fn("\n--- draft (paste into Reddit yourself if you approve) ---")
        print_fn(item.draft)
        if input_fn("Approve? [y/N] ").strip().lower() == "y":
            queue.approve(item)
            print_fn("Approved -- not posted. Copy the text above manually.")


def main(argv: list[str] | None = None, print_fn=print, input_fn=input) -> None:
    args = parse_args(argv)

    try:
        applied = apply_overrides(load_overrides(args.config))
    except ConfigError as exc:
        # A broken config file is a user mistake with an obvious fix, not a
        # crash worth a traceback.
        print_fn(f"config error: {exc}")
        raise SystemExit(2) from exc
    if applied:
        print_fn(f"config: {args.config} overrides {', '.join(applied)}")

    subreddits = args.subreddit or sorted(SUBREDDIT_ALLOWLIST)
    source = build_source(subreddits, SYMPTOMS)

    print_fn(f"source: {source.name}")
    print_fn(f"subreddits: {', '.join(subreddits) if subreddits else '(none)'}")
    print_fn(f"symptom vocabulary: {', '.join(SYMPTOMS)}")
    if source.name == FixtureSource.name:
        print_fn(
            "No REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET in the environment "
            "-- running against the empty fixture, nothing will be fetched."
        )
    if not SUBREDDIT_ALLOWLIST:
        print_fn(
            "WARNING: the subreddit allowlist is empty, so policy drops every "
            "candidate before scoring. Add subreddits to config.SUBREDDIT_ALLOWLIST "
            "once you have read their self-promo rules."
        )

    since_hours = config.MAX_AGE_HOURS if args.since_hours is None else args.since_hours
    max_age_hours = since_hours or None  # 0 (or 0.0) means "no window at all"
    print_fn(
        f"freshness window: {max_age_hours}h"
        if max_age_hours
        else "freshness window: disabled"
    )

    seen = SeenStore("seen.json")
    report_paths: dict = {}

    def _persist(queue, source):
        # The report is the durable record for a non-ReviewStore run: it must
        # complete before scout() marks anything seen (W2-001).
        report_paths["paths"] = write_report(
            queue.pending(),
            directory=args.report_dir,
            failures=getattr(source, "last_failures", []),
        )

    queue = scout(
        source,
        signal_fn=extract_signals,
        seen=seen,
        persist=_persist,
        limit=args.limit,
        max_age_hours=max_age_hours,
    )
    for subreddit, error in getattr(source, "last_failures", []):
        # Never silent: a subreddit that answered nothing and a subreddit that
        # could not be reached look identical in the queue.
        print_fn(f"WARNING: {subreddit} could not be fetched -- {error}")

    print_fn(f"{len(queue.pending())} candidate(s) queued for review.")

    paths = report_paths["paths"]
    print_fn(f"report: {paths.jsonl} / {paths.markdown}")

    if args.report_only:
        # Nothing below this line may touch stdin: an unattended run that
        # blocks on a prompt hangs until something kills it.
        return

    run_interactive(queue, input_fn=input_fn, print_fn=print_fn)


if __name__ == "__main__":
    main()
