import os
import time

from saipet.sources.base import Candidate, FetchHealth, Source

DEFAULT_RETRIES = 2
DEFAULT_BACKOFF_SECONDS = 2.0

# PERF-004: errors that will never succeed on retry -- the target does not
# exist, is private or banned, or the request itself is malformed. Matched
# against exception class name plus message text, because praw's exception
# family is wide and importing it here would defeat the lazy import.
_PERMANENT_MARKERS = (
    "notfound",
    "forbidden",
    "redirect",
    "404",
    "403",
    "private",
    "banned",
    "nonexistent",
    "does not exist",
)

# PERF-005: authentication/session failures -- a dead token poisons every
# target, so a fetch where everything failed this way must not keep serving
# the same cached client.
_AUTH_FAILURE_MARKERS = (
    "401",
    "unauthorized",
    "invalid_grant",
    "token",
    "expired",
)


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

    PERF-004: retries run in ROUNDS, not per-target -- every target gets one
    attempt before any backoff sleeps once for the whole round. Exhausting
    each target's retries before advancing multiplied a broad outage's delay
    by the number of failing targets; N targets cost two shared sleeps now,
    not 2N individual ones. Known-permanent errors skip the remaining rounds.
    """

    name = "reddit"

    # One read-only client per effective credential tuple, shared across the
    # long-lived daemon's cycles. Instances are cheap and per-cycle; the
    # client (connection pool, token state) is the thing worth keeping alive.
    _CLIENT_CACHE: dict[tuple[str, str, str], object] = {}

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
        self._health = FetchHealth()

    @staticmethod
    def _is_permanent(exc: Exception) -> bool:
        text = f"{type(exc).__name__}: {exc}".lower()
        return any(marker in text for marker in _PERMANENT_MARKERS)

    @staticmethod
    def _is_auth_failure(exc: Exception) -> bool:
        # PERF-004: one canonical classifier for authentication/session
        # failures, shared by the retry decision and the cache-eviction check
        # so the two cannot drift apart. An auth-shaped failure cannot be
        # repaired by retrying with the same (now-expired) token, so it must
        # not consume the round-retry budget.
        text = f"{type(exc).__name__}: {exc}".lower()
        return any(marker in text for marker in _AUTH_FAILURE_MARKERS)

    @staticmethod
    def _reason_is_auth(reason: str) -> bool:
        return any(marker in reason.lower() for marker in _AUTH_FAILURE_MARKERS)

    @staticmethod
    def _credential_key() -> tuple[str, str, str]:
        return (
            os.environ.get("REDDIT_CLIENT_ID", ""),
            os.environ.get("REDDIT_CLIENT_SECRET", ""),
            os.environ.get("REDDIT_USER_AGENT", "saipet-scout/0.1 (read-only)"),
        )

    def _build_client(self):
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

    def _client(self):
        """The read-only Reddit client for the current credentials, reused.

        PERF-005: a fresh praw.Reddit per cycle threw away session and
        connection state every 15 minutes and rebuilt it on the next cycle.
        One client is cached per effective credential tuple; changing a
        credential selects (and builds) exactly one replacement, and an
        unrecoverable authentication failure drops the cached entry so the
        next fetch rebuilds.
        """
        key = self._credential_key()
        client = RedditSource._CLIENT_CACHE.get(key)
        if client is None:
            client = self._build_client()
            RedditSource._CLIENT_CACHE[key] = client
        return client

    def _query(self) -> str:
        return " OR ".join(f'"{s}"' for s in self.symptoms)

    def _search_one(self, reddit, subreddit: str, query: str, limit: int) -> list[Candidate]:
        """One attempt against one subreddit. Raises on failure.

        The whole search is attempted rather than resumed: `search()` returns
        a lazy listing, so a failure partway through has no resumable
        position and a partial page would silently look like a complete one.
        Round scheduling lives in `fetch`.
        """
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

    @property
    def health(self) -> FetchHealth:
        return self._health

    def fetch(self, limit: int = 25) -> list[Candidate]:
        self.last_failures = []
        self._health = FetchHealth()
        try:
            reddit = self._client()
        except Exception as exc:  # noqa: BLE001 -- bad or missing credentials
            # Every target is unreachable, not zero targets attempted: a
            # dead client that reported `attempted=0` would read as healthy.
            failure = f"{type(exc).__name__}: {exc}"
            self.last_failures = [(sub, failure) for sub in self.subreddits]
            self._health = FetchHealth(
                attempted=len(self.subreddits), failed=tuple(self.last_failures)
            )
            return []

        query = self._query()
        out: list[Candidate] = []
        failures: dict[str, str] = {}
        retrying = [sub for sub in self.subreddits]

        for round_index in range(self.retries + 1):
            if not retrying:
                break
            next_round: list[str] = []
            for sub in retrying:
                try:
                    out.extend(self._search_one(reddit, sub, query, limit))
                    failures.pop(sub, None)
                except Exception as exc:  # noqa: BLE001 -- see _search_one
                    failures[sub] = f"{type(exc).__name__}: {exc}"
                    # PERF-004: a known-permanent or auth-shaped failure is
                    # not worth retrying this fetch -- the target is gone or
                    # the token is dead, so another attempt only burns the
                    # round-retry budget and the backoff sleeps.
                    if not self._is_permanent(exc) and not self._is_auth_failure(exc):
                        next_round.append(sub)
            retrying = next_round
            # One sleep per ROUND, only when something is left to retry --
            # never after the final pass.
            if retrying and round_index < self.retries:
                self._sleep(self.backoff_seconds * (2**round_index))

        self.last_failures = [
            (sub, failures[sub]) for sub in self.subreddits if sub in failures
        ]
        # PERF-005: every target failed the same auth-shaped way -- the
        # cached client is poisoned, not the targets. Drop it so the next
        # fetch rebuilds instead of failing identically forever. Uses the
        # same classifier as the retry decision (PERF-004).
        if (
            len(self.last_failures) == len(self.subreddits)
            and self.subreddits
            and all(
                self._reason_is_auth(reason)
                for _sub, reason in self.last_failures
            )
        ):
            RedditSource._CLIENT_CACHE.pop(self._credential_key(), None)
        self._health = FetchHealth(
            attempted=len(self.subreddits), failed=tuple(self.last_failures)
        )
        return out
