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
import os
import tempfile
from pathlib import Path

from saipet import config

DEFAULT_CONFIG_PATH = "saipet.config.json"

# CORE-005: an immutable copy of the shipped weights, captured at import time
# BEFORE any runtime override mutates `config.WEIGHTS`. Weight-sign validation
# must anchor to this canonical contract, never to the live (mutable) container:
# otherwise a GUI that saves a penalty as 0 could later convert it into a
# reward, because validation would then begin from the in-memory 0.
_SHIPPED_WEIGHTS = {name: float(value) for name, value in config.WEIGHTS.items()}

# key in the JSON file -> the config.py name it overrides.
_OVERRIDABLE = {
    "symptoms": "SYMPTOMS",
    "subreddit_allowlist": "SUBREDDIT_ALLOWLIST",
    "weights": "WEIGHTS",
    "gate_ignore_below": "GATE_IGNORE_BELOW",
    "gate_prioritize_at": "GATE_PRIORITIZE_AT",
    "max_age_hours": "MAX_AGE_HOURS",
    "notify_min_score": "NOTIFY_MIN_SCORE",
    "report_retention_days": "REPORT_RETENTION_DAYS",
    "seen_ttl_days": "SEEN_TTL_DAYS",
}


class ConfigError(ValueError):
    """A config file that exists but says something unusable.

    Loud on purpose: a mistyped key silently ignored turns the file into a
    lie the user keeps trusting.
    """


def load_overrides(path: str | Path = DEFAULT_CONFIG_PATH) -> dict:
    """Read the override file. A missing file is normal and yields `{}`.

    W2-005: a file that IS present but cannot be decoded (invalid UTF-8) or
    read (permission/I/O error) is a configuration error, not a "use
    defaults" signal. Every such failure is normalised to `ConfigError` so
    CLI/monitor/terminal all take their advertised configuration-error exit
    path instead of propagating a raw implementation traceback.
    """
    config_path = Path(path)
    if not config_path.exists():
        return {}
    try:
        text = config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{config_path}: cannot read config -- {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{config_path}: not valid JSON -- {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{config_path}: top level must be an object, got {type(data).__name__}")
    return data


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigError(message)


def validate_overrides(overrides: dict) -> dict:
    """Validate `overrides` and return the canonicalised form.

    `apply_overrides` runs this first, so a file with one bad entry leaves
    the defaults completely untouched. Split out so a GUI can validate the
    form it is about to write without mutating the running config.

    Scoring semantics are enforced at the boundary (W2-009): symptoms are
    trimmed, must be non-empty and lowercase (candidate text is matched
    lowercased), and must be unique after normalisation -- one repeated
    symptom must never count as several distinct matches. Numeric weights
    must be finite.
    """
    import math

    canonical = dict(overrides)

    for key in canonical:
        _require(
            key in _OVERRIDABLE,
            f"unknown config key {key!r} -- known keys: {', '.join(sorted(_OVERRIDABLE))}",
        )

    if "symptoms" in canonical:
        value = canonical["symptoms"]
        _require(
            isinstance(value, list) and all(isinstance(s, str) for s in value),
            "symptoms must be a list of strings",
        )
        _require(bool(value), "symptoms must not be empty -- an empty vocabulary matches nothing")
        normalized = [s.strip() for s in value]
        _require(
            all(normalized),
            "symptoms must be non-empty strings (no whitespace-only entries)",
        )
        _require(
            all(s == s.lower() for s in normalized),
            "symptoms must be lowercase -- candidate text is matched lowercased",
        )
        _require(
            len(set(normalized)) == len(normalized),
            "symptoms must be unique after normalisation",
        )
        canonical["symptoms"] = normalized

    if "subreddit_allowlist" in canonical:
        value = canonical["subreddit_allowlist"]
        _require(
            isinstance(value, list) and all(isinstance(s, str) for s in value),
            "subreddit_allowlist must be a list of strings",
        )
        canonical["subreddit_allowlist"] = [s.strip() for s in value if s.strip()]

    if "weights" in canonical:
        value = canonical["weights"]
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
            _require(
                math.isfinite(float(weight)),
                f"weight {name!r} must be finite",
            )
            # CORE-005: the shipped sign encodes the scorer's contract --
            # penalties subtract, evidence boosts add. A sign-flipped
            # override turns "already solved" into a reward. Anchor the
            # check to the immutable shipped baseline, not the mutable
            # live `config.WEIGHTS`, so a 0-saved penalty cannot later
            # become a reward.
            default = _SHIPPED_WEIGHTS[name]
            flipped = (default < 0 and float(weight) > 0) or (
                default > 0 and float(weight) < 0
            )
            _require(
                not flipped,
                f"weight {name!r} must keep its "
                f"{'penalty' if default < 0 else 'boost'} sign "
                f"(shipped {config.WEIGHTS[name]}, got {weight})",
            )

    for key in ("gate_ignore_below", "gate_prioritize_at", "max_age_hours", "notify_min_score",
                "report_retention_days", "seen_ttl_days"):
        if key in canonical:
            _require(
                isinstance(canonical[key], (int, float)) and not isinstance(canonical[key], bool),
                f"{key} must be a number",
            )
            _require(
                math.isfinite(float(canonical[key])),
                f"{key} must be finite",
            )

    # CORE-011: both gates decide bands over the scorer's fixed 0..100
    # output domain; thresholds outside it produce valid-looking but
    # contradictory classification (score 0 as priority, or unreachable
    # priority bands).
    for key in ("gate_ignore_below", "gate_prioritize_at"):
        if key in canonical:
            _require(
                0 <= float(canonical[key]) <= 100,
                f"{key} must be within the scorer's 0..100 score domain",
            )

    if "report_retention_days" in canonical:
        _require(canonical["report_retention_days"] > 0, "report_retention_days must be positive")

    if "seen_ttl_days" in canonical:
        _require(canonical["seen_ttl_days"] > 0, "seen_ttl_days must be positive")

    if "max_age_hours" in canonical:
        _require(canonical["max_age_hours"] >= 0, "max_age_hours must not be negative")

    if "notify_min_score" in canonical:
        _require(
            0 <= canonical["notify_min_score"] <= 100,
            "notify_min_score must be between 0 and 100",
        )

    ignore_below = canonical.get("gate_ignore_below", config.GATE_IGNORE_BELOW)
    prioritize_at = canonical.get("gate_prioritize_at", config.GATE_PRIORITIZE_AT)
    _require(
        ignore_below <= prioritize_at,
        f"gate_ignore_below ({ignore_below}) must not exceed gate_prioritize_at ({prioritize_at})",
    )

    return canonical


def apply_overrides(overrides: dict) -> list[str]:
    """Apply `overrides` to config.py's tunables. Returns the keys applied.

    Every key is validated before anything is written, so a file with one
    bad entry leaves the defaults completely untouched instead of a
    half-applied mixture. The canonicalised form is what gets applied.
    """
    canonical = validate_overrides(overrides)

    # Validation is complete: from here nothing can fail partway through.
    if "symptoms" in canonical:
        config.SYMPTOMS[:] = canonical["symptoms"]
    if "subreddit_allowlist" in canonical:
        config.SUBREDDIT_ALLOWLIST.clear()
        config.SUBREDDIT_ALLOWLIST.update(canonical["subreddit_allowlist"])
    if "weights" in canonical:
        config.WEIGHTS.update(canonical["weights"])
    if "gate_ignore_below" in canonical:
        config.GATE_IGNORE_BELOW = canonical["gate_ignore_below"]
    if "gate_prioritize_at" in canonical:
        config.GATE_PRIORITIZE_AT = canonical["gate_prioritize_at"]
    if "max_age_hours" in canonical:
        config.MAX_AGE_HOURS = canonical["max_age_hours"]
    if "notify_min_score" in canonical:
        config.NOTIFY_MIN_SCORE = canonical["notify_min_score"]
    if "report_retention_days" in canonical:
        config.REPORT_RETENTION_DAYS = canonical["report_retention_days"]
    if "seen_ttl_days" in canonical:
        config.SEEN_TTL_DAYS = canonical["seen_ttl_days"]

    return sorted(canonical)


def write_overrides(overrides: dict, path: str | Path = DEFAULT_CONFIG_PATH) -> Path:
    """Validate, canonicalise, then atomically persist `overrides` as a
    config file.

    A settings editor never half-writes: the JSON goes to a temp file in the
    same directory, is flushed and fsynced, then `os.replace`d over the
    target. A crash mid-write leaves either the old file or the new one,
    never a truncated mix. Unknown keys are refused before anything is
    written, exactly as `apply_overrides` refuses them before applying.
    The canonicalised form is what gets persisted.
    """
    canonical = validate_overrides(overrides)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(canonical, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    return target
