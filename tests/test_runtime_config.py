import json

import pytest

from saipet import config
from saipet.policy import is_subreddit_allowed
from saipet.runtime_config import ConfigError, apply_overrides, load_overrides
from saipet.scorer import gate


@pytest.fixture(autouse=True)
def _restore_config():
    """These tests deliberately mutate the shipped defaults in place -- that
    is the mechanism under test -- so every one of them is undone here."""
    saved = (
        list(config.SYMPTOMS),
        set(config.SUBREDDIT_ALLOWLIST),
        dict(config.WEIGHTS),
        config.GATE_IGNORE_BELOW,
        config.GATE_PRIORITIZE_AT,
    )
    yield
    config.SYMPTOMS[:] = saved[0]
    config.SUBREDDIT_ALLOWLIST.clear()
    config.SUBREDDIT_ALLOWLIST.update(saved[1])
    config.WEIGHTS.clear()
    config.WEIGHTS.update(saved[2])
    config.GATE_IGNORE_BELOW = saved[3]
    config.GATE_PRIORITIZE_AT = saved[4]


def _write(tmp_path, data):
    path = tmp_path / "saipet.config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_a_missing_file_is_normal_and_changes_nothing(tmp_path):
    before = list(config.SYMPTOMS)
    assert load_overrides(tmp_path / "nope.json") == {}
    assert apply_overrides({}) == []
    assert config.SYMPTOMS == before


def test_every_documented_key_takes_effect(tmp_path):
    path = _write(
        tmp_path,
        {
            "symptoms": ["stuck agent"],
            "subreddit_allowlist": ["testsub"],
            "weights": {"problem_match": 10},
            "gate_ignore_below": 5,
            "gate_prioritize_at": 9,
        },
    )

    applied = apply_overrides(load_overrides(path))

    assert applied == [
        "gate_ignore_below",
        "gate_prioritize_at",
        "subreddit_allowlist",
        "symptoms",
        "weights",
    ]
    assert config.SYMPTOMS == ["stuck agent"]
    assert config.SUBREDDIT_ALLOWLIST == {"testsub"}
    assert config.WEIGHTS["problem_match"] == 10
    assert (config.GATE_IGNORE_BELOW, config.GATE_PRIORITIZE_AT) == (5, 9)


def test_overrides_reach_modules_that_imported_the_names_by_value(tmp_path):
    """The whole point of overriding in place: policy.py and scorer.py did
    `from saipet.config import ...` at import time and must still see this."""
    assert not is_subreddit_allowed("testsub")

    apply_overrides(load_overrides(_write(tmp_path, {"subreddit_allowlist": ["testsub"]})))
    assert is_subreddit_allowed("testsub")

    apply_overrides({"gate_ignore_below": 10, "gate_prioritize_at": 20})
    assert gate(5) == "ignore"
    assert gate(15) == "review"
    assert gate(25) == "priority"


def test_weights_override_merges_rather_than_replacing():
    apply_overrides({"weights": {"problem_match": 1}})
    assert config.WEIGHTS["problem_match"] == 1
    assert "already_solved" in config.WEIGHTS


def test_an_unknown_key_is_refused_not_ignored():
    with pytest.raises(ConfigError, match="unknown config key 'symptomz'"):
        apply_overrides({"symptomz": ["x"]})


def test_an_unknown_weight_name_is_refused():
    with pytest.raises(ConfigError, match="unknown weight 'vibes'"):
        apply_overrides({"weights": {"vibes": 5}})


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"symptoms": "not a list"}, "symptoms must be a list of strings"),
        ({"symptoms": []}, "must not be empty"),
        ({"subreddit_allowlist": [1]}, "must be a list of strings"),
        ({"weights": {"problem_match": True}}, "must be a number"),
        ({"gate_ignore_below": "high"}, "gate_ignore_below must be a number"),
        ({"gate_ignore_below": 90, "gate_prioritize_at": 10}, "must not exceed"),
    ],
)
def test_bad_values_are_refused(overrides, message):
    with pytest.raises(ConfigError, match=message):
        apply_overrides(overrides)


def test_a_rejected_file_leaves_the_defaults_completely_untouched():
    before = list(config.SYMPTOMS)
    with pytest.raises(ConfigError):
        apply_overrides({"symptoms": ["fine"], "nonsense": 1})
    assert config.SYMPTOMS == before


def test_malformed_json_names_the_file(tmp_path):
    path = tmp_path / "saipet.config.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_overrides(path)


def test_a_non_object_top_level_is_refused(tmp_path):
    path = tmp_path / "saipet.config.json"
    path.write_text('["a"]', encoding="utf-8")
    with pytest.raises(ConfigError, match="top level must be an object"):
        load_overrides(path)
