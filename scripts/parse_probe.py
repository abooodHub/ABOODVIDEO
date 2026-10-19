import json
from pathlib import Path

with (Path(__file__).resolve().parents[1] / 'scratch' / 'probes' / 'probe_s2e01.json').open(encoding='utf-16') as f:
    d = json.load(f)

vs = [s for s in d['streams'] if s.get('codec_type')=='video'][0]
subs = [s for s in d['streams'] if s.get('codec_type')=='subtitle']
aud = [s for s in d['streams'] if s.get('codec_type')=='audio']
fmt = d['format']

print('=== VIDEO ===')
print('Codec:', vs.get('codec_name'))
print('Resolution:', str(vs.get('width'))+'x'+str(vs.get('height')))
print('PixFmt:', vs.get('pix_fmt'))
print('ColorTransfer:', vs.get('color_transfer',''))
print('ColorPrimaries:', vs.get('color_primaries',''))
print('ColorSpace:', vs.get('color_space',''))
print('Profile:', vs.get('profile',''))
print('FPS:', vs.get('r_frame_rate',''))
dur = float(fmt.get('duration',0))
size = int(fmt.get('size',0))
h = int(dur//3600)
m = int((dur%3600)//60)
s = int(dur%60)
print('Duration:', f'{h:02d}:{m:02d}:{s:02d}')
print('Size:', f'{size/1024/1024/1024:.2f} GB ({size:,} bytes)')

print()
print('=== AUDIO TRACKS ===')
for a in aud:
    tags = a.get('tags',{})
    idx = a.get('index')
    codec = a.get('codec_name')
    lang = tags.get('language','')
    title = tags.get('title','')
    ch = a.get('channels')
    print(f'  Stream {idx}: {codec} | lang={lang} | title={title} | ch={ch}')

print()
print('=== SUBTITLE TRACKS ===')
for sub in subs:
    tags = sub.get('tags',{})
    idx = sub.get('index')
    codec = sub.get('codec_name')
    lang = tags.get('language','')
    title = tags.get('title','')
    print(f'  Stream {idx}: codec={codec} | lang={lang} | title={title}')

print()
print('=== HDR ANALYSIS ===')
ct = vs.get('color_transfer','')
cp = vs.get('color_primaries','')
cs = vs.get('color_space','')
pix = vs.get('pix_fmt','')
HDR_TRANSFERS = {'smpte2084','arib-std-b67','smpte428','bt2020-10','bt2020-12'}
is_hdr = ct.lower() in HDR_TRANSFERS or any(t in ct.lower() for t in ['pq','hlg','hdr'])
is_10bit = any(x in pix for x in ['p10','p12','10le','12le'])
print(f'color_transfer: {ct}')
print(f'color_primaries: {cp}')
print(f'color_space: {cs}')
print(f'pix_fmt: {pix}')
print(f'is_10bit: {is_10bit}')
print(f'is_hdr: {is_hdr}')
if is_hdr:
    label = 'HLG HDR' if 'arib' in ct.lower() else 'HDR (PQ/HDR10)'
elif is_10bit:
    label = '10-bit SDR'
else:
    label = 'SDR'
print(f'Correct label: {label}')
