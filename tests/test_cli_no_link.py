from saipet.cli import run_interactive
from saipet.review import ReviewItem, ReviewQueue
from saipet.sources.base import Candidate


def _queue_with_one_item():
    queue = ReviewQueue()
    queue.add(
        ReviewItem(
            candidate=Candidate(source="fixture", id="1", title="t", body="", permalink="p"),
            relevance_score=90,
            band="priority",
            draft="",
        )
    )
    return queue


def _fake_input(answers):
    it = iter(answers)
    return lambda _prompt: next(it)


def test_choosing_no_link_drops_the_saipen_aside_and_url():
    queue = _queue_with_one_item()
    item = queue.pending()[0]
    answers = _fake_input(["Try X, it fixed it for me.", "n", "y"])  # solution, no-link, approve
    run_interactive(queue, input_fn=answers, print_fn=lambda *_: None)

    assert "saipen" not in item.draft.lower()
    assert "github.com" not in item.draft.lower()
    assert item.status == "approved"


def test_default_still_mentions_saipen():
    queue = _queue_with_one_item()
    answers = _fake_input(["Try X, it fixed it for me.", "", "n"])  # blank -> mention, decline approve
    run_interactive(queue, input_fn=answers, print_fn=lambda *_: None)

    item = queue.pending()[0]
    assert "saipen" in item.draft.lower()
