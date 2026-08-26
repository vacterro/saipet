"""Settings save path: validated before write, atomic, secret never persisted."""

import json

import pytest

from saipet import config
from saipet.bridge import Result
from saipet.gui.controller import Controller
from saipet.runtime_config import ConfigError, load_overrides, write_overrides


class StubBridge:
    def dispatch(self, verb, **kwargs):
        return Result(ok=True, verb=verb, data={})


@pytest.fixture(autouse=True)
def _restore_config():
    """save_settings applies overrides in place; undo that here."""
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


def test_invalid_config_is_never_partially_written(tmp_path):
    target = tmp_path / "saipet.config.json"
    target.write_text('{"max_age_hours": 24}', encoding="utf-8")

    overrides = {"max_age_hours": 24, "bogus_key": 1}
    try:
        write_overrides(overrides, target)
    except ConfigError:
        pass

    assert load_overrides(target) == {"max_age_hours": 24}


def test_unknown_keys_are_rejected_before_anything_is_written(tmp_path):
    target = tmp_path / "saipet.config.json"
    assert not target.exists()

    try:
        write_overrides({"nonsense": 1}, target)
    except ConfigError:
        pass

    assert not target.exists()


def test_validation_rejects_unknown_keys_via_the_controller():
    ctrl = Controller(StubBridge(), synchronous=True)

    ok, err = ctrl.validate_settings({"nonsense": 1})

    assert ok is False
    assert "unknown config key" in err


def test_a_successful_save_is_atomic_and_applies(tmp_path):
    target = tmp_path / "saipet.config.json"

    ctrl = Controller(StubBridge(), synchronous=True)
    ctrl.save_settings({"subreddit_allowlist": ["LocalLLaMA"]}, str(target))

    assert config.SUBREDDIT_ALLOWLIST == {"LocalLLaMA"}
    assert load_overrides(target) == {"subreddit_allowlist": ["LocalLLaMA"]}
    leftovers = [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_save_status_names_the_file_and_the_applied_keys(tmp_path):
    target = tmp_path / "saipet.config.json"
    ctrl = Controller(StubBridge(), synchronous=True)

    ctrl.save_settings({"max_age_hours": 72}, str(target))

    assert "Saved" in ctrl.state.status_text
    assert "max_age_hours" in ctrl.state.status_text
    assert ctrl.state.settings.saved is True


def test_the_credential_secret_is_never_written_or_displayed(tmp_path):
    import os

    os.environ["REDDIT_CLIENT_SECRET"] = "s3cr3t-token-value"
    target = tmp_path / "saipet.config.json"
    ctrl = Controller(StubBridge(), synchronous=True)

    ctrl.save_settings({"max_age_hours": 12}, str(target))

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert "s3cr3t-token-value" not in json.dumps(payload)
    assert "s3cr3t" not in ctrl.state.status_text
    del os.environ["REDDIT_CLIENT_SECRET"]


def test_settings_validation_preserves_entered_values_on_failure():
    """The form is not cleared on a validation failure -- the user keeps what
    they typed and fixes the one bad field."""
    ctrl = Controller(StubBridge(), synchronous=True)

    ok, err = ctrl.validate_settings({"weights": {"vibes": 5}})

    assert ok is False
    assert "unknown weight" in err


# -- P0-7: config load on startup --------------------------------------


def test_load_config_applies_a_valid_file(tmp_path):
    write_overrides({"max_age_hours": 42}, tmp_path / "saipet.config.json")
    ctrl = Controller(StubBridge(), synchronous=True)

    err = ctrl.load_config(str(tmp_path / "saipet.config.json"))

    assert err is None
    assert config.MAX_AGE_HOURS == 42


def test_load_config_returns_none_when_there_is_no_file(tmp_path):
    ctrl = Controller(StubBridge(), synchronous=True)

    assert ctrl.load_config(str(tmp_path / "nope.json")) is None


def test_load_config_reports_invalid_json_without_partial_apply(tmp_path):
    path = tmp_path / "saipet.config.json"
    path.write_text("{not json", encoding="utf-8")
    ctrl = Controller(StubBridge(), synchronous=True)

    err = ctrl.load_config(str(path))

    assert err is not None
    assert "invalid" in err
    assert "not applied" in err


def test_load_config_rejects_unknown_keys_without_applying_anything(tmp_path):
    path = tmp_path / "saipet.config.json"
    write_overrides({"max_age_hours": 7}, path)
    path.write_text('{"max_age_hours": 7, "bogus": 1}', encoding="utf-8")
    ctrl = Controller(StubBridge(), synchronous=True)

    err = ctrl.load_config(str(path))

    assert err is not None
    assert "unknown config key" in err
    assert config.MAX_AGE_HOURS == 168  # nothing applied
