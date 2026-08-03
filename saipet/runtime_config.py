"""Optional runtime overrides for config.py's tunables.

Retuning the scout -- which subreddits are allowed, which symptoms it hunts,
how the signals are weighted, where the gate bands sit -- is a per-user,
per-run decision, and editing an installed package to make it is not one.
A JSON file next to wherever you run from does it instead; no file at all
means config.py's shipped defaults, unchanged.

Overrides are applied *in place* to the existing container objects rather
than rebound onto the module: every consumer does `from saipet.config
import NAME`, so a rebind would only ever be visible to modules imported
afterwards -- a silent half-applied config, which is worse than none.
"""

import json
from pathlib import Path

from saipet import config

DEFAULT_CONFIG_PATH = "saipet.config.json"

# key in the JSON file -> the config.py name it overrides.
_OVERRIDABLE = {
    "symptoms": "SYMPTOMS",
    "subreddit_allowlist": "SUBREDDIT_ALLOWLIST",
    "weights": "WEIGHTS",
    "gate_ignore_below": "GATE_IGNORE_BELOW",
    "gate_prioritize_at": "GATE_PRIORITIZE_AT",
    "max_age_hours": "MAX_AGE_HOURS",
}


class ConfigError(ValueError):
    """A config file that exists but says something unusable.

    Loud on purpose: a mistyped key silently ignored turns the file into a
    lie the user keeps trusting.
    """


def load_overrides(path: str | Path = DEFAULT_CONFIG_PATH) -> dict:
    """Read the override file. A missing file is normal and yields `{}`."""
    config_path = Path(path)
    if not config_path.exists():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{config_path}: not valid JSON -- {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{config_path}: top level must be an object, got {type(data).__name__}")
    return data


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigError(message)


def apply_overrides(overrides: dict) -> list[str]:
    """Apply `overrides` to config.py's tunables. Returns the keys applied.

    Every key is validated before anything is written, so a file with one
    bad entry leaves the defaults completely untouched instead of a
    half-applied mixture.
    """
    for key in overrides:
        _require(
            key in _OVERRIDABLE,
            f"unknown config key {key!r} -- known keys: {', '.join(sorted(_OVERRIDABLE))}",
        )

    if "symptoms" in overrides:
        value = overrides["symptoms"]
        _require(
            isinstance(value, list) and all(isinstance(s, str) for s in value),
            "symptoms must be a list of strings",
        )
        _require(bool(value), "symptoms must not be empty -- an empty vocabulary matches nothing")

    if "subreddit_allowlist" in overrides:
        value = overrides["subreddit_allowlist"]
        _require(
            isinstance(value, list) and all(isinstance(s, str) for s in value),
            "subreddit_allowlist must be a list of strings",
        )

    if "weights" in overrides:
        value = overrides["weights"]
        _require(isinstance(value, dict), "weights must be an object")
        for name, weight in value.items():
            _require(
                name in config.WEIGHTS,
                f"unknown weight {name!r} -- known weights: {', '.join(sorted(config.WEIGHTS))}",
            )
            _require(
                isinstance(weight, (int, float)) and not isinstance(weight, bool),
                f"weight {name!r} must be a number",
            )

    for key in ("gate_ignore_below", "gate_prioritize_at", "max_age_hours"):
        if key in overrides:
            _require(
                isinstance(overrides[key], (int, float)) and not isinstance(overrides[key], bool),
                f"{key} must be a number",
            )

    if "max_age_hours" in overrides:
        _require(overrides["max_age_hours"] >= 0, "max_age_hours must not be negative")

    ignore_below = overrides.get("gate_ignore_below", config.GATE_IGNORE_BELOW)
    prioritize_at = overrides.get("gate_prioritize_at", config.GATE_PRIORITIZE_AT)
    _require(
        ignore_below <= prioritize_at,
        f"gate_ignore_below ({ignore_below}) must not exceed gate_prioritize_at ({prioritize_at})",
    )

    # Validation is complete: from here nothing can fail partway through.
    if "symptoms" in overrides:
        config.SYMPTOMS[:] = overrides["symptoms"]
    if "subreddit_allowlist" in overrides:
        config.SUBREDDIT_ALLOWLIST.clear()
        config.SUBREDDIT_ALLOWLIST.update(overrides["subreddit_allowlist"])
    if "weights" in overrides:
        config.WEIGHTS.update(overrides["weights"])
    if "gate_ignore_below" in overrides:
        config.GATE_IGNORE_BELOW = overrides["gate_ignore_below"]
    if "gate_prioritize_at" in overrides:
        config.GATE_PRIORITIZE_AT = overrides["gate_prioritize_at"]
    if "max_age_hours" in overrides:
        config.MAX_AGE_HOURS = overrides["max_age_hours"]

    return sorted(overrides)
