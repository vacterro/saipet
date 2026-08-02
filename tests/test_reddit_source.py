"""Fixture-recorded Reddit integration test (T-005).

No network, no praw, no credentials: `_client()` is monkeypatched to
return a small fake shaped exactly like the bits of praw.Reddit this
source actually touches (`.subreddit(name).search(query, sort, limit)`
yielding objects with `.id/.title/.selftext/.permalink/.created_utc`).
This is what catches a regression in query construction or field mapping
without ever hitting Reddit's servers.
"""

from saipet.sources.reddit import RedditSource


class _FakePost:
    def __init__(self, id_, title, selftext, permalink, created_utc):
        self.id = id_
        self.title = title
        self.selftext = selftext
        self.permalink = permalink
        self.created_utc = created_utc


class _FakeSubreddit:
    def __init__(self, name, posts, calls):
        self._name = name
        self._posts = posts
        self._calls = calls

    def search(self, query, sort="new", limit=25):
        self._calls.append({"subreddit": self._name, "query": query, "sort": sort, "limit": limit})
        return iter(self._posts)


class _FakeReddit:
    def __init__(self, posts_by_sub, calls):
        self._posts_by_sub = posts_by_sub
        self._calls = calls

    def subreddit(self, name):
        return _FakeSubreddit(name, self._posts_by_sub.get(name, []), self._calls)


def test_query_or_joins_every_symptom_and_maps_candidate_fields():
    calls = []
    posts = {
        "testsub": [
            _FakePost("abc123", "Lost context after handoff", "body text", "/r/testsub/abc123", 1770000000.0)
        ]
    }
    source = RedditSource(subreddits=["testsub"], symptoms=["continue", "handoff", "checkpoint"])
    source._client = lambda: _FakeReddit(posts, calls)  # skip real OAuth entirely

    results = source.fetch(limit=10)

    assert len(calls) == 1
    assert calls[0]["subreddit"] == "testsub"
    assert calls[0]["limit"] == 10
    for symptom in ["continue", "handoff", "checkpoint"]:
        assert f'"{symptom}"' in calls[0]["query"]
    assert " OR " in calls[0]["query"]

    assert len(results) == 1
    c = results[0]
    assert c.source == "reddit"
    assert c.id == "abc123"
    assert c.subreddit == "testsub"
    assert c.body == "body text"
    assert c.permalink == "https://reddit.com/r/testsub/abc123"
    assert c.created_utc == 1770000000.0


def test_missing_selftext_defaults_to_empty_body():
    calls = []
    post = _FakePost("xyz", "title only", "", "/r/x/xyz", 0.0)
    del post.selftext  # simulate a link post: praw submissions don't all carry selftext
    source = RedditSource(subreddits=["x"], symptoms=["state"])
    source._client = lambda: _FakeReddit({"x": [post]}, calls)

    results = source.fetch()
    assert results[0].body == ""


def test_multiple_subreddits_all_get_queried():
    calls = []
    source = RedditSource(subreddits=["a", "b", "c"], symptoms=["memory"])
    source._client = lambda: _FakeReddit({}, calls)

    source.fetch()
    assert [c["subreddit"] for c in calls] == ["a", "b", "c"]
