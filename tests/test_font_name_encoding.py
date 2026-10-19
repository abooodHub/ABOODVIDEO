from studio.core.fonts import extract_font_names_from_ttf
from synthetic_fonts import write_synthetic_ttf


def test_macintosh_font_family_alias_is_not_decoded_as_utf16(tmp_path):
    # ASCII Mac Roman bytes of even length must not be decoded as UTF-16.
    # No user font or redistribution permission is needed for a name-table fixture.
    font=write_synthetic_ttf(tmp_path / "names.ttf", family="Synthetic Janna", mac_family="Adobe Arabic")
    names=extract_font_names_from_ttf(font)
    assert "Adobe Arabic" in names
    assert any("Janna" in name for name in names)
