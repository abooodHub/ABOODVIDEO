"""Tiny deterministic OpenType name-table fixtures for font handling tests."""
from __future__ import annotations

import struct
from pathlib import Path


def write_synthetic_ttf(path: Path, family: str = "ABOODVIDEO Test Font", *, mac_family: str | None = None) -> Path:
    """Write a minimal TTF-shaped fixture with a valid family-name table.

    The fixture is sufficient for ABOODVIDEO's metadata parser and FFmpeg's
    Matroska attachment tests. It is not a renderable or distributable font.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    family_bytes = family.encode("utf-16-be")
    records = [(3, 1, 0x0409, 1, family_bytes)]
    if mac_family is not None:
        records.append((1, 0, 0, 1, mac_family.encode("mac_roman")))
    name_table = struct.pack(">HHH", 0, len(records), 6 + 12 * len(records))
    strings = b""
    for platform, encoding, language, name_id, value in records:
        name_table += struct.pack(">HHHHHH", platform, encoding, language, name_id, len(value), len(strings))
        strings += value
    name_table += strings

    offset = 28  # 12-byte sfnt header followed by one 16-byte table record.
    sfnt_header = struct.pack(">IHHHH", 0x00010000, 1, 16, 0, 0)
    table_record = b"name" + struct.pack(">III", 0, offset, len(name_table))
    path.write_bytes(sfnt_header + table_record + name_table)
    return path
