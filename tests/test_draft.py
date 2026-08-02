import pytest

from saipet.draft import build_draft, strip_aside


def test_draft_stays_useful_with_link_removed():
    draft = build_draft("Try splitting the job into checkpoints; that's what fixed it for me.")
    stripped = strip_aside(draft)
    assert "saipen" not in stripped.lower()
    assert len(stripped) > 20  # a real sentence survives, not a stub


def test_draft_link_is_a_trailing_aside_not_the_opening_line():
    draft = build_draft("Here's a concrete fix for your handoff bug: ...")
    first_line = draft.splitlines()[0]
    assert "github.com" not in first_line
    assert "saipen" not in first_line.lower()


def test_no_solution_no_draft():
    with pytest.raises(ValueError):
        build_draft("   ")
