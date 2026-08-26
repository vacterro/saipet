"""Golden Default -- the only palette, the only spacing, the only typography.

Every visible color in the GUI resolves to a token in PALETTE; every font is
Verdana at one of the allowed sizes; every border is exactly BEVEL pixels.
UI.md is the binding contract for these values; tests/test_gui_theme.py pins
them so a stray hand-typed hex cannot drift in.
"""

# The 21 canonical Golden Default tokens, byte-for-byte from UI.md.
PALETTE = {
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

# Named bindings -- every widget resolves a colour once, at import, from the
# single PALETTE dict. A name is never a second source of truth.
BACKGROUND = PALETTE["background"]
BACKGROUND_SOFT = PALETTE["backgroundSoft"]
SURFACE = PALETTE["surface"]
SURFACE_RAISED = PALETTE["surfaceRaised"]
SURFACE_ALT = PALETTE["surfaceAlt"]
BORDER_DARK = PALETTE["borderDark"]
BORDER_HIGHLIGHT = PALETTE["borderHighlight"]
BEVEL_LIGHT = PALETTE["bevelLight"]
BORDER_MUTED = PALETTE["borderMuted"]
TEXT_PRIMARY = PALETTE["textPrimary"]
TEXT_SECONDARY = PALETTE["textSecondary"]
TEXT_MUTED = PALETTE["textMuted"]
ACCENT_TEAL = PALETTE["accentTeal"]
ACCENT_TEAL_DEEP = PALETTE["accentTealDeep"]
SUCCESS = PALETTE["success"]
WARNING = PALETTE["warning"]
DANGER = PALETTE["danger"]
DANGER_TEXT = PALETTE["dangerText"]
SELECTION = PALETTE["selection"]
COMPARE_BACK = PALETTE["compareBack"]
LINK = PALETTE["link"]

# Typography: Verdana everywhere, only these pixel sizes. Negative sizes tell
# Tk "pixels" instead of points, which is the deterministic way to honour the
# px contract across displays.
FONT_FAMILY = "Verdana"
ALLOWED_FONT_SIZES = (10, 11, 12, 14, 16)
TITLE_FONT = (FONT_FAMILY, -16)
SECTION_FONT = (FONT_FAMILY, -14)
BODY_FONT = (FONT_FAMILY, -12)
LIST_FONT = (FONT_FAMILY, -11)
META_FONT = (FONT_FAMILY, -10)

# Spacing scale (UI.md layout rules).
MARGIN = 12        # outer margins: 12-16 px
SECTION_GAP = 8    # between sections
GROUP_PAD = 4      # inside a group/panel
CONTROL_PAD = 2    # inside a control

# Depth language: the one bevel width in the whole application.
BEVEL = 2

# Geometry constants.
ROW_HEIGHT = 18    # list row target: 16-18 px
MIN_BUTTON_H = 24  # primary control minimum target
SCREEN_MIN_W = 640
SCREEN_MIN_H = 480
DEFAULT_W = 900
DEFAULT_H = 620
