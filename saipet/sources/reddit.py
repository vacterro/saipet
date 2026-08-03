import os
import time

from saipet.sources.base import Candidate, Source

DEFAULT_RETRIES = 2
DEFAULT_BACKOFF_SECONDS = 2.0


class RedditSource(Source):
    """Read-only fetch via a user-owned Reddit script app.

    Needs REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET / REDDIT_USER_AGENT in the
    environment -- register the app yourself at reddit.com/prefs/apps
    (script type). `read_only=True` means no Reddit username/password is
    ever supplied: this can search and read, and nothing in this class (or
    anywhere else in saipet/) calls .submit/.reply/.comment or any other
    write endpoint. See .saipen/KNOWLEDGE/reddit-access.md.

    Subreddits fail independently. A private, banned, misspelled or
    rate-limited one is an ordinary fact of scouting a dozen of them, not a
    reason to lose the ten that answered -- each is retried a few times and
    then recorded in `last_failures` rather than raised.
    """

    name = "reddit"

    def __init__(
        self,
        subreddits: list[str],
        symptoms: list[str],
        retries: int = DEFAULT_RETRIES,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        sleep_fn=time.sleep,
    ):
        self.subreddits = subreddits
        self.symptoms = symptoms
        self.retries = retries
        self.backoff_seconds = backoff_seconds
        self._sleep = sleep_fn
        # (subreddit, error text) for every subreddit this fetch gave up on.
        # Reset per fetch: it describes the last run, not a lifetime tally.
        self.last_failures: list[tuple[str, str]] = []

    def _client(self):
        import praw  # local import: only needed once real credentials exist

        client_id = os.environ["REDDIT_CLIENT_ID"]
        client_secret = os.environ["REDDIT_CLIENT_SECRET"]
        user_agent = os.environ.get("REDDIT_USER_AGENT", "saipet-scout/0.1 (read-only)")
        return praw.Reddit(
            client_id=client_id,
            client_secret=client_secret,
            user_agent=user_agent,
            read_only=True,
        )

    def _query(self) -> str:
        return " OR ".join(f'"{s}"' for s in self.symptoms)

    def _search_one(self, reddit, subreddit: str, query: str, limit: int) -> list[Candidate]:
        """One subreddit, with retries. Raises only if every attempt failed.

        The whole search is retried rather than resumed: `search()` returns a
        lazy listing, so a failure partway through has no resumable position
        and a partial page would silently look like a complete one.
        """
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                return [
                    Candidate(
                        source=self.name,
                        id=post.id,
                        title=post.title,
                        body=getattr(post, "selftext", "") or "",
                        permalink=f"https://reddit.com{post.permalink}",
                        subreddit=subreddit,
                        created_utc=post.created_utc,
                    )
                    for post in reddit.subreddit(subreddit).search(query, sort="new", limit=limit)
                ]
            except Exception as exc:  # noqa: BLE001 -- praw raises a wide family
                last_error = exc
                if attempt < self.retries:
                    self._sleep(self.backoff_seconds * (2**attempt))
        raise last_error  # type: ignore[misc]

    def fetch(self, limit: int = 25) -> list[Candidate]:
        reddit = self._client()
        query = self._query()
        self.last_failures = []
        out: list[Candidate] = []
        for sub in self.subreddits:
            try:
                out.extend(self._search_one(reddit, sub, query, limit))
            except Exception as exc:  # noqa: BLE001 -- see _search_one
                self.last_failures.append((sub, f"{type(exc).__name__}: {exc}"))
        return out
