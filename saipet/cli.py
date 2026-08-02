"""Manual-loop entrypoint: fetch -> score -> gate -> draft -> human approve.

Never posts anything. The only output of an approval is printed text for
the human to paste into Reddit themselves.
"""

from saipet.config import SYMPTOMS
from saipet.draft import build_draft
from saipet.review import ReviewItem, ReviewQueue
from saipet.scorer import gate, score
from saipet.sources.base import FixtureSource, Source


def scout(source: Source, signal_fn) -> ReviewQueue:
    """`signal_fn(candidate) -> dict` computes the weighted signals for one
    candidate; scoring itself stays a pure function (scorer.py) so it's
    testable without a source at all.
    """
    queue = ReviewQueue()
    for candidate in source.fetch():
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


def run_interactive(queue: ReviewQueue) -> None:
    items = sorted(queue.pending(), key=lambda i: -i.relevance_score)
    for item in items:
        c = item.candidate
        print(f"\n[{item.band.upper()}] score={item.relevance_score:.0f} {c.permalink}")
        print(f"  {c.title}")
        solution = input("  Solve-first reply (blank to skip): ").strip()
        if not solution:
            continue
        item.draft = build_draft(solution)
        print("\n--- draft (paste into Reddit yourself if you approve) ---")
        print(item.draft)
        if input("Approve? [y/N] ").strip().lower() == "y":
            queue.approve(item)
            print("Approved -- not posted. Copy the text above manually.")


def main() -> None:
    print(f"symptom vocabulary: {', '.join(SYMPTOMS)}")
    print("No live source wired yet (needs REDDIT_CLIENT_ID/SECRET) -- using empty fixture.")
    run_interactive(scout(FixtureSource([]), signal_fn=lambda c: {}))


if __name__ == "__main__":
    main()
