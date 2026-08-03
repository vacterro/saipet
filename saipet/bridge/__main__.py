"""`python -m saipet.bridge` -- the command engine on stdin/stdout.

A separate module rather than a `__main__` block inside terminal.py: the
package's `__init__` already imports that module, so running it directly as
a script imports it twice and Python warns about exactly that.
"""

from saipet.bridge.terminal import run_terminal

run_terminal()
