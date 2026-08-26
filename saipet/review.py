from dataclasses import dataclass, field

from saipet.sources.base import Candidate

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"


@dataclass
class ReviewItem:
    candidate: Candidate
    relevance_score: float
    band: str
    draft: str
    status: str = STATUS_PENDING
    # The signal breakdown behind `relevance_score`. Kept so a report can
    # show why a thread scored what it did: a bare number is not something
    # a human can argue with a week later.
    signals: dict = field(default_factory=dict)
    # When this item first entered a review queue. Persisted with the item
    # so a restored queue keeps the original discovery time.
    discovered: float = 0.0

    def identity(self) -> tuple[str, str]:
        """The canonical candidate identity: (source, id). Two sources with
        the same id are distinct findings, not the same review item."""
        return (self.candidate.source, self.candidate.id)


class ReviewQueue:
    """Holds scored candidates for a human to look at.

    No method here calls out to any network write API -- there isn't one
    in this codebase. `approve()` only flips a status flag and hands back
    the text; posting it is a manual, human action outside this program.

    PERF-006: keeps a synchronized `(source,id) -> ReviewItem` dict
    alongside the ordered list so exact-identity lookup is O(1) instead
    of O(P). The list preserves insertion order for display/sorting; the
    index is updated on every add/replace/remove.
    """

    def __init__(self, items=None):
        self._items: list[ReviewItem] = list(items) if items else []
        # PERF-006: canonical-identity index synced with the list.
        self._by_identity: dict[tuple[str, str], ReviewItem] = {
            item.identity(): item for item in self._items
        }

    def add(self, item: ReviewItem) -> None:
        self._items.append(item)
        self._by_identity[item.identity()] = item

    def pending(self) -> list[ReviewItem]:
        return [i for i in self._items if i.status == STATUS_PENDING]

    def find(self, source: str, candidate_id: str) -> ReviewItem | None:
        """Exact canonical-identity lookup (PERF-006: O(1) via index)."""
        return self._by_identity.get((source, candidate_id))

    def find_by_id(self, candidate_id: str) -> ReviewItem | None:
        """Id-only lookup for callers that only carry an id. Ambiguous ids
        (the same id from two sources) return the first match; Bridge verbs
        reject that case explicitly rather than silently picking one."""
        for item in self._items:
            if item.candidate.id == candidate_id:
                return item
        return None

    def replace(self, old: ReviewItem, new: ReviewItem) -> bool:
        """Swap one item object for another, preserving position."""
        for index, item in enumerate(self._items):
            if item is old or item.identity() == old.identity():
                self._items[index] = new
                self._by_identity[new.identity()] = new
                return True
        return False

    def remove(self, source: str, candidate_id: str) -> bool:
        """Remove one item by canonical identity (PERF-005: drops terminal
        items from the live queue after approve/reject). Returns True iff
        the item was present."""
        key = (source, candidate_id)
        item = self._by_identity.pop(key, None)
        if item is not None:
            self._items.remove(item)
            return True
        return False

    def approve(self, item: ReviewItem) -> str:
        """Takes the item itself, not a position -- pending() is sorted by
        the caller for display, so an index into that view would land on
        the wrong element in self._items (found in review, T-001)."""
        item.status = STATUS_APPROVED
        return item.draft

    def reject(self, item: ReviewItem) -> None:
        item.status = STATUS_REJECTED