import pytest

from saipet.cli import DEFAULT_LIMIT, build_source, main, parse_args
from saipet.sources.base import FixtureSource
from saipet.sources.reddit import RedditSource


@pytest.fixture
def _no_creds(monkeypatch):
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)


@pytest.fixture
def _creds(monkeypatch):
    monkeypatch.setenv("REDDIT_CLIENT_ID", "id")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")


def test_build_source_is_live_reddit_when_credentials_are_present(_creds):
    source = build_source(["testsub"], ["lost context"])
    assert isinstance(source, RedditSource)
    assert source.subreddits == ["testsub"]
    assert source.symptoms == ["lost context"]


def test_build_source_falls_back_to_the_empty_fixture_without_credentials(_no_creds):
    source = build_source(["testsub"], ["lost context"])
    assert isinstance(source, FixtureSource)
    assert source.fetch() == []


def test_build_source_needs_both_halves_of_the_credential_pair(monkeypatch):
    monkeypatch.setenv("REDDIT_CLIENT_ID", "id")
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    assert isinstance(build_source(["testsub"], []), FixtureSource)


def test_parse_args_defaults():
    args = parse_args([])
    assert args.subreddit is None
    assert args.limit == DEFAULT_LIMIT


def test_subreddit_flag_is_repeatable_and_limit_is_an_int():
    args = parse_args(["--subreddit", "a", "--subreddit", "b", "--limit", "5"])
    assert args.subreddit == ["a", "b"]
    assert args.limit == 5


def test_main_warns_when_the_allowlist_is_empty(monkeypatch, tmp_path, _no_creds):
    monkeypatch.setattr("saipet.cli.SUBREDDIT_ALLOWLIST", set())
    monkeypatch.chdir(tmp_path)
    lines: list[str] = []

    main([], print_fn=lines.append)

    assert any("allowlist is empty" in line for line in lines)
    assert any("empty fixture" in line for line in lines)


def test_main_does_not_warn_once_the_allowlist_has_entries(monkeypatch, tmp_path, _no_creds):
    monkeypatch.setattr("saipet.cli.SUBREDDIT_ALLOWLIST", {"testsub"})
    monkeypatch.chdir(tmp_path)
    lines: list[str] = []

    main([], print_fn=lines.append)

    assert not any("allowlist is empty" in line for line in lines)
    assert any("subreddits: testsub" in line for line in lines)
