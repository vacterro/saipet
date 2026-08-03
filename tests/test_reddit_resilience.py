import pytest

from saipet.sources.reddit import RedditSource


class _FakePost:
    def __init__(self, post_id):
        self.id = post_id
        self.title = f"title {post_id}"
        self.selftext = "body"
        self.permalink = f"/r/x/{post_id}"
        self.created_utc = 1_800_000_000.0


class _FakeSubreddit:
    def __init__(self, posts, error=None, fail_times=None):
        self._posts = posts
        self._error = error
        self._remaining_failures = fail_times
        self.search_calls = 0

    def search(self, query, sort, limit):
        self.search_calls += 1
        if self._error is not None and (
            self._remaining_failures is None or self._remaining_failures > 0
        ):
            if self._remaining_failures is not None:
                self._remaining_failures -= 1
            raise self._error
        return list(self._posts)


class _FakeReddit:
    def __init__(self, subreddits):
        self._subreddits = subreddits

    def subreddit(self, name):
        return self._subreddits[name]


def _source(subreddits, **kwargs):
    sleeps: list[float] = []
    source = RedditSource(
        list(subreddits),
        ["lost context"],
        sleep_fn=sleeps.append,
        **kwargs,
    )
    return source, sleeps


def test_a_dead_subreddit_does_not_take_the_healthy_ones_with_it(monkeypatch):
    fakes = {
        "dead": _FakeSubreddit([], error=RuntimeError("403 Forbidden")),
        "alive": _FakeSubreddit([_FakePost("ok1")]),
    }
    source, _ = _source(["dead", "alive"])
    monkeypatch.setattr(source, "_client", lambda: _FakeReddit(fakes))

    candidates = source.fetch()

    assert [c.id for c in candidates] == ["ok1"]
    assert source.last_failures == [("dead", "RuntimeError: 403 Forbidden")]


def test_a_transient_failure_is_retried_and_then_succeeds(monkeypatch):
    fakes = {
        "flaky": _FakeSubreddit(
            [_FakePost("ok1")], error=RuntimeError("429 Too Many Requests"), fail_times=2
        )
    }
    source, sleeps = _source(["flaky"], retries=2, backoff_seconds=2.0)
    monkeypatch.setattr(source, "_client", lambda: _FakeReddit(fakes))

    candidates = source.fetch()

    assert [c.id for c in candidates] == ["ok1"]
    assert source.last_failures == []
    assert fakes["flaky"].search_calls == 3
    assert sleeps == [2.0, 4.0]  # backoff doubles


def test_retries_are_bounded_and_the_last_error_is_what_gets_reported(monkeypatch):
    fakes = {"dead": _FakeSubreddit([], error=RuntimeError("nope"))}
    source, sleeps = _source(["dead"], retries=2, backoff_seconds=1.0)
    monkeypatch.setattr(source, "_client", lambda: _FakeReddit(fakes))

    assert source.fetch() == []
    assert fakes["dead"].search_calls == 3
    assert len(sleeps) == 2
    assert source.last_failures == [("dead", "RuntimeError: nope")]


def test_no_sleeping_at_all_when_nothing_fails(monkeypatch):
    fakes = {"alive": _FakeSubreddit([_FakePost("ok1")])}
    source, sleeps = _source(["alive"])
    monkeypatch.setattr(source, "_client", lambda: _FakeReddit(fakes))

    source.fetch()

    assert sleeps == []


def test_failures_describe_the_last_fetch_not_a_lifetime_tally(monkeypatch):
    fakes = {"flaky": _FakeSubreddit([], error=RuntimeError("nope"))}
    source, _ = _source(["flaky"], retries=0)
    monkeypatch.setattr(source, "_client", lambda: _FakeReddit(fakes))

    source.fetch()
    assert len(source.last_failures) == 1

    fakes["flaky"] = _FakeSubreddit([_FakePost("ok1")])
    source.fetch()
    assert source.last_failures == []


def test_a_broken_subreddit_is_reported_by_the_cli_not_swallowed(monkeypatch, tmp_path):
    from saipet.cli import main

    fakes = {"dead": _FakeSubreddit([], error=RuntimeError("403 Forbidden"))}
    source, _ = _source(["dead"], retries=0)
    monkeypatch.setattr(source, "_client", lambda: _FakeReddit(fakes))
    monkeypatch.setattr("saipet.cli.build_source", lambda *_: source)
    monkeypatch.chdir(tmp_path)
    lines: list[str] = []

    main(["--subreddit", "dead"], print_fn=lines.append)

    assert any("WARNING: dead could not be fetched" in line for line in lines)


@pytest.mark.parametrize("retries", [0, 1, 3])
def test_attempt_count_is_retries_plus_one(monkeypatch, retries):
    fakes = {"dead": _FakeSubreddit([], error=RuntimeError("nope"))}
    source, _ = _source(["dead"], retries=retries, backoff_seconds=0)
    monkeypatch.setattr(source, "_client", lambda: _FakeReddit(fakes))

    source.fetch()

    assert fakes["dead"].search_calls == retries + 1
