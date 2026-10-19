"""Softsub package test for Spirit Cage S2E01."""
import sys
import tempfile
import json
from pathlib import Path
sys.path.insert(0, '.')

from studio.core.subtitles import extract_embedded_subtitle, process_softsub_file
from studio.core.fonts import parse_ass_fonts, package_required_fonts

src = Path(r'D:\ok.anime\Ling Cage (2019)\Season 02\[SILVER] Spirit Cage S2E01 4K HEVC AAC.mkv')
work = Path(tempfile.mkdtemp())

# Extract embedded subtitle (stream 2, Arabic ASS)
print('[1] Extracting embedded ASS subtitle...')
ext_path, ext_fmt = extract_embedded_subtitle(src, 2, work / 'extracted_subs', 'ar')
print(f'    Extracted: {ext_path.name} ({ext_path.stat().st_size} bytes)')
print(f'    Format: {ext_fmt}')

# Process as softsub
print('[2] Processing softsub...')
sub_res = process_softsub_file(ext_path, work / 'subtitles', 'ar')
print('    format:', sub_res['format'])
print('    primary_rel_path:', sub_res['primary_rel_path'])

# Parse ASS fonts
print('[3] Parsing ASS font requirements...')
req_fonts, _ = parse_ass_fonts(sub_res['primary_abs_path'])
print(f'    Required fonts ({len(req_fonts)}):', req_fonts)

# Package fonts (extracts from MKV attachments)
print('[4] Packaging fonts (including MKV attachments)...')
pkged = package_required_fonts(req_fonts, work / 'fonts', src)
print(f'    Packaged: {len(pkged)} font files')
for f in pkged:
    print(f'      {f["path"]} (sha256: {f["sha256"][:12]}...)')

print()
print('[5] Package directory contents:')
for item in sorted(work.rglob('*')):
    if item.is_file():
        print(f'    {item.relative_to(work)} ({item.stat().st_size} bytes)')

# Verify source MKV untouched
print()
print('[6] Source MKV integrity check:')
print('    Exists:', src.exists())
print('    Size unchanged:', src.stat().st_size == 1226667095)

print()
print('SOFTSUB EXTRACTION TEST COMPLETE')
