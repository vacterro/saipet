"""Lightweight automated UI-contract checks.

These are the regression tripwires behind the UI.md contract: palette drift,
unapproved fonts, forbidden ttk, accidental ad-hoc hex, wrong bevel width.
They are plain functions returning problem strings -- tests assert on them,
a human can run them from a REPL. Not a screenshot framework; deliberately
small.
"""

import re
from pathlib import Path

from saipet.gui import theme

# The canonical token/value set, copied verbatim from UI.md. theme.PALETTE
# must equal this exactly -- no drift, no additions, no deletions.
CANONICAL_PALETTE = {
    "background": "#1A1810",
    "backgroundSoft": "#232018",
    "surface": "#332E22",
    "surfaceRaised": "#3D372A",
    "surfaceAlt": "#453D30",
    "borderDark": "#100E08",
    "borderHighlight": "#F0D060",
    "bevelLight": "#75663D",
    "borderMuted": "#5A5040",
    "textPrimary": "#D4C89A",
    "textSecondary": "#9C9371",
    "textMuted": "#6E674E",
    "accentTeal": "#008080",
    "accentTealDeep": "#004C4C",
    "success": "#4A7A20",
    "warning": "#7A7A20",
    "danger": "#7A2020",
    "dangerText": "#D66464",
    "selection": "#3D372A",
    "compareBack": "#14120C",
    "link": "#F0D060",
}

_GUI_ROOT = Path(__file__).resolve().parent
_HEX_RE = re.compile(r"#[0-9A-Fa-f]{6}")


def check_palette() -> list[str]:
    """Palette must match the canonical 21 values exactly."""
    expected = CANONICAL_PALETTE
    actual = dict(theme.PALETTE)
    problems = []
    missing = set(expected) - set(actual)
    extra = set(actual) - set(expected)
    if missing:
        problems.append(f"missing tokens: {', '.join(sorted(missing))}")
    if extra:
        problems.append(f"extra tokens: {', '.join(sorted(extra))}")
    for key in sorted(set(expected) & set(actual)):
        if expected[key] != actual[key]:
            problems.append(f"drift {key}: expected {expected[key]}, got {actual[key]}")
    return problems


def check_fonts() -> list[str]:
    """Every named font constant is Verdana at an allowed pixel size."""
    problems = []
    for name in dir(theme):
        if name.startswith("_"):
            continue
        value = getattr(theme, name)
        if not (isinstance(value, tuple) and len(value) == 2):
            continue
        family, size = value
        if family != theme.FONT_FAMILY:
            problems.append(f"{name} uses family {family!r}, expected {theme.FONT_FAMILY!r}")
        if size >= 0:
            problems.append(f"{name} uses a positive (point) size {size}; use negative pixels")
        if -size not in theme.ALLOWED_FONT_SIZES:
            problems.append(f"{name} uses disallowed size {abs(size)}")
    return problems


def check_bevel() -> list[str]:
    """The whole depth language is one 2px bevel."""
    if theme.BEVEL != 2:
        return [f"BEVEL={theme.BEVEL}, expected 2"]
    return []


def check_no_ttk() -> list[str]:
    """The themed widgets must stay classic tk; ttk could re-skin them.

    Looks for actual ttk module use (`ttk.` or an explicit ttk import), not
    the word in prose -- the docstrings say "no ttk" on purpose.
    """
    use_patterns = ("ttk.", "import ttk")
    problems = []
    for path in _GUI_ROOT.rglob("*.py"):
        if path.name in ("qa.py", "theme.py"):
            continue
        text = path.read_text(encoding="utf-8")
        if any(pattern in text for pattern in use_patterns):
            problems.append(f"{path.name} references ttk")
    return problems


def check_no_adhoc_hex() -> list[str]:
    """Every hex literal in the GUI source resolves to a Golden Default token."""
    allowed = set(CANONICAL_PALETTE.values())
    problems = []
    for path in _GUI_ROOT.rglob("*.py"):
        if path.name == "theme.py":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for hit in _HEX_RE.findall(line):
                if hit.lower() not in {v.lower() for v in allowed}:
                    problems.append(f"{path.name}:{lineno} uses non-token colour {hit}")
    return problems
