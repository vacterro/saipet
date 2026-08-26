"""UI contract automated checks: palette drift, forbidden ttk, font sizes,
bevel width, accidental ad-hoc hex colours."""

from saipet.gui import qa


def test_ui_palette_has_no_drift():
    assert qa.check_palette() == []


def test_ui_fonts_stay_within_allowed_sizes():
    assert qa.check_fonts() == []


def test_bevel_is_exactly_two_px():
    assert qa.check_bevel() == []


def test_no_ttk_in_gui_widgets():
    assert qa.check_no_ttk() == []


def test_no_adhoc_hex_colours_outside_the_palette():
    assert qa.check_no_adhoc_hex() == []