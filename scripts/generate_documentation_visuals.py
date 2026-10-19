"""Capture real templates with synthetic offline fixtures; never contact a deployment."""
import json
import mimetypes
from pathlib import Path
import subprocess
import time
from urllib.parse import urlsplit, unquote

from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs/assets'


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    admin = ROOT / 'backend/app'
    studio = ROOT / 'studio/web'
    templates = Environment(loader=FileSystemLoader(admin / 'templates'), autoescape=select_autoescape())
    studio_templates = Environment(loader=FileSystemLoader(studio / 'templates'), autoescape=select_autoescape())
    now = '2026-10-19T09:00:00Z'
    videos = [dict(id=f'demo-{i}', title=t, status='READY', quality_summary='1080p', duration_seconds=1240+i*55,
                   size_bytes=524288000, created_at=now) for i,t in enumerate(['رحلة الضوء — الحلقة 01', 'آفاق جديدة — الحلقة 02', 'مذكّرات الطبيعة — الحلقة 03', 'المسار الأخير — الحلقة 04'])]
    data = dict(videos=dict(total=24, ready=24, processing=0, failed=0), jobs=dict(active_count=0, queued_count=0, items=[]),
                storage=dict(used_bytes=18*1024**3, free_bytes=82*1024**3, total_bytes=100*1024**3, used_percentage=18, status='healthy'),
                services=dict(api='healthy', database='healthy', redis='healthy', storage='healthy', worker='healthy'),
                latest_videos=videos, activity=[], generated_at=now)
    episodes=[dict(filename=f'Demo Episode {i:02}.mkv', episode_number=i, source_path=f'D:\\Media\\Demo\\Season 01\\Demo Episode {i:02}.mkv', relative_source_path=f'Season 01/Demo Episode {i:02}.mkv', source_bytes=1024**3, issues=[]) for i in range(1,5)]
    fixtures = {
        '/api/v1/admin/dashboard': data,
        '/api/v1/admin/transcodes': {'items': [], 'total': 0},
        '/api/v1/health': {'status': 'healthy', 'version': '1.0.0'},
        '/api/v1/admin/quality-settings/encoder': {'preset': 'veryfast', 'video_bitrate_percent': 100, 'enabled_qualities': ['720p','1080p'], 'available_qualities': ['360p','480p','720p','1080p'], 'four_k_allowed': False},
        '/api/config': {'converted_output_folder': 'D:\\Media\\Packages', 'default_encoder': 'cpu', 'default_preset': 'balanced', 'first_run_complete': True, 'server_url': '', 'cpu_concurrent_jobs': 1, 'gpu_concurrent_jobs': 1},
        '/api/hardware': {'hardware': {'cpu_ram': {'cpu_util_pct':12,'cpu_cores_physical':8,'cpu_cores_logical':16,'ram_used_mb':8192,'ram_total_mb':32768,'ram_util_pct':25}, 'gpu': {'gpu_detected':True,'gpu_name':'Compatible GPU','gpu_util_pct':4,'vram_used_mb':512,'vram_total_mb':8192,'vram_util_pct':6,'gpu_temp_c':36}}},
        '/api/batch/browse-folder': {'path':'D:\\Media\\Demo','cancelled':False},
        '/api/batch/inspect-season': {'series_name':'رحلة الضوء — عرض توضيحي','episode_count':4,'season_count':1,'source_bytes':4*1024**3,'errors':[],'seasons':[{'name':'الموسم الأول','season_number':1,'episode_count':4,'episodes':episodes}]},
        '/api/batch/current': {'episodes': [], 'batches': [], 'running': False},
        '/api/server/status': {'connected': False, 'configured': False, 'authenticated': False},
    }
    evidence = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for name, template, route_path, page_id, base in [
            ('platform-dashboard', 'admin_dashboard.html', '/admin', 'dashboard', admin),
            ('platform-upload', 'admin_upload.html', '/admin/upload', 'upload', admin),
            ('studio', 'index_v3.html', '/', 'studio', studio),
        ]:
            context = browser.new_context(viewport={'width': 1600, 'height': 1000}, device_scale_factor=1)
            context.route_web_socket('**/*', lambda ws: None)
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            html = (studio_templates if page_id == 'studio' else templates).get_template(template).render(
                page_id=page_id, page_title='نظرة عامة' if page_id=='dashboard' else 'الرفع والاستيراد',
                server_transcoding_enabled=True, version='v1.0.0')

            def route(request_route):
                path = unquote(urlsplit(request_route.request.url).path)
                if path == route_path:
                    request_route.fulfill(status=200, content_type='text/html', body=html)
                elif path.startswith('/static/'):
                    target = (base / path.lstrip('/')).resolve()
                    if target.is_relative_to(base) and target.is_file():
                        mime = mimetypes.guess_type(str(target))[0] or 'application/octet-stream'
                        if target.suffix == '.js': mime='application/javascript'
                        request_route.fulfill(status=200, content_type=mime, body=target.read_bytes())
                    else:request_route.fulfill(status=404, body='')
                elif path.endswith('/poster'):
                    request_route.fulfill(status=200,content_type='image/svg+xml',body='<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180"><defs><linearGradient id="g"><stop stop-color="#103d45"/><stop offset="1" stop-color="#0b1822"/></linearGradient></defs><rect width="320" height="180" fill="url(#g)"/><circle cx="160" cy="90" r="34" fill="#65dfca" opacity=".15"/><path d="m151 72 28 18-28 18z" fill="#65dfca"/></svg>')
                elif path == '/api/v1/admin/dashboard/live':
                    ticks=int(time.monotonic()*1000000)
                    request_route.fulfill(status=200,json={'available':True,'received_bytes':ticks,'sent_bytes':ticks*2})
                elif path in fixtures:request_route.fulfill(status=200,json=fixtures[path])
                elif path.startswith('/api/'):request_route.fulfill(status=200,json={'items':[],'total':0})
                else:request_route.fulfill(status=404,body='')

            page.route('**/*', route)
            page.goto('http://127.0.0.1:18899'+route_path, wait_until='networkidle')
            page.evaluate('document.fonts.ready')
            if page_id=='upload':
                page.locator('#file-input-files').set_input_files({'name':'Sample Episode 01.mp4','mimeType':'video/mp4','buffer':b'\0'*(1024*1024)})
                page.wait_for_selector('#upload-processing-controls:not(.hidden)')
            if page_id=='dashboard':page.wait_for_function("document.getElementById('kpiVideosVal').textContent==='24'")
            if page_id=='studio':
                page.locator('#add-season').click()
                page.wait_for_selector('.review-row')
            page.wait_for_timeout(900)
            page.screenshot(path=str(OUT / (name+'.png')))
            assert not errors, (name,errors)
            evidence.append({'image':name+'.png','actual_template':template,'synthetic_offline_data':True,'page_errors':errors})
            context.close()
        browser.close()
    (OUT/'screenshots.json').write_text(json.dumps(evidence,indent=2),encoding='utf-8')
    print('Captured three actual interfaces with offline synthetic data.')


if __name__ == '__main__':main()
