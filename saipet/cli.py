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
    seen = SeenStore("seen.json")
    run_interactive(scout(FixtureSource([]), signal_fn=extract_signals, seen=seen))


if __name__ == "__main__":
    main()
