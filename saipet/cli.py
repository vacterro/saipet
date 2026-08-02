"""Manual-loop entrypoint: fetch -> score -> gate -> draft -> human approve.

Never posts anything. The only output of an approval is printed text for
the human to paste into Reddit themselves.
"""

from saipet.config import SYMPTOMS
from saipet.draft import build_draft
from saipet.review import ReviewItem, ReviewQueue
from saipet.scorer import gate, score
from saipet.signals import extract_signals
from saipet.sources.base import FixtureSource, Source
from saipet.store import SeenStore


def scout(source: Source, signal_fn, seen: SeenStore | None = None) -> ReviewQueue:
    """`signal_fn(candidate) -> dict` computes the weighted signals for one
    candidate; scoring itself stays a pure function (scorer.py) so it's
    testable without a source at all.

    `seen`, if given, is checked/marked for every fetched candidate --
    regardless of gate band -- so a second run never re-surfaces (or lets a
    human re-approve into) the same thread.
    """
    queue = ReviewQueue()
    for candidate in source.fetch():
        if seen is not None:
            if seen.has(candidate.source, candidate.id):
                continue
            seen.mark(candidate.source, candidate.id)
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


def main() -> None:
    print(f"symptom vocabulary: {', '.join(SYMPTOMS)}")
    print("No live source wired yet (needs REDDIT_CLIENT_ID/SECRET) -- using empty fixture.")
    seen = SeenStore("seen.json")
    run_interactive(scout(FixtureSource([]), signal_fn=extract_signals, seen=seen))


if __name__ == "__main__":
    main()
