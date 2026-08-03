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
    limit: int = DEFAULT_LIMIT,
    max_age_hours: float | None = None,
    now_fn=time.time,
) -> ReviewQueue:
    """`signal_fn(candidate) -> dict` computes the weighted signals for one
    candidate; scoring itself stays a pure function (scorer.py) so it's
    testable without a source at all.

    `seen`, if given, is checked/marked for every fetched candidate --
    regardless of gate band -- so a second run never re-surfaces (or lets a
    human re-approve into) the same thread.

    `max_age_hours` drops threads too old to be worth answering; `now_fn` is
    injectable so the window is testable without freezing the clock.
    """
    queue = ReviewQueue()
    now = now_fn()
    for candidate in source.fetch(limit):
        if seen is not None:
            if seen.has(candidate.source, candidate.id):
                continue
            seen.mark(candidate.source, candidate.id)
        if not is_fresh(candidate, max_age_hours, now):
            continue
        if not is_subreddit_allowed(candidate.subreddit):
            continue
        relevance_score = score(signal_fn(candidate))
        band = gate(relevance_score)
        if band == "ignore":
            continue
        queue.add(
            ReviewItem(
                candidate=candidate,
                relevance_score=relevance_score,
                band=band,
                draft="",  # filled in only once a human supplies a real solution
            )
        )
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
        "--config",
        default=DEFAULT_CONFIG_PATH,
        metavar="PATH",
        help=(
            "JSON file overriding symptoms, subreddit_allowlist, weights and the "
            f"gate thresholds (default: {DEFAULT_CONFIG_PATH}; absent = built-in defaults)"
        ),
    )
    args = parser.parse_args(argv)
    if args.since_hours is not None and args.since_hours < 0:
        # Left unchecked this silently inverts the window: every dated
        # candidate becomes "older than the cutoff" and the run finds nothing.
        parser.error("--since-hours must not be negative (0 disables the window)")
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


def main(argv: list[str] | None = None, print_fn=print) -> None:
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
    queue = scout(
        source,
        signal_fn=extract_signals,
        seen=seen,
        limit=args.limit,
        max_age_hours=max_age_hours,
    )
    print_fn(f"{len(queue.pending())} candidate(s) queued for review.")
    run_interactive(queue, print_fn=print_fn)


if __name__ == "__main__":
    main()
