"""`python -m saipet.bridge` -- the command engine on stdin/stdout.

A separate module rather than a `__main__` block inside terminal.py: the
package's `__init__` already imports that module, so running it directly as
a script imports it twice and Python warns about exactly that.
"""

import sys

from saipet.bridge.terminal import run_terminal
from saipet.jsonio import StateFileError
from saipet.runtime_config import ConfigError


def _parse_config(argv: list[str]) -> str | None:
    config_path = None
    rest = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--config" and i + 1 < len(argv):
            config_path = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--config="):
            config_path = arg.split("=", 1)[1]
            i += 1
            continue
        rest.append(arg)
        i += 1
    # Leave any remaining args for the engine's own (line-based) protocol.
    sys.argv[:] = [sys.argv[0]] + rest
    return config_path


def main() -> None:
    config_path = _parse_config(sys.argv[1:])
    try:
        run_terminal(config_path=config_path)
    except (ConfigError, StateFileError) as exc:
        # CORE-004 / W2-001: a bad config or corrupt durable review state is a
        # startup failure, surfaced loudly -- never a silent default fallback.
        print(f"saipet.bridge: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()

