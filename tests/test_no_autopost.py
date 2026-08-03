"""Red control for the ticket's core invariant: no code path posts/comments.

Static scan, not a mock -- a mock only proves the mocked call wasn't hit,
it can't prove the call doesn't exist anywhere in the tree. This greps the
actual source for the write-side praw/requests methods a submit path would
need and fails if any turn up outside this test file itself.
"""

from pathlib import Path

FORBIDDEN = [".submit(", ".reply(", ".comment(", "requests.post", "requests.put", ".delete("]

# The bridge (T-013) accepts verbs from an external caller and acts on text
# fetched from the public internet. Nothing in this package may reach an
# execution primitive: that is what turns "fetched some posts" into "ran
# what a stranger wrote". Same static-scan reasoning as FORBIDDEN above.
NO_EXEC = ["subprocess", "os.system", "os.popen", "eval(", "exec(", "__import__", "pty.spawn"]

SAIPET_SRC = Path(__file__).resolve().parent.parent / "saipet"


def _scan(tokens: list[str]) -> list[str]:
    offenders = []
    for path in SAIPET_SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in tokens:
            if token in text:
                offenders.append(f"{path}: {token}")
    return offenders


def test_no_write_calls_anywhere_in_saipet():
    assert not _scan(FORBIDDEN), f"found write-capable calls: {_scan(FORBIDDEN)}"


def test_no_execution_primitive_anywhere_in_saipet():
    assert not _scan(NO_EXEC), f"found execution primitives: {_scan(NO_EXEC)}"


def test_symptom_vocabulary_has_no_brand_literal():
    from saipet.config import SYMPTOMS

    assert not any("saipen" in s.lower() for s in SYMPTOMS)
