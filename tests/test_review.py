from saipet.review import ReviewItem, ReviewQueue
from saipet.sources.base import Candidate


def _item(id_):
    return ReviewItem(
        candidate=Candidate(source="fixture", id=id_, title=id_, body="", permalink=id_),
        relevance_score=70,
        band="review",
        draft=f"draft-{id_}",
    )


def test_approve_targets_the_right_item_regardless_of_display_order():
    queue = ReviewQueue()
    a, b, c = _item("a"), _item("b"), _item("c")
    for it in (a, b, c):
        queue.add(it)

    # caller sorts/reorders its own view before approving -- approve() must
    # not reinterpret a position in that view as an index into the queue
    displayed = sorted(queue.pending(), key=lambda i: i.candidate.id, reverse=True)
    assert [i.candidate.id for i in displayed] == ["c", "b", "a"]

    returned = queue.approve(displayed[0])  # "c", first in the reordered view
    assert returned == "draft-c"
    assert c.status == "approved"
    assert a.status == "pending"
    assert b.status == "pending"
