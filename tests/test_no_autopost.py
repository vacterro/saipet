"""Red control for the ticket's core invariant: no code path posts/comments.

Static scan, not a mock -- a mock only proves the mocked call wasn't hit,
it can't prove the call doesn't exist anywhere in the tree. This greps the
actual source for the write-side praw/requests methods a submit path would
need and fails if any turn up outside this test file itself.
"""

from pathlib import Path

FORBIDDEN = [".submit(", ".reply(", ".comment(", "requests.post", "requests.put", ".delete("]

SAIPET_SRC = Path(__file__).resolve().parent.parent / "saipet"


def test_no_write_calls_anywhere_in_saipet():
    offenders = []
    for path in SAIPET_SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in FORBIDDEN:
            if token in text:
                offenders.append(f"{path}: {token}")
    assert not offenders, f"found write-capable calls: {offenders}"


def test_symptom_vocabulary_has_no_brand_literal():
    from saipet.config import SYMPTOMS

    assert not any("saipen" in s.lower() for s in SYMPTOMS)
