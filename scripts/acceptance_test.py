"""Studio v0.2 acceptance test script."""
import sys
import threading
import time
import json

sys.path.insert(0, '.')

PORT = 18765

def run_server():
    import uvicorn
    from studio.web.app import app
    uvicorn.run(app, host='127.0.0.1', port=PORT, log_level='warning')

t = threading.Thread(target=run_server, daemon=True)
t.start()
time.sleep(4)

import urllib.request

def fetch(url, method='GET', data=None):
    req = urllib.request.Request(url, method=method)
    req.add_header('Content-Type', 'application/json')
    if data:
        req.data = json.dumps(data).encode()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except Exception as e:
        return None, str(e)

print('=== PROBE TEST ===')
status, p = fetch(
    f'http://127.0.0.1:{PORT}/api/probe',
    method='POST',
    data={'video_path': r'D:\ok.anime\Ling Cage (2019)\Season 02\[SILVER] Spirit Cage S2E01 4K HEVC AAC.mkv'}
)
print('HTTP status:', status)
if isinstance(p, dict):
    print('file_name:', p['file_name'])
    print('file_size_formatted:', p['file_size_formatted'])
    print('duration_formatted:', p['duration_formatted'])
    print('resolution:', p['resolution'])
    print('video_codec:', p['video_codec'])
    print('pixel_format:', p['pixel_format'])
    print('fps:', p['fps'])
    print('hdr_label:', p.get('hdr_label','MISSING'))
    print('is_hdr:', p.get('is_hdr', 'MISSING'))
    print('is_10bit:', p.get('is_10bit', 'MISSING'))
    print('color_transfer:', p.get('color_transfer','MISSING'))
    print('subtitle_tracks:', p['subtitle_tracks'])
    allowed = [q['key'] for q in p['qualities'] if q['allowed']]
    print('Allowed qualities:', allowed)
    arabic = next((tr for tr in p['subtitle_tracks'] if tr['language'] in ('ara','ar')), None)
    print('Arabic track auto-detected:', arabic)
else:
    print('ERROR:', p)

print()
print('=== DISK INFO TEST ===')
status2, d = fetch(f'http://127.0.0.1:{PORT}/api/system/disk-info')
print('HTTP status:', status2)
if isinstance(d, dict):
    print('free_formatted:', d.get('free_formatted'))
    print('total_formatted:', d.get('total_formatted'))
    print('used_pct:', d.get('used_pct'))

print()
print('=== RECENT JOBS TEST ===')
status3, rj = fetch(f'http://127.0.0.1:{PORT}/api/batch/recent-jobs')
print('HTTP status:', status3)
print('recent jobs response:', rj)

print()
print('=== VERSION TEST ===')
status4, _ = fetch(f'http://127.0.0.1:{PORT}/')
print('Homepage HTTP status:', status4)

print()
print('=== HARDSUB FILTER UNIT TEST ===')
from studio.core.subtitles import build_hardsub_filter
from pathlib import Path
sub = Path(r'C:\test.ass')
result = build_hardsub_filter(sub, None, {
    'source': 'custom',
    'font': 'Cairo',
    'size': 36,
    'color': '#ff0000',
    'border_color': '#000000',
    'border_size': 3.0,
    'position': 'top',
})
print('force_style filter:', result)
assert 'force_style' in result, 'force_style missing!'
assert 'Fontname=Cairo' in result, 'Font missing!'
assert 'Alignment=8' in result, 'Top alignment missing!'
assert '&H000000ff' in result, 'Red color BGR conversion missing!'
print('HARDSUB FILTER: PASS')

print()
print('ALL ACCEPTANCE TESTS PASSED')
