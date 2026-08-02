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


class Source:
    """Read-only fetch interface. Every concrete source only ever reads."""

    name = "base"

    def fetch(self, limit: int = 25) -> list[Candidate]:
        raise NotImplementedError


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
