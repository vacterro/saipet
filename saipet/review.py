from dataclasses import dataclass

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


class ReviewQueue:
    """Holds scored candidates for a human to look at.

    No method here calls out to any network write API -- there isn't one
    in this codebase. `approve()` only flips a status flag and hands back
    the text; posting it is a manual, human action outside this program.
    """

    def __init__(self):
        self._items: list[ReviewItem] = []

    def add(self, item: ReviewItem) -> None:
        self._items.append(item)

    def pending(self) -> list[ReviewItem]:
        return [i for i in self._items if i.status == STATUS_PENDING]

    def approve(self, item: ReviewItem) -> str:
        """Takes the item itself, not a position -- pending() is sorted by
        the caller for display, so an index into that view would land on
        the wrong element in self._items (found in review, T-001)."""
        item.status = STATUS_APPROVED
        return item.draft

    def reject(self, item: ReviewItem) -> None:
        item.status = STATUS_REJECTED
