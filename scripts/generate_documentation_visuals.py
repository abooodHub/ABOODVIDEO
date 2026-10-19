"""Capture real templates with synthetic offline fixtures; never contact a deployment."""
import json
import argparse
import mimetypes
from pathlib import Path
import subprocess
import time
import sys
from urllib.parse import urlsplit, unquote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.sync_api import sync_playwright
from shared.i18n import configure_environment, use_language, gettext, WEB_DIR

OUT = ROOT / 'docs/assets'


def main():
    global OUT
    args=argparse.ArgumentParser(description=__doc__)
    args.add_argument('--language',choices=['ar','en'],default='ar')
    args.add_argument('--output',type=Path,default=OUT)
    args.add_argument('--width',type=int,default=1600)
    args.add_argument('--extended',action='store_true',help='Also check library, settings and management pages')
    options=args.parse_args()
    OUT=options.output
    OUT.mkdir(parents=True, exist_ok=True)
    admin = ROOT / 'backend/app'
    studio = ROOT / 'studio/web'
    templates = configure_environment(Environment(loader=FileSystemLoader(admin / 'templates'), autoescape=select_autoescape()))
    studio_templates = configure_environment(Environment(loader=FileSystemLoader(studio / 'templates'), autoescape=select_autoescape()))
    now = '2026-10-19T09:00:00Z'
    videos = [dict(id=f'demo-{i}', title=t, status='READY', quality_summary='1080p', duration_seconds=1240+i*55,
                   size_bytes=524288000, created_at=now) for i,t in enumerate(['رحلة الضوء — الحلقة 01', 'آفاق جديدة — الحلقة 02', 'مذكّرات الطبيعة — الحلقة 03', 'المسار الأخير — الحلقة 04'])]
    data = dict(videos=dict(total=24, ready=24, processing=0, failed=0), jobs=dict(active_count=0, queued_count=0, items=[]),
                storage=dict(used_bytes=18*1024**3, free_bytes=82*1024**3, total_bytes=100*1024**3, used_percentage=18, status='healthy'),
                services=dict(api='healthy', database='healthy', redis='healthy', storage='healthy', worker='healthy'),
                latest_videos=videos, activity=[], generated_at=now)
    episodes=[dict(filename=f'Demo Episode {i:02}.mkv', episode_number=i, source_path=f'D:\\Media\\Demo\\Season 01\\Demo Episode {i:02}.mkv', relative_source_path=f'Season 01/Demo Episode {i:02}.mkv', source_bytes=1024**3, issues=[]) for i in range(1,5)]
    if options.extended:
        episodes[0]['issues']=['رقم الحلقة غير محدد؛ يحتاج مراجعة']
    fixtures = {
        '/api/v1/drive/tree': {'tree':[], 'trash_count':0},
        '/api/v1/drive/scope': {'items':[{'id':'00000000-0000-4000-8000-000000000001',
            'name':'إضافة محتوى', 'type':'video','type_label':'فيديو','status':'READY','visibility':'PRIVATE'}],
            'breadcrumbs':[], 'total_items':1},
        '/api/v1/admin/dashboard': data,
        '/api/v1/admin/transcodes': {'items': [], 'total': 0},
        '/api/v1/health': {'status': 'healthy', 'version': '1.1.0'},
        '/api/v1/admin/quality-settings/encoder': {'preset': 'veryfast', 'video_bitrate_percent': 100, 'enabled_qualities': ['720p','1080p'], 'available_qualities': ['360p','480p','720p','1080p'], 'four_k_allowed': False},
        '/api/config': {'converted_output_folder': 'D:\\Media\\Packages', 'default_encoder': 'cpu', 'default_preset': 'balanced', 'first_run_complete': True, 'server_url': '', 'cpu_concurrent_jobs': 1, 'gpu_concurrent_jobs': 1},
        '/api/hardware': {'hardware': {'cpu_ram': {'cpu_util_pct':12,'cpu_cores_physical':8,'cpu_cores_logical':16,'ram_used_mb':8192,'ram_total_mb':32768,'ram_util_pct':25}, 'gpu': {'gpu_detected':True,'gpu_name':'Compatible GPU','gpu_util_pct':4,'vram_used_mb':512,'vram_total_mb':8192,'vram_util_pct':6,'gpu_temp_c':36}}},
        '/api/batch/browse-folder': {'path':'D:\\Media\\Demo','cancelled':False},
        '/api/batch/inspect-season': {'series_name':'رحلة الضوء — عرض توضيحي','episode_count':4,'season_count':1,'source_bytes':4*1024**3,'errors':[],'seasons':[{'name':'الموسم الأول','season_number':1,'episode_count':4,'episodes':episodes}]},
        '/api/batch/current': {'episodes': [], 'batches': [], 'running': False},
        '/api/server/status': {'connected': False, 'configured': False, 'authenticated': False},
    }
    evidence = []
    pages = [
        ('platform-dashboard', 'admin_dashboard.html', '/admin', 'dashboard', admin),
        ('platform-upload', 'admin_upload.html', '/admin/upload', 'upload', admin),
        ('studio', 'index_v3.html', '/', 'studio', studio),
    ]
    if options.extended:
        pages += [(f'platform-{name}',template,'/admin/'+route,name,admin) for name,template,route in [
            ('content','drive.html','videos'), ('settings','admin_settings.html','settings'),
            ('shares','admin_shares.html','shares'), ('users','admin_users.html','users'),
            ('api-keys','admin_api_keys.html','api-keys'), ('analytics','admin_analytics.html','analytics'),
            ('processing','admin_processing.html','processing'), ('audit','admin_audit.html','audit'),
        ]]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for name, template, route_path, page_id, base in pages:
            context = browser.new_context(viewport={'width': options.width, 'height': 1000}, device_scale_factor=1)
            context.add_cookies([{'name':'av_language','value':options.language,'url':'http://127.0.0.1:18899/'}])
            context.route_web_socket('**/*', lambda ws: None)
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with use_language(options.language):
                html = (studio_templates if page_id == 'studio' else templates).get_template(template).render(
                    page_id=page_id, page_title=gettext('نظرة عامة' if page_id=='dashboard' else 'الرفع والاستيراد'),
                    server_transcoding_enabled=True, version='v1.1.0')

            def route(request_route):
                path = unquote(urlsplit(request_route.request.url).path)
                if path == route_path:
                    request_route.fulfill(status=200, content_type='text/html', body=html)
                elif path.startswith('/static/i18n/'):
                    target=(WEB_DIR/path.rsplit('/',1)[-1]).resolve()
                    if target.is_relative_to(WEB_DIR) and target.is_file():
                        request_route.fulfill(status=200,content_type='application/javascript' if target.suffix=='.js' else 'text/css',body=target.read_bytes())
                    else:request_route.fulfill(status=404,body='')
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
                assert page.locator('[data-language-select]').is_disabled(), 'Queued files must survive language selection'
            if page_id=='dashboard':page.wait_for_function("document.getElementById('kpiVideosVal').textContent==='24'")
            if page_id=='shares':
                with page.expect_request(lambda req: urlsplit(req.url).path=='/api/v1/drive/shares' and 'sort=expires_at' in req.url):
                    page.locator('#sharesSort').select_option('expires_at:asc')
                with page.expect_request(lambda req: urlsplit(req.url).path=='/api/v1/drive/shares' and 'limit=50' in req.url):
                    page.locator('#sharesPageSize').select_option('50')
                with page.expect_request(lambda req: urlsplit(req.url).path=='/api/v1/drive/shares' and 'q=Synthetic' in req.url):
                    page.locator('#sharesSearch').fill('Synthetic')
            if page_id=='studio':
                page.locator('#add-season').click()
                page.wait_for_selector('.review-row')
                if options.extended:
                    assert page.locator('.review-issue').first.inner_text()==gettext_for_language('رقم الحلقة غير محدد؛ يحتاج مراجعة',options.language)
            if page_id=='content':
                page.wait_for_selector('.drive-table tbody tr[data-id]')
                assert 'إضافة محتوى' in page.locator('.drive-table tbody').inner_text()
                assert gettext_for_language('فيديو',options.language) in page.locator('.drive-table tbody').inner_text()
            page.wait_for_timeout(900)
            suffix='-en' if options.language=='en' else ''
            page.screenshot(path=str(OUT / (name+suffix+'.png')))
            assert not errors, (name,errors)
            layout=page.evaluate('''() => {
              const sidebar=document.querySelector('.sidebar,.console-sidebar');
              const rect=sidebar?.getBoundingClientRect();
              return {width:innerWidth, document_width:document.documentElement.scrollWidth,
                direction:document.documentElement.dir, language:document.documentElement.lang,
                sidebar:rect?{left:rect.left,right:rect.right,width:rect.width}:null};
            }''')
            assert layout['language']==options.language, layout
            assert layout['direction']==('ltr' if options.language=='en' else 'rtl'),layout
            assert layout['document_width']<=options.width+1,(name,layout)
            if options.width>900:
                edge=layout['sidebar']['left'] if options.language=='en' else options.width-layout['sidebar']['right']
                assert abs(edge)<2,(name,layout)
            missing=page.evaluate('Array.from(window.AVi18n.missing)')
            assert not missing,(name,missing)
            evidence.append({'image':name+suffix+'.png','language':options.language,'actual_template':template,'synthetic_offline_data':True,'page_errors':errors,'missing_messages':missing,'layout':layout})
            context.close()
        browser.close()
    (OUT/('screenshots-en.json' if options.language=='en' else 'screenshots.json')).write_text(json.dumps(evidence,indent=2),encoding='utf-8')
    print(f'Checked {len(pages)} actual interfaces with offline synthetic data.')


def gettext_for_language(message, locale):
    with use_language(locale):
        return gettext(message)


if __name__ == '__main__':main()
