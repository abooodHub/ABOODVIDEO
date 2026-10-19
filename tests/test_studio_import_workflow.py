"""Full-width import workflow, with real discovery and durable queue API in a disposable profile."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from urllib.request import urlopen
import pytest

pytestmark = pytest.mark.skipif(os.getenv("ABOODVIDEO_RUN_BROWSER_TEST") != "1", reason="Chromium opt-in")

@pytest.fixture
def workflow_server(tmp_path):
    root=Path(__file__).resolve().parents[1]
    profile=tmp_path/'profile'; user=profile/'ABOODVIDEO Studio';user.mkdir(parents=True,exist_ok=True)
    output=tmp_path/'output';output.mkdir(exist_ok=True)
    (user/'config.json').write_text(json.dumps({'server_url':'http://127.0.0.1:1/api/v1','converted_output_folder':str(output),'first_run_complete':True,'default_encoder':'cpu','processing_mode':'server'}),encoding='utf-8')
    source=tmp_path/'UI Test Series';sample=tmp_path/'sample.mp4'
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','color=c=black:s=320x180:r=24','-t','1','-c:v','mpeg4','-y',str(sample)],check=True,capture_output=True,timeout=30)
    for season,filename in [('Season 01','S01E01.mp4'),('Season 01','S01E02.mp4'),('Season 02','S02E01.mp4'),('Specials','S00E01.mp4')]:
        target=source/season/filename;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(sample,target)
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    env=dict(os.environ,LOCALAPPDATA=str(profile))
    process=subprocess.Popen([sys.executable,'-m','uvicorn','studio.web.app:app','--host','127.0.0.1','--port',str(port),'--log-level','error'],cwd=root,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    try:
        for _ in range(120):
            if process.poll() is not None:raise AssertionError('isolated Studio exited')
            try:urlopen(f'http://127.0.0.1:{port}/',timeout=1);break
            except OSError:time.sleep(.1)
        else:raise AssertionError('isolated Studio startup timed out')
        yield f'http://127.0.0.1:{port}',source,tmp_path
    finally:process.terminate();process.wait(timeout=15)


def test_full_width_selection_settings_and_real_subset_queue(workflow_server):
    from playwright.sync_api import sync_playwright
    url,source,tmp=workflow_server
    with sync_playwright() as pw:
        browser=pw.chromium.launch();page=browser.new_page(viewport={'width':1600,'height':1000});errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.goto(url)
        page.wait_for_function('()=>state.config.first_run_complete===true')
        assert page.locator('.import-layout,.import-options').count()==0
        assert page.locator('.source-tile').count()==3
        tiles=page.locator('.source-tile').all();assert len({round(t.bounding_box()['y']) for t in tiles})==1
        assert page.locator('#queue-selection').is_disabled()
        page.route('**/api/batch/browse-folder?kind=series',lambda r:r.fulfill(json={'path':str(source),'cancelled':False}))
        page.locator('#add-series').click();page.wait_for_function('()=>state.selection?.discovery?.episode_count===4')
        assert page.locator('.review-season').count()==3
        assert page.locator('.review-row').count()==4
        assert 'حلقات خاصة' in page.locator('#import-preview').inner_text()
        assert page.locator('#import-selected-count').inner_text()=='4 حلقات محددة'
        preview=page.locator('.import-review').bounding_box();ribbon=page.locator('.source-ribbon').bounding_box();assert preview['width']>=ribbon['width']*.99
        assert page.locator('#import-config').evaluate("el=>el.tagName==='SECTION'")
        assert page.locator('#destination-label,.dock-advanced').count()==0
        assert page.locator('#import-config').bounding_box()['y']<page.locator('.source-ribbon').bounding_box()['y']
        assert page.locator('#import-mode,[data-import-mode],[data-mode]').count()==0
        assert page.locator('#import-encoder-cpu').is_visible();assert page.locator('#import-preset,#import-quality-help,.quality-auto-note').count()==0
        row=page.locator('.review-row').filter(has_text='S01E02.mp4');row.locator('[data-import-select]').uncheck();assert '3 حلقات محددة' in page.locator('#import-selected-count').inner_text()
        page.locator('#import-select-all').check();page.locator('#import-select-all').uncheck();assert page.locator('#queue-selection').is_disabled();page.locator('#import-select-all').check();row.locator('[data-import-select]').uncheck()
        for width in (1920,1440,1100,800):
            page.set_viewport_size({'width':width,'height':900});assert page.evaluate('()=>document.documentElement.scrollWidth<=innerWidth')
            action=page.locator('.queue-commit').bounding_box();assert action['y']+action['height']<=902
        page.set_viewport_size({'width':1440,'height':950});page.locator('#queue-selection').click();page.wait_for_function('()=>!state.importBusy && state.selection===null',timeout=30000)
        records=page.request.get(url+'/api/batch/current').json();assert len(records['episodes'])==3
        assert all(ep['settings']['processing_mode']=='local' for ep in records['episodes']);assert all(ep['state']=='PENDING' for ep in records['episodes']);assert not records['runner']['is_running'];assert not any(ep['filename']=='S01E02.mp4' for ep in records['episodes'])
        assert {ep['settings']['series_name'] for ep in records['episodes']}=={'UI Test Series'}
        assert all(ep['settings']['upload_destination']['series_name']=='UI Test Series' and ep['settings']['upload_destination']['season_name'] for ep in records['episodes'])
        assert not errors,errors;browser.close()


def test_cancel_error_and_validation_do_not_destroy_selection(workflow_server):
    from playwright.sync_api import sync_playwright
    url,source,tmp=workflow_server
    with sync_playwright() as pw:
        browser=pw.chromium.launch();page=browser.new_page();page.goto(url)
        page.route('**/api/system/select-video',lambda r:r.fulfill(json={'path':str(source/'Season 01'/'S01E01.mp4'),'cancelled':False}))
        page.locator('#add-file').click();page.wait_for_function('()=>state.selection?.type==="file"');original=page.locator('#import-source-label').inner_text()
        page.route('**/api/batch/browse-folder?kind=season',lambda r:r.fulfill(json={'cancelled':True}))
        page.locator('#add-season').click();page.wait_for_function('()=>!state.selectionBusy');assert page.locator('#import-source-label').inner_text()==original;assert page.locator('#queue-selection').is_enabled()
        page.unroute('**/api/batch/browse-folder?kind=season');page.route('**/api/batch/browse-folder?kind=season',lambda r:r.fulfill(status=500,json={'detail':'Fixture picker error'}));page.locator('#add-season').click();page.wait_for_function('()=>!state.selectionBusy');assert 'Fixture picker error' in page.locator('#import-validation').inner_text();assert page.locator('.review-row').count()==1
        page.locator('#import-clear').click();assert page.locator('#queue-selection').is_disabled();assert page.locator('.review-empty').is_visible()
        page.evaluate('()=>{state.selection={type:"series",path:"fixture",discovery:{episode_count:1,errors:[{message:"Unreadable",path:"fixture"}],seasons:[]}};renderSelection()}');assert page.locator('#queue-selection').is_disabled();assert 'Unreadable' in page.locator('.review-error').inner_text()
        assert page.request.get(url+'/api/batch/current').json()['episodes']==[];browser.close()

def test_review_flags_and_open_settings_have_reserved_space(workflow_server):
    from playwright.sync_api import sync_playwright
    url,source,tmp=workflow_server
    shutil.copyfile(tmp/'sample.mp4',source/'Season 01'/'S01E02_alternate.mp4')
    shutil.copyfile(tmp/'sample.mp4',source/'Season 01'/'unknown-title.mp4')
    with sync_playwright() as pw:
        browser=pw.chromium.launch();page=browser.new_page();page.goto(url)
        page.route('**/api/batch/browse-folder?kind=series',lambda r:r.fulfill(json={'path':str(source),'cancelled':False}))
        page.locator('#add-series').click();page.wait_for_function('()=>state.selection?.discovery?.episode_count===6')
        assert page.locator('.review-badge.warning').count()==2
        assert page.locator('.review-row').filter(has_text='unknown-title').locator('.review-number').inner_text()=='؟'
        page.locator('.review-season').filter(has_text='Season 01').locator('[data-import-season]').uncheck();assert 'حلقتان محددتان' in page.locator('#import-selected-count').inner_text()
        assert page.locator('#import-config').evaluate("el=>el.tagName==='SECTION'")
        for width in (1920,1440,1100,800):
            page.set_viewport_size({'width':width,'height':950})
            config=page.locator('#import-config').bounding_box();action=page.locator('.queue-commit').bounding_box();preview=page.locator('.import-review').bounding_box()
            assert config['y']+config['height']<=page.locator('.source-ribbon').bounding_box()['y']+1
            assert preview['y']+preview['height']<=action['y']+1
            assert page.evaluate('()=>document.documentElement.scrollWidth<=innerWidth')
        assert page.request.get(url+'/api/batch/current').json()['episodes']==[]
        browser.close()


def test_partial_selection_never_silently_changes_series_identity(workflow_server):
    from playwright.sync_api import sync_playwright
    url,source,tmp=workflow_server
    nested=source/'Disc A';nested.mkdir();shutil.copyfile(tmp/'sample.mp4',nested/'S03E01.mp4')
    with sync_playwright() as pw:
        browser=pw.chromium.launch();page=browser.new_page();page.goto(url)
        page.route('**/api/batch/browse-folder?kind=series',lambda r:r.fulfill(json={'path':str(source),'cancelled':False}))
        page.locator('#add-series').click();page.wait_for_function('()=>state.selection?.discovery?.episode_count===5')
        page.locator('#import-select-all').uncheck();page.locator('.review-row').filter(has_text='S03E01.mp4').locator('[data-import-select]').check()
        page.locator('#queue-selection').click();page.wait_for_function('()=>!state.importBusy')
        assert 'للحفاظ على تنظيمه' in page.locator('#toasts').inner_text()
        assert page.request.get(url+'/api/batch/current').json()['episodes']==[]
        assert page.locator('.review-row').count()==5;browser.close()
