from dataclasses import dataclass


@dataclass(frozen=True)
class Candidate:
    """One fetched post/thread, source-agnostic.

    `permalink` is a full URL a human can open to read and reply manually --
    this is the only thing any source ever hands back. Nothing in this
    dataclass, or anything that produces it, carries a way to post to it.
    """

    source: str
    id: str
    title: str
    body: str
    permalink: str
    subreddit: str = ""
    created_utc: float = 0.0


HEALTHY = "healthy"
DEGRADED = "degraded"
FAILED = "failed"


@dataclass(frozen=True)
class FetchHealth:
    """How much of what we asked for actually came back.

    Two states are not enough, and that gap let a total outage look like a
    quiet night: a source that reads none of its targets still returned a
    list, an empty one, indistinguishable from "nobody posted". The count
    of targets is only known where the fetch happens, so the verdict is
    made here rather than inferred downstream from a failure list whose
    emptiness also means "nothing was configured".
    """

    attempted: int = 0
    failed: tuple = ()  # (target, error) pairs

    @property
    def succeeded(self) -> int:
        return self.attempted - len(self.failed)

    @property
    def state(self) -> str:
        if self.attempted == 0:
            # Nothing was asked for. Not a failure of the source -- the
            # caller has an empty allowlist, which its own check reports.
            return HEALTHY
        if not self.failed:
            return HEALTHY
        return FAILED if self.succeeded == 0 else DEGRADED

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "attempted": self.attempted,
            "succeeded": self.succeeded,
            "failed": [{"target": target, "error": err} for target, err in self.failed],
        }


class Source:
    """Read-only fetch interface. Every concrete source only ever reads."""

    name = "base"

    def fetch(self, limit: int = 25) -> list[Candidate]:
        raise NotImplementedError

    @property
    def health(self) -> FetchHealth:
        """The last fetch's health. Sources that cannot fail say so."""
        return FetchHealth(attempted=1)


class FixtureSource(Source):
    """Local, no-network source for development/tests.

    Also the default fallback until a platform's own read credentials
    (e.g. Reddit's client_id/client_secret) are supplied by the user --
    see sources/reddit.py and .saipen/KNOWLEDGE/reddit-access.md.
    """

    name = "fixture"

    def __init__(self, candidates: list[Candidate] | None = None):
        self._candidates = list(candidates) if candidates else []

    def fetch(self, limit: int = 25) -> list[Candidate]:
        return self._candidates[:limit]
