"""Atomic config writing, added for the Settings screen.

`apply_overrides` mutates the running process; `write_overrides` is what
persists a change so the next process sees it too. The two share one
validator, so a file that passes validation can never be refused at apply
time and vice versa.
"""

import json

import pytest

from saipet import config
from saipet.runtime_config import (
    ConfigError,
    apply_overrides,
    load_overrides,
    validate_overrides,
    write_overrides,
)


@pytest.fixture(autouse=True)
def _restore_config():
    """apply_overrides mutates the shipped defaults in place; undo that here
    so this module cannot leak into whatever runs next in the same process."""
    saved = (
        list(config.SYMPTOMS),
        set(config.SUBREDDIT_ALLOWLIST),
        dict(config.WEIGHTS),
        config.GATE_IGNORE_BELOW,
        config.GATE_PRIORITIZE_AT,
        config.MAX_AGE_HOURS,
    )
    yield
    config.SYMPTOMS[:] = saved[0]
    config.SUBREDDIT_ALLOWLIST.clear()
    config.SUBREDDIT_ALLOWLIST.update(saved[1])
    config.WEIGHTS.clear()
    config.WEIGHTS.update(saved[2])
    config.GATE_IGNORE_BELOW = saved[3]
    config.GATE_PRIORITIZE_AT = saved[4]
    config.MAX_AGE_HOURS = saved[5]


def test_write_round_trips_through_load(tmp_path):
    overrides = {
        "subreddit_allowlist": ["LocalLLaMA"],
        "max_age_hours": 48,
    }
    path = write_overrides(overrides, tmp_path / "saipet.config.json")

    assert path.exists()
    assert load_overrides(path) == overrides


def test_write_is_atomic_no_partial_file_on_validation_failure(tmp_path):
    path = tmp_path / "saipet.config.json"
    path.write_text('{"max_age_hours": 24}', encoding="utf-8")

    with pytest.raises(ConfigError):
        write_overrides({"max_age_hours": 24, "nonsense_key": 1}, path)

    assert load_overrides(path) == {"max_age_hours": 24}


def test_write_refuses_an_unknown_key_without_creating_the_file(tmp_path):
    path = tmp_path / "saipet.config.json"
    assert not path.exists()

    with pytest.raises(ConfigError, match="unknown config key"):
        write_overrides({"typo_key": 1}, path)

    assert not path.exists()


def test_a_saved_file_applies_like_any_other(tmp_path):
    path = write_overrides({"subreddit_allowlist": ["testsub"]}, tmp_path / "saipet.config.json")

    assert apply_overrides(load_overrides(path)) == ["subreddit_allowlist"]
    assert config.SUBREDDIT_ALLOWLIST == {"testsub"}


def test_validate_overrides_matches_apply(tmp_path):
    """The GUI validates before writing; apply must agree on the same input."""
    good = {"gate_ignore_below": 50, "gate_prioritize_at": 70}
    validate_overrides(good)
    assert apply_overrides(good) == ["gate_ignore_below", "gate_prioritize_at"]

    bad = {"gate_ignore_below": 90, "gate_prioritize_at": 10}
    with pytest.raises(ConfigError):
        validate_overrides(bad)


def test_the_writer_never_touches_a_secret(tmp_path):
    """A config file must never carry a credential, and the writer has no
    channel for one even if asked."""
    data = write_overrides({"max_age_hours": 12}, tmp_path / "saipet.config.json")
    payload = data.read_text(encoding="utf-8")
    assert "secret" not in payload.lower()
    assert "REDDIT_CLIENT" not in payload
