"""Shared text hygiene for anything rendered as Markdown or printed.

One implementation of the sanitisation rules, used by every output surface
(notify sinks, run reports): untrusted internet text must not be able to
forge extra lines, ANSI escapes, or Markdown structure. The JSONL feeds
keep raw text -- machines read those, humans read the rendered surfaces.
"""

import re

# Unicode categories for control characters we want to drop: Cc (control),
# Cf (format), Cs (surrogate), Co (private use), Cn (unassigned).
CONTROL_RE = re.compile(r"[\x00-\x0d\x0e-\x1f\x7f\uFFF0-\uFFFF]")
# ANSI escape sequences: ESC[ ... any printable bytes ... m, x, K, etc.
ANSI_RE = re.compile(r"\x1b\[[\d;]*[A-Za-z]")

# Characters that carry structure in the Markdown we emit.
_MD_SPECIALS = r"\*`_[]()>#-+.!|"


def strip_untrusted(text: str) -> str:
    """Drop ANSI escapes and control characters, trim the edges."""
    text = ANSI_RE.sub("", text)
    text = CONTROL_RE.sub(" ", text)
    return text.strip()


def markdown_escape(text: str) -> str:
    """Escape every character that carries Markdown structure."""
    for ch in _MD_SPECIALS:
        text = text.replace(ch, "\\" + ch)
    return text


def safe_link_destination(url) -> str | None:
    """A permalink is only ever an https URL. Returns the URL unchanged when
    it is safe to link to, else None (the caller renders plain text)."""
    from urllib.parse import urlparse

    if not url or not isinstance(url, str):
        return None
    parsed = urlparse(url)
    if parsed.scheme not in ("https",):
        return None
    if not parsed.netloc:
        return None
    return url
