import os

from saipet.sources.base import Candidate, Source


class RedditSource(Source):
    """Read-only fetch via a user-owned Reddit script app.

    Needs REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET / REDDIT_USER_AGENT in the
    environment -- register the app yourself at reddit.com/prefs/apps
    (script type). `read_only=True` means no Reddit username/password is
    ever supplied: this can search and read, and nothing in this class (or
    anywhere else in saipet/) calls .submit/.reply/.comment or any other
    write endpoint. See .saipen/KNOWLEDGE/reddit-access.md.
    """

    name = "reddit"

    def __init__(self, subreddits: list[str], symptoms: list[str]):
        self.subreddits = subreddits
        self.symptoms = symptoms

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

    def fetch(self, limit: int = 25) -> list[Candidate]:
        reddit = self._client()
        out: list[Candidate] = []
        query = " OR ".join(f'"{s}"' for s in self.symptoms)
        for sub in self.subreddits:
            for post in reddit.subreddit(sub).search(query, sort="new", limit=limit):
                out.append(
                    Candidate(
                        source=self.name,
                        id=post.id,
                        title=post.title,
                        body=getattr(post, "selftext", "") or "",
                        permalink=f"https://reddit.com{post.permalink}",
                        subreddit=sub,
                        created_utc=post.created_utc,
                    )
                )
        return out
