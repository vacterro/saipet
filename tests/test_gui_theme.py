"""The theme regression test: exact Golden Default tokens and typography.

These pin the palette so a stray hand-typed hex, a "close enough" colour, or
an unapproved font size cannot drift into the GUI.
"""

from saipet.gui import qa, theme


def test_all_21_canonical_tokens_are_present():
    assert set(theme.PALETTE) == set(qa.CANONICAL_PALETTE)


def test_every_value_is_exactly_the_canonical_one():
    assert theme.PALETTE == qa.CANONICAL_PALETTE


def test_the_palette_has_no_second_colour_system():
    assert len(theme.PALETTE) == 21


def test_theme_has_no_extra_ad_hoc_palette():
    assert qa.check_palette() == []


def test_only_allowed_font_sizes_are_used():
    assert qa.check_fonts() == []


def test_font_family_is_verdana_everywhere():
    assert theme.FONT_FAMILY == "Verdana"


def test_font_size_contract_smoke():
    assert theme.ALLOWED_FONT_SIZES == (10, 11, 12, 14, 16)
    assert {abs(theme.TITLE_FONT[1]), abs(theme.SECTION_FONT[1])} & {14, 16}


def test_bevel_width_is_exactly_two():
    assert qa.check_bevel() == []


def test_no_adhoc_hex_values_outside_the_palette():
    assert qa.check_no_adhoc_hex() == []


def test_no_ttk_in_the_core_widget_set():
    assert qa.check_no_ttk() == []
