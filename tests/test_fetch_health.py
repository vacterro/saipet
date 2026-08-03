import pytest

from saipet.bridge import Bridge
from saipet.monitor import run_monitor
from saipet.notify import Notification, Notifier
from saipet.sources.base import DEGRADED, FAILED, HEALTHY, Candidate, FetchHealth, FixtureSource
from saipet.sources.reddit import RedditSource

NOW = 1_800_000_000.0
_RELEVANT_BODY = "Lost context, session state, handoff, resume, checkpoint, deterministic protocol."


class _Collector(Notifier):
    def __init__(self):
        self.sent: list[Notification] = []

    def kinds(self):
        return [n.kind for n in self.sent]

    def send(self, notification):
        self.sent.append(notification)


class _FakeSubreddit:
    def __init__(self, posts=(), error=None):
        self._posts = list(posts)
        self._error = error

    def search(self, query, sort, limit):
        if self._error is not None:
            raise self._error
        return list(self._posts)


class _FakePost:
    def __init__(self, post_id):
        self.id = post_id
        self.title = "agent keeps losing context"
        self.selftext = _RELEVANT_BODY
        self.permalink = f"/r/testsub/{post_id}"
        self.created_utc = NOW - 3600


def _reddit(subreddits, fakes):
    source = RedditSource(subreddits, ["lost context"], retries=0, sleep_fn=lambda _s: None)
    source._client = lambda: type("R", (), {"subreddit": staticmethod(lambda n: fakes[n])})()
    return source


# -- the verdict itself ------------------------------------------------


@pytest.mark.parametrize(
    "attempted, failed, expected",
    [
        (3, (), HEALTHY),
        (3, (("a", "403"),), DEGRADED),
        (3, (("a", "403"), ("b", "403")), DEGRADED),
        (3, (("a", "403"), ("b", "403"), ("c", "403")), FAILED),
        (1, (("a", "403"),), FAILED),
        (0, (), HEALTHY),  # nothing asked for is the caller's problem, not the source's
    ],
)
def test_health_states(attempted, failed, expected):
    assert FetchHealth(attempted=attempted, failed=failed).state == expected


def test_succeeded_counts_what_actually_answered():
    health = FetchHealth(attempted=3, failed=(("a", "403"),))
    assert health.succeeded == 2


# -- the source reports it ---------------------------------------------


def test_a_total_outage_is_failed_not_empty():
    """The bug this ticket exists for: every subreddit 403s, an empty list
    comes back, and that used to be indistinguishable from a quiet night."""
    source = _reddit(
        ["dead1", "dead2"],
        {
            "dead1": _FakeSubreddit(error=RuntimeError("403 Forbidden")),
            "dead2": _FakeSubreddit(error=RuntimeError("403 Forbidden")),
        },
    )

    assert source.fetch() == []
    assert source.health.state == FAILED
    assert source.health.attempted == 2
    assert source.health.succeeded == 0


def test_one_dead_subreddit_among_healthy_ones_is_degraded():
    source = _reddit(
        ["dead", "alive"],
        {
            "dead": _FakeSubreddit(error=RuntimeError("403 Forbidden")),
            "alive": _FakeSubreddit(posts=[_FakePost("ok1")]),
        },
    )

    assert [c.id for c in source.fetch()] == ["ok1"]
    assert source.health.state == DEGRADED


def test_all_reachable_is_healthy():
    source = _reddit(["alive"], {"alive": _FakeSubreddit(posts=[_FakePost("ok1")])})
    source.fetch()
    assert source.health.state == HEALTHY


def test_a_dead_client_is_every_target_failing_not_zero_targets():
    """Expired keys raise in `_client()`, before any subreddit is touched.
    Reported as `attempted=0` that would read as perfectly healthy."""
    source = RedditSource(["a", "b"], ["x"], retries=0)

    def _broken():
        raise OSError("401 Unauthorized")

    source._client = _broken

    assert source.fetch() == []
    assert source.health.state == FAILED
    assert source.health.attempted == 2


def test_the_fixture_source_cannot_fail():
    assert FixtureSource([]).health.state == HEALTHY


# -- the bridge carries it ---------------------------------------------


@pytest.fixture
def make_bridge(tmp_path, monkeypatch):
    allowed = {"testsub", "alive", "dead", "dead1", "dead2", "flaky"}
    monkeypatch.setattr("saipet.policy.SUBREDDIT_ALLOWLIST", allowed)
    monkeypatch.setattr("saipet.config.SUBREDDIT_ALLOWLIST", allowed)
    monkeypatch.setattr("saipet.config.NOTIFY_MIN_SCORE", 0)

    def _make(source):
        return Bridge(
            report_dir=tmp_path / "runs",
            seen_path=tmp_path / "seen.json",
            source_factory=lambda *_: source,
            now_fn=lambda: NOW,
        )

    return _make


def test_scout_reports_the_health_it_got(make_bridge):
    source = _reddit(["dead"], {"dead": _FakeSubreddit(error=RuntimeError("403 Forbidden"))})

    data = make_bridge(source).dispatch("scout", subreddits=["dead"]).data

    assert data["health"] == FAILED
    assert data["fetch"]["attempted"] == 1
    assert data["fetch"]["failed"] == [{"target": "dead", "error": "RuntimeError: 403 Forbidden"}]


# -- the monitor acts on it --------------------------------------------


def test_a_total_outage_is_a_failed_cycle_with_backoff(make_bridge):
    source = _reddit(["dead"], {"dead": _FakeSubreddit(error=RuntimeError("403 Forbidden"))})
    sink = _Collector()
    sleeps: list[float] = []

    state = run_monitor(
        make_bridge(source),
        notifier=sink,
        cycles=2,
        interval_seconds=100,
        heartbeat_every=1,
        scout_args={"subreddits": ["dead"]},
        sleep_fn=sleeps.append,
        now_fn=lambda: NOW,
    )

    assert state.errors == 2
    assert state.health == FAILED
    assert "403 Forbidden" in state.last_error
    assert sink.kinds() == ["error", "error"]
    assert sleeps == [200]  # backed off, not the plain 100


def test_a_degraded_cycle_warns_but_does_not_slow_the_monitor(make_bridge):
    source = _reddit(
        ["dead", "alive"],
        {
            "dead": _FakeSubreddit(error=RuntimeError("403 Forbidden")),
            "alive": _FakeSubreddit(posts=[_FakePost("ok1")]),
        },
    )
    sink = _Collector()
    sleeps: list[float] = []

    state = run_monitor(
        make_bridge(source),
        notifier=sink,
        cycles=2,
        interval_seconds=100,
        heartbeat_every=1,
        scout_args={"subreddits": ["dead", "alive"]},
        sleep_fn=sleeps.append,
        now_fn=lambda: NOW,
    )

    assert state.errors == 0
    assert state.degraded_cycles == 2
    assert state.health == DEGRADED
    assert sink.kinds() == ["finding", "warning", "heartbeat", "warning", "heartbeat"]
    assert sleeps == [100]  # no backoff: the live subreddit is still worth polling


def test_a_healthy_cycle_says_nothing_extra(make_bridge):
    source = _reddit(["alive"], {"alive": _FakeSubreddit(posts=[_FakePost("ok1")])})
    sink = _Collector()

    state = run_monitor(
        make_bridge(source),
        notifier=sink,
        cycles=1,
        heartbeat_every=1,
        scout_args={"subreddits": ["alive"]},
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )

    assert state.health == HEALTHY
    assert state.degraded_cycles == 0
    assert sink.kinds() == ["finding", "heartbeat"]


def test_recovery_clears_the_degradation(make_bridge):
    """A subreddit that comes back must not leave the monitor permanently
    marked degraded."""
    states = [
        _FakeSubreddit(error=RuntimeError("403 Forbidden")),
        _FakeSubreddit(posts=[_FakePost("ok2")]),
    ]
    fakes = {"flaky": states[0], "alive": _FakeSubreddit(posts=[_FakePost("ok1")])}
    source = _reddit(["flaky", "alive"], fakes)
    bridge = make_bridge(source)

    run_monitor(
        bridge,
        cycles=1,
        scout_args={"subreddits": ["flaky", "alive"]},
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )
    fakes["flaky"] = states[1]
    state = run_monitor(
        bridge,
        cycles=1,
        scout_args={"subreddits": ["flaky", "alive"]},
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )

    assert state.health == HEALTHY
    assert state.last_degradation == ""


def test_a_fixture_only_run_stays_healthy(make_bridge):
    """The empty-allowlist case must not masquerade as an outage."""
    state = run_monitor(
        make_bridge(FixtureSource([])),
        cycles=1,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )

    assert state.health == HEALTHY
    assert state.errors == 0


def test_a_candidate_still_flows_through_a_degraded_cycle(make_bridge):
    source = _reddit(
        ["dead", "alive"],
        {
            "dead": _FakeSubreddit(error=RuntimeError("403")),
            "alive": _FakeSubreddit(posts=[_FakePost("ok1")]),
        },
    )
    sink = _Collector()

    run_monitor(
        make_bridge(source),
        notifier=sink,
        cycles=1,
        scout_args={"subreddits": ["dead", "alive"]},
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )

    assert [n.data["id"] for n in sink.sent if n.kind == "finding"] == ["ok1"]


def test_candidate_is_still_a_plain_dataclass():
    """Health rides on the source; the Candidate contract is untouched."""
    candidate = Candidate(source="fixture", id="x", title="t", body="b", permalink="p")
    assert candidate.subreddit == ""
