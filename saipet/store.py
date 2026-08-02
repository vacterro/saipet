import json
from pathlib import Path


class SeenStore:
    """Tracks which (source, id) candidates have already been processed.

    Local JSON, loaded once at construction and rewritten on every `mark`.
    That's the whole durability story -- fine for the single-process,
    low-volume scout this is; a concurrent-writer version would need more.
    """

    def __init__(self, path: str | Path = "seen.json"):
        self.path = Path(path)
        self._seen: set[str] = self._load()

    def _key(self, source: str, candidate_id: str) -> str:
        return f"{source}:{candidate_id}"

    def _load(self) -> set[str]:
        if self.path.exists():
            return set(json.loads(self.path.read_text(encoding="utf-8")))
        return set()

    def has(self, source: str, candidate_id: str) -> bool:
        return self._key(source, candidate_id) in self._seen

    def mark(self, source: str, candidate_id: str) -> None:
        self._seen.add(self._key(source, candidate_id))
        self.path.write_text(json.dumps(sorted(self._seen)), encoding="utf-8")
