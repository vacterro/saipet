"""Line-oriented command engine over the bridge, for a local agent to drive.

One line in, one JSON line out. That is the whole protocol, and it is
deliberately boring: an agent (or SAIPEN, or a person with a pipe) writes
`scout subreddit=LocalLLaMA limit=50`, reads back a JSON object, and never
has to parse prose.

**This is a command engine, not a terminal.** The name is the trap: what
the caller wants is a place to send commands from, and the obvious
implementation -- hand the line to a shell -- would be catastrophic here.
This process holds text fetched off the public internet; a Reddit title
that reaches an execution path is a stranger's code running on your
machine. So an unrecognised line is refused and passed nowhere. There is
no fallback, no "if it isn't a verb, try running it". The engine's whole
vocabulary is the bridge's closed verb set plus `help` and `quit`, and
tests/test_no_autopost.py fails the build if any execution primitive ever
appears anywhere in this package.

Argument syntax is `key=value`, quoted with normal shell quoting rules
(`shlex`, which only splits strings -- it runs nothing):

    scout subreddit=LocalLLaMA,AI_Agents limit=50 since_hours=48
    queue
    approve id=abc123 solution="set a checkpoint after each step"
    status
    quit
"""

import json
import shlex

from saipet.bridge.dispatch import VERBS, Bridge
from saipet.runtime_config import (
    DEFAULT_CONFIG_PATH,
    ConfigError,
    apply_overrides,
    load_overrides,
)

META_VERBS = frozenset({"help", "quit"})

# W2-001: the canonical durable review store the default terminal engine uses,
# so a finding survives a process restart (the user-facing terminal is the
# agent control plane, not an ephemeral session).
DEFAULT_REVIEW_PATH = "review-state.json"

# Arguments that are lists of strings when written comma-separated.
_LIST_ARGS = frozenset({"subreddit", "subreddits"})
_ALIASES = {"subreddit": "subreddits"}

# Arguments that are ALWAYS strings, never JSON-coerced. Reddit ids are
# base36 and an all-digit one ("17439") is perfectly ordinary -- coercing
# it to an int makes `approve id=17439` silently match nothing.
_STRING_ARGS = frozenset({"id", "solution"})


class CommandError(ValueError):
    """A line this engine will not act on. Refused, never forwarded."""


def _coerce(value: str):
    """Turn one token's value into a Python value.

    JSON first (so `limit=50`, `mention_saipen=false` and `since_hours=1.5`
    arrive as the right types), plain string otherwise.
    """
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def parse_line(line: str) -> tuple[str, dict]:
    """`"scout limit=50"` -> `("scout", {"limit": 50})`.

    Raises `CommandError` for anything that is not `verb key=value ...`.
    Refusing here is the point: the caller gets an error, not a guess.
    """
    try:
        tokens = shlex.split(line.strip())
    except ValueError as exc:  # unbalanced quotes
        raise CommandError(f"could not parse line: {exc}") from exc
    if not tokens:
        raise CommandError("empty line")

    verb, *rest = tokens
    if verb not in VERBS and verb not in META_VERBS:
        raise CommandError(
            f"unknown command {verb!r} -- this is a command engine, not a shell. "
            f"Known: {', '.join(sorted(VERBS | META_VERBS))}"
        )

    args: dict = {}
    for token in rest:
        if "=" not in token:
            raise CommandError(f"argument {token!r} is not key=value")
        key, _, raw = token.partition("=")
        if not key:
            raise CommandError(f"argument {token!r} has an empty key")
        key = _ALIASES.get(key, key)
        if key in _STRING_ARGS:
            value = raw
        elif key in _LIST_ARGS:
            value = [part for part in raw.split(",") if part]
        else:
            value = _coerce(raw)
        args[key] = value
    return verb, args


def _help() -> dict:
    return {
        "verbs": sorted(VERBS),
        "meta": sorted(META_VERBS),
        "syntax": "verb key=value ...",
        "note": "command engine, not a shell -- unknown lines are refused, never run",
    }


def handle_line(bridge: Bridge, line: str) -> dict:
    """One line -> one response dict. Never raises for bad input."""
    try:
        verb, args = parse_line(line)
    except CommandError as exc:
        return {"ok": False, "verb": None, "error": str(exc)}

    if verb == "help":
        return {"ok": True, "verb": "help", "data": _help()}
    if verb == "quit":
        return {"ok": True, "verb": "quit", "data": {"bye": True}}

    result = bridge.dispatch(verb, **args)
    response = {"ok": result.ok, "verb": result.verb}
    if result.ok:
        response["data"] = result.data
    else:
        response["error"] = result.error
    return response


def run_terminal(
    bridge: Bridge | None = None,
    read_fn=input,
    write_fn=print,
    config_path: str | None = None,
) -> None:
    """Read lines until `quit` or end of input, writing one JSON line each.

    No prompt is printed. The caller here is normally another program, and
    a `saipet> ` on stdout would sit in front of every JSON line it has to
    parse. `read_fn`/`write_fn` are injectable for the same reason the
    interactive review loop's are: a protocol that can only be tested by
    faking stdin ends up untested.

    CORE-004: when no engine is injected, the default terminal applies the
    project's persisted runtime config (saipet.config.json) BEFORE building
    the bridge, so the agent control plane honors the same allowlist, gates,
    weights and TTL as CLI/monitor/GUI. An invalid config is a clear startup
    failure, not a silent fall-back to defaults. W2-001: the default engine
    also uses the durable review store (review-state.json) so a finding
    survives a restart; a corrupt store fails explicitly rather than falling
    back to an empty queue.
    """
    if bridge is None:
        try:
            apply_overrides(load_overrides(config_path or DEFAULT_CONFIG_PATH))
        except ConfigError:
            raise
        engine = Bridge(review_path=DEFAULT_REVIEW_PATH)
    else:
        engine = bridge
    while True:
        try:
            line = read_fn()
        except EOFError:
            return
        if line is None:
            return
        if not line.strip():
            continue
        response = handle_line(engine, line)
        write_fn(json.dumps(response, ensure_ascii=False))
        if response["verb"] == "quit":
            return
