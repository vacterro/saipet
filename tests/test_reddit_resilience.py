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


# PERF-004: round-based retry scheduling ----------------------------------------

def test_ten_transient_failures_cost_two_shared_sleeps_not_twenty(monkeypatch):
    """PERF-004: retries used to exhaust one target before advancing -- 10
    failing targets slept 2s+4s EACH (120s total). Rounds sleep once per
    retry pass: same 30 attempts, two sleeps totalling the configured 6s."""
    attempts = {"n": 0}
    sleeps: list[float] = []

    class _FakeReddit:
        def subreddit(self, name):
            attempts["n"] += 1

            class _Sub:
                def search(self, *_a, **_k):
                    raise RuntimeError("rate limited")

                def __iter__(self):
                    return iter([])

            return _Sub()

    source = RedditSource(
        [f"sub{i}" for i in range(10)],
        ["lost context"],
        retries=2,
        backoff_seconds=2.0,
        sleep_fn=sleeps.append,
    )
    monkeypatch.setattr(source, "_client", lambda: _FakeReddit())

    out = source.fetch(limit=5)

    assert out == []
    assert attempts["n"] == 30  # 10 targets x 3 passes, unchanged
    assert len(sleeps) == 2  # two ROUND sleeps, not 2 per target
    assert sleeps == [2.0, 4.0]
    assert len(source.last_failures) == 10


def test_healthy_targets_are_attempted_before_the_first_backoff(monkeypatch):
    NOW = 1_800_000_000.0
    order: list[str] = []

    class _FakeReddit:
        def __init__(self, subs):
            self._subs = subs

        def subreddit(self, name):
            order.append(name)

            class _Sub:
                def __init__(self, posts):
                    self._posts = posts

                def search(self, *a, **k):
                    if not self._posts:
                        raise RuntimeError("rate limited")
                    return self._posts

            return _Sub(self._posts.get(name, []))

    good = [
        type("P", (), {"id": "g1", "title": "t", "selftext": "", "permalink": "/x", "created_utc": NOW})()
    ]
    fake = _FakeReddit({"good1": good})
    fake._posts = {"good1": good}
    source = RedditSource(
        ["dead", "good1", "dead2"],
        ["lost context"],
        retries=2,
        backoff_seconds=2.0,
        sleep_fn=lambda _s: order.append("SLEEP"),
    )
    monkeypatch.setattr(source, "_client", lambda: fake)

    out = source.fetch(limit=5)

    # Every first-pass attempt happens before any backoff.
    first_sleep = order.index("SLEEP")
    for sub in ("dead", "good1", "dead2"):
        assert sub in order[:first_sleep]
    assert [c.id for c in out] == ["g1"]


def test_a_permanent_error_gives_up_after_one_attempt(monkeypatch):
    calls = {"n": 0}

    class _FakeReddit:
        def subreddit(self, name):
            calls["n"] += 1

            class _Sub:
                def search(self, *a, **k):
                    raise RuntimeError("404 Not Found")

            return _Sub()

    source = RedditSource(
        ["gone"],
        ["lost context"],
        retries=2,
        backoff_seconds=2.0,
        sleep_fn=lambda _s: (_ for _ in ()).throw(AssertionError("must not sleep")),
    )
    monkeypatch.setattr(source, "_client", lambda: _FakeReddit())

    out = source.fetch(limit=5)

    assert out == []
    assert calls["n"] == 1  # permanent: no second round
    assert len(source.last_failures) == 1


# PERF-005: one client per credential tuple across cycles -----------------------


def _post(pid):
    return type(
        "P",
        (),
        {"id": pid, "title": "t", "selftext": "", "permalink": f"/{pid}", "created_utc": 0},
    )()


def test_one_client_is_built_for_many_cycles(monkeypatch):
    """PERF-005: every fetch built a fresh praw.Reddit -- three cycles, three
    constructions, session state discarded each time. The cache holds one
    client per effective credential tuple."""
    constructions = {"n": 0}
    source = RedditSource(["testsub"], ["lost context"])

    def _fake_build():
        constructions["n"] += 1

        class _Fake:
            def subreddit(self, name):
                class _Sub:
                    def search(self, *a, **k):
                        return [_post("x")]

                return _Sub()

        return _Fake()

    monkeypatch.setattr(source, "_build_client", _fake_build)
    monkeypatch.setenv("REDDIT_CLIENT_ID", "id")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")
    RedditSource._CLIENT_CACHE.pop(RedditSource._credential_key(), None)

    for _ in range(100):
        assert len(source.fetch(limit=5)) == 1

    assert constructions["n"] == 1


def test_a_credential_change_builds_exactly_one_replacement(monkeypatch):
    constructions = {"n": 0}
    source = RedditSource(["testsub"], ["lost context"])

    def _fake_build():
        constructions["n"] += 1
        return object()

    monkeypatch.setattr(source, "_build_client", _fake_build)
    RedditSource._CLIENT_CACHE.clear()
    monkeypatch.setenv("REDDIT_CLIENT_ID", "id-a")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")
    source.fetch(limit=5)
    monkeypatch.setenv("REDDIT_CLIENT_ID", "id-b")
    source.fetch(limit=5)

    assert constructions["n"] == 2
    # Each credential tuple has its own entry; returning to id-a is a cache
    # hit (no third construction).
    monkeypatch.setenv("REDDIT_CLIENT_ID", "id-a")
    source.fetch(limit=5)
    assert constructions["n"] == 2
    RedditSource._CLIENT_CACHE.clear()


def test_an_auth_failure_drops_the_cached_client(monkeypatch):
    """PERF-004 + PERF-005 guardrail: an unrecoverable authentication state
    must not be retried within the fetch (zero sleeps, one attempt per target)
    and must not be served forever from the cache."""
    constructions = {"n": 0}
    attempts = {"n": 0}
    sleeps = []

    class _AuthBoom:
        def subreddit(self, name):
            class _Sub:
                def search(self, *a, **k):
                    attempts["n"] += 1
                    raise RuntimeError("401 Unauthorized")

            return _Sub()

    source = RedditSource(["testsub"], ["lost context"])
    RedditSource._CLIENT_CACHE.clear()

    def _build():
        constructions["n"] += 1
        return _AuthBoom()

    monkeypatch.setattr(source, "_build_client", _build)
    monkeypatch.setattr(source, "_sleep", lambda s: sleeps.append(s))

    out = source.fetch(limit=5)
    assert out == []
    assert len(source.last_failures) == 1
    # PERF-004: a 401 is auth-shaped and must NOT be retried this fetch.
    assert attempts["n"] == 1  # exactly one attempt per target
    assert sleeps == []  # no wasted backoff sleeps

    # The cached client (poisoned) is dropped, so a second fetch rebuilds it.
    out = source.fetch(limit=5)
    assert out == []
    assert constructions["n"] == 2  # rebuilt after the auth-shaped total failure
    RedditSource._CLIENT_CACHE.clear()
