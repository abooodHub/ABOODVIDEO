"""Opt-in full-length owner media, installed binary and NAT abuse boundaries.

Run only through run_real_integration with ABV_FULL_LENGTH_ROOT pointing to a
new evidence directory containing real-originals.json. No production/profile reads.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

import httpx
import psutil
import pytest
from sqlalchemy import select

from real_platform_support import RealPlatform, docker, assert_owned
from test_staging_readiness import proxy, login

pytestmark=pytest.mark.skipif(not os.getenv('ABV_FULL_LENGTH_ROOT') or os.getenv('ABV_RUN_REAL_INTEGRATION')!='1',reason='Requires explicit full-length real-media fixture and disposable stack')


def write(p,key,value):
    with p.lock:
        p.measurements.setdefault(key,[]).append(value)
        p.evidence.write_text(json.dumps(p.measurements,indent=2),encoding='utf-8')


@pytest.fixture(scope='module')
def staging():
    from app.models.setting import SystemSetting
    from app.services.quality_settings import SETTING_KEY
    from scripts.capture_gpu_ownership import capture_gpu_ownership
    p=RealPlatform();p.lock=threading.RLock();p.measurements={}
    p.root=Path(os.environ['ABV_FULL_LENGTH_ROOT']);p.evidence=p.root/('full-measurements-'+os.environ['ABV_INTEGRATION_RUN_ID']+'.json')
    p.originals=json.loads((p.root/'real-originals.json').read_text());p.stop=threading.Event();p.installed=None
    p.sampling=[]
    def sample():
        while not p.stop.is_set():
            try:
                owned=psutil.Process().children(recursive=True)
                procs=[]
                for child in owned:
                    try:
                        io=child.io_counters();procs.append({'pid':child.pid,'name':child.name(),'rss':child.memory_info().rss,'cpu_seconds':sum(child.cpu_times()[:2]),'read_bytes':io.read_bytes,'write_bytes':io.write_bytes})
                    except psutil.Error:pass
                row={'at':time.time(),'gpu':capture_gpu_ownership(os.getpid()),'host_cpu_pct':psutil.cpu_percent(),
                    'host_available_ram':psutil.virtual_memory().available,'owned_processes':procs,
                    'containers':[json.loads(r) for r in docker('stats','--no-stream','--format','{{json .}}',p.api_name,*p.workers,'abv-f16-pg','abv-f16-redis').splitlines()]}
                p.sampling.append(row)
                (p.root/'resource-samples.json').write_text(json.dumps(p.sampling),encoding='utf-8')
            except (RuntimeError,psutil.Error):pass
            p.stop.wait(15)
    try:
        p.start()
        p.env_file.write_text(p.env_file.read_text().replace('FFMPEG_PRESET=ultrafast','FFMPEG_PRESET=medium').replace('FFMPEG_THREADS=1','FFMPEG_THREADS=2'))
        for worker in p.workers:p.restart_worker(worker);docker('update','--cpus','4','--memory','3072m','--memory-swap','3072m',worker)
        with p.session() as db:db.get(SystemSetting,SETTING_KEY).value='360p';db.commit()
        write(p,'gpu_before',capture_gpu_ownership(os.getpid()))
        thread=threading.Thread(target=sample,daemon=True);thread.start()
        yield p
    finally:
        p.stop.set()
        if 'thread' in locals():thread.join(35)
        if p.installed and p.installed.poll() is None:
            # This is only the child launched with this disposable user profile.
            parent=psutil.Process(p.installed.pid)
            for child in parent.children(recursive=True):
                try:child.terminate()
                except psutil.Error:pass
            p.installed.terminate();p.installed.wait(30)
        write(p,'gpu_after',capture_gpu_ownership(os.getpid()))
        for item in p.originals:
            stat=Path(item['path']).stat();assert stat.st_size==item['bytes'] and stat.st_mtime_ns==item['mtime_ns']
        p.close()


def test_complete_installed_studio_and_concurrent_full_source_processing(staging,proxy):
    from studio.core.server_processing import process_on_server
    from app.models.video import Video,VideoRendition
    p=staging
    owner_profile=Path(os.environ['ABV_INSTALLED_STUDIO_EXE']).resolve(strict=True)
    original=next(i for i in p.originals if i['id']=='h264-ass')
    second=next(i for i in p.originals if i['id']=='hevc-mp4')
    assert 1200<=float(original['duration'])<=3600 and 1200<=float(second['duration'])<=3600
    profile=p.root/('installed-profile-'+os.environ['ABV_INTEGRATION_RUN_ID']);appdata=profile/'ABOODVIDEO Studio';appdata.mkdir(parents=True)
    (appdata/'config.json').write_text('{}') # Never import a legacy user config.
    output=p.root/('installed-output-'+os.environ['ABV_INTEGRATION_RUN_ID']);output.mkdir();fonts=p.root/('empty-fonts-'+os.environ['ABV_INTEGRATION_RUN_ID']);fonts.mkdir()
    env=dict(os.environ,LOCALAPPDATA=str(profile),APPDATA=str(profile/'roaming'),USERPROFILE=str(profile/'home'),HOME=str(profile/'home'))
    Path(env['USERPROFILE']).mkdir();Path(env['APPDATA']).mkdir()
    env['PATH']=r'C:\tools\bin'+os.pathsep+env['PATH']
    log=(p.root/'installed-process.log').open('w',encoding='utf-8')
    p.installed=subprocess.Popen([str(owner_profile),'--port','28557'],env=env,cwd=p.root,stdout=log,stderr=subprocess.STDOUT)
    p.studio_base='http://127.0.0.1:28557'
    # Polling sleeps five seconds: expire this test client's idle connection
    # before Uvicorn's five-second keep-alive boundary to avoid a probe race.
    with httpx.Client(base_url=p.studio_base,timeout=120,headers={'Origin':p.studio_base},limits=httpx.Limits(keepalive_expiry=2)) as c:
        for _ in range(120):
            try:
                cfg=c.get('/api/config')
                if cfg.status_code==200:break
            except httpx.HTTPError:pass
            time.sleep(.5)
        else:pytest.fail('Installed Studio did not start in disposable profile')
        assert str(profile).casefold() in cfg.json()['appdata_root'].casefold()
        config={'server_url':p.base+'/api/v1','api_key':p.identities['OWNER']['key'],'processing_mode':'local','default_encoder':'gpu',
                'default_preset':'balanced','converted_output_folder':str(output),'font_library_folder':str(fonts),'include_360p':True,
                'cpu_concurrent_jobs':1,'gpu_concurrent_jobs':1,'auto_cleanup_package':False}
        assert c.post('/api/config',json=config).status_code==200
        assert c.get('/api/server/status').status_code==200
        sha=hashlib.sha256(owner_profile.read_bytes()).hexdigest()
        write(p,'installed',{'exe':str(owner_profile),'sha256':sha,'pid':p.installed.pid,'isolated_profile_confirmed':True,'base':p.studio_base})
        # Expose no API key in evidence. The live desktop window is the installed binary.
        (p.root/'installed-ready.txt').write_text(str(p.installed.pid))
        source_probe=json.loads(subprocess.check_output([r'C:\tools\bin\ffprobe.exe','-v','error','-show_streams','-of','json',original['path']],text=True))
        subtitle=next(s for s in source_probe['streams'] if s.get('codec_name')=='ass')
        start=time.monotonic()
        job=c.post('/api/jobs/start',json={'video_path':original['path'],'title':'Full real 39-minute installed Studio episode',
            'selected_qualities':['360p','720p','2160p'],'encoder_choice':'gpu','subtitle_mode':'softsub',
            'embedded_subtitle_index':subtitle['index'],'auto_upload':True})
        assert job.status_code==200,job.text;job_id=job.json()['job_id']
        def server(index):
            saved={};states=[];started=time.monotonic()
            vid=process_on_server(Path(second['path']),p.base,p.identities['OWNER']['key'],None,
                {'title':f'Full 22-minute concurrent source {index}'},saved,lambda stage,pct:states.append((stage,round(pct,1))),threading.Event(),saved.update,poll_seconds=14400)
            return {'index':index,'video_id':vid,'elapsed_seconds':round(time.monotonic()-started,3),'states':sorted(set(s for s,_ in states))}
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(server,index) for index in range(2)]
            deadline=time.monotonic()+14400;history=[]
            while time.monotonic()<deadline:
                state=c.get(f'/api/jobs/{job_id}/status').json()
                history.append({'elapsed':round(time.monotonic()-start,1),'status':state.get('status'),'progress':state.get('progress_pct'),'rendition':state.get('current_rendition'),'speed':state.get('speed')})
                (p.root/'installed-progress.json').write_text(json.dumps(history,indent=2))
                if state.get('status') in {'completed','failed','cancelled'}:break
                time.sleep(5)
            assert state.get('status')=='completed',state
            assert state['result']['success'],state
            p.real_video=state['result']['video_id']
            write(p,'installed_completion',{'seconds':round(time.monotonic()-start,3),'video_id':p.real_video,'duration':float(original['duration']),'state':'completed','history_file':'installed-progress.json'})
            for future in futures:write(p,'full_source',future.result(timeout=14400))
        with p.session() as db:
            for row in p.measurements['full_source']:
                video=db.get(Video,uuid.UUID(row['video_id']));assert video.status.value=='READY'
                assert abs(video.source_duration_seconds-float(second['duration']))<2
        # Upload a genuine full SRT through the real authenticated API. It is a
        # timing-compatibility fixture, not a matching translation of this episode.
        prior=Path(os.environ['ABV_STAGING_MEDIA_MANIFEST']);srt=Path(json.loads(prior.read_text())['srt_original'])
        with login(p,proxy) as viewer,srt.open('rb') as f:
            response=viewer.post(f'/api/v1/admin/videos/{p.real_video}/subtitles',data={'language_code':'en','language_name':'SRT compatibility','label':'Full real SRT','is_default':'false'},files={'file':(srt.name,f,'application/x-subrip')})
            assert response.status_code==201,response.text
        write(p,'full_media_ready',{'video_id':p.real_video,'all_uploads_ready':True,'srt_uploaded':True})


def media_url(p,base,client):
    page=client.get('/watch/'+p.real_video);assert page.status_code==200
    token=re.search(r'/api/v1/playback/(s/[^/"\s]+)',page.text).group(1)
    master=client.get('/api/v1/playback/'+token+'/master.m3u8');assert master.status_code==200
    variant=next(x for x in master.text.splitlines() if x and not x.startswith('#'))
    playlist=client.get(urljoin(str(master.request.url),variant));assert playlist.status_code==200
    segment=next(x for x in playlist.text.splitlines() if x and not x.startswith('#'))
    return urljoin(str(playlist.request.url),segment)


def test_nat_viewers_and_single_session_abuse(staging,proxy):
    p=staging;assert hasattr(p,'real_video')
    with login(p,proxy) as authenticated:
        clients=[];urls=[]
        for _ in range(24):
            c=httpx.Client(base_url=proxy,cookies=authenticated.cookies,timeout=30)
            for cookie in list(c.cookies.jar):
                if cookie.name=='abood_playback':c.cookies.jar.clear(cookie.domain,cookie.path,cookie.name)
            urls.append(media_url(p,proxy,c));clients.append(c)
        time.sleep(3)
        def legitimate(i):
            rows=[]
            for _ in range(12):
                start=time.monotonic();r=clients[i].get(urls[i]);rows.append((r.status_code,time.monotonic()-start));time.sleep(.25)
            clients[i].close();return rows
        with ThreadPoolExecutor(max_workers=24) as pool:rows=[r for group in pool.map(legitimate,range(24)) for r in group]
        write(p,'nat_legitimate',{'sessions':24,'requests':len(rows),'statuses':{str(code):sum(s==code for s,_ in rows) for code,_ in rows},'p95_ms':round(sorted(t*1000 for _,t in rows)[int(.95*len(rows))],3)})
        assert all(code==200 for code,_ in rows)
        time.sleep(4)
        url=media_url(p,proxy,authenticated)
        def abuse(_):return authenticated.get(url).status_code
        with ThreadPoolExecutor(max_workers=40) as pool:codes=list(pool.map(abuse,range(180)))
        write(p,'single_session_abuse',{'requests':len(codes),'statuses':{str(code):codes.count(code) for code in set(codes)}})
        assert 429 in codes and 503 not in codes and all(code in {200,429} for code in codes)
        time.sleep(5)
        # Rotating fabricated session URLs must remain bounded by the aggregate
        # IP ceiling, and no fabricated session may receive a playable response.
        with httpx.Client(base_url=proxy,timeout=30,limits=httpx.Limits(max_connections=100,max_keepalive_connections=100)) as anonymous:
            def forged(i):return anonymous.get(f'/api/v1/playback/s/{uuid.uuid4().hex}/master.m3u8',headers={'X-Forwarded-For':f'198.51.100.{i%250+1}'}).status_code
            with ThreadPoolExecutor(max_workers=80) as pool:codes=list(pool.map(forged,range(800)))
        write(p,'forged_session_abuse',{'requests':len(codes),'statuses':{str(code):codes.count(code) for code in set(codes)}})
        assert 200 not in codes and 429 in codes and 503 not in codes
        time.sleep(5)


def test_full_duration_browser_subtitle_sync_resume_and_cleanup(staging,proxy):
    from playwright.sync_api import sync_playwright
    sys.path.insert(0,str(staging.root/'test-dependencies'))
    import pysubs2
    p=staging;assert hasattr(p,'real_video')
    duration=float(next(i for i in p.originals if i['id']=='h264-ass')['duration'])
    prior=Path(os.environ['ABV_STAGING_MEDIA_MANIFEST']);srt=Path(json.loads(prior.read_text())['srt_original'])
    srt_events=pysubs2.load(str(srt));samples=[]
    original=next(i for i in p.originals if i['id']=='h264-ass')
    ass_file=p.root/('original-ass-'+os.environ['ABV_INTEGRATION_RUN_ID']+'.ass')
    if not ass_file.exists():
        subprocess.run([r'C:\tools\bin\ffmpeg.exe','-v','error','-i',original['path'],'-map','0:s:0','-c','copy',str(ass_file)],capture_output=True,check=True,timeout=120)
    ass_events=pysubs2.load(str(ass_file))
    with login(p,proxy) as c,sync_playwright() as pw:
        # An unattended staging decoder must not compete for Windows audio
        # focus with the owner's foreground media applications.
        browser=pw.chromium.launch(headless=True,args=['--disable-features=AudioFocusEnforcement,HardwareMediaKeyHandling']);context=browser.new_context()
        context.add_cookies([{'name':cookie.name,'value':cookie.value,'url':proxy} for cookie in c.cookies.jar])
        page=context.new_page();errors=[];failed_resources=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.on('response',lambda r:failed_resources.append({'status':r.status,'asset':r.url.split('/')[-1].split('?')[0]}) if '/api/v1/playback/' in r.url and r.status>=400 else None)
        assert page.goto(proxy+'/watch/'+p.real_video,wait_until='networkidle').status==200
        page.locator('#big-play-btn').click();page.wait_for_function("() => document.querySelector('video').currentTime > .2",timeout=60000)
        page.wait_for_function('d => Math.abs(document.querySelector("video").duration-d)<3',arg=duration)
        metadata=c.get(f'/api/v1/admin/videos/{p.real_video}/subtitles').json()
        ass_sub=next(s for s in metadata if s.get('format') in {'ass','ssa'})
        session=re.search(r'/api/v1/playback/(s/[^/"\s]+)',c.get('/watch/'+p.real_video).text).group(1)
        delivered=c.get(f'/api/v1/playback/{session}/subtitles/{ass_sub["id"]}/file')
        assert delivered.status_code==200
        delivered_events=pysubs2.SSAFile.from_string(delivered.text,format_='ass')
        assert [(e.start,e.end,e.text) for e in delivered_events]==[(e.start,e.end,e.text) for e in ass_events]
        write(p,'ass_dialogue_integrity',{'events':len(ass_events),'all_original_dialogue_times_and_text_preserved':True})
        options=page.locator('#sub-opt-off').locator('..').inner_text();write(p,'subtitle_menu',options)
        # Find the genuine SRT by label, not a hard-coded ordering.
        page.locator('#sub-opt-off').locator('..').locator('.menu-item').evaluate_all("nodes => nodes.find(n=>n.textContent.includes('Full real SRT')).click()")
        for target in [60,600,1200,1800,min(duration-30,max(e.end for e in srt_events)/1000-60)]:
            cue=next(e for e in srt_events if e.start/1000>=target and e.end/1000<duration-1)
            at=(cue.start+cue.end)/2000
            page.evaluate('at => {let v=document.querySelector("video");v.pause();v.currentTime=at}',at)
            expected=pysubs2.SSAEvent(text=cue.text).plaintext
            page.wait_for_function("expected => Array.from(document.querySelector('video').textTracks).some(t=>t.mode==='showing' && Array.from(t.activeCues||[]).some(c=>c.text.replace(/<[^>]+>/g,'').trim()===expected.trim()))",arg=expected,timeout=30000)
            samples.append({'srt_original_start':cue.start/1000,'video_time':page.evaluate("document.querySelector('video').currentTime"),'cue_text_matches_original':True})
        # Actual embedded ASS at widely separated timestamps, using its full
        # preserved dialogue timings (not the separate SRT translation).
        page.locator('#sub-opt-0').evaluate('n=>n.click()')
        page.wait_for_function('() => Boolean(window.jassubInstance)',timeout=30000)
        page.evaluate('async()=>await window.jassubInstance._loaded')
        ass_samples=[]
        for target in [60,600,1200,1800,duration-120]:
            candidates=[e for e in ass_events if not e.is_comment and e.start/1000>=target and e.end/1000<duration-1 and re.search('[\u0600-\u06ff]',e.plaintext)]
            assert candidates,'No real Arabic dialogue at requested long-duration checkpoint'
            cue=candidates[0];at=(cue.start+cue.end)/2000
            page.evaluate('at => {let v=document.querySelector("video");v.currentTime=at}',at)
            page.wait_for_function('at=>Math.abs(document.querySelector("video").currentTime-at)<1 && document.querySelector("video").readyState>=2',arg=at,timeout=30000)
            page.wait_for_timeout(1000)
            page.wait_for_function("""() => {const s=document.querySelector('#ass-canvas');if(!s.width||!s.height)return false;
                const c=document.createElement('canvas');c.width=s.width;c.height=s.height;const x=c.getContext('2d');x.drawImage(s,0,0);
                const d=x.getImageData(0,0,c.width,c.height).data;let colored=0;for(let i=0;i<d.length;i+=4){if(d[i+3]>0 && Math.max(d[i],d[i+1],d[i+2])>40)colored++}return colored>100}""",timeout=15000)
            page.locator('#player-container').screenshot(path=str(p.root/f'ass-full-{int(at)}.png'))
            ass_samples.append({'original_start':cue.start/1000,'video_time':at,'non_mask_text_pixels_rendered':True})
        write(p,'ass_sync_samples',ass_samples)
        # Real-time uninterrupted full-episode decode and HLS delivery. Record
        # media time versus wall time; no fake clock or accelerated playback.
        available_heights=page.evaluate('() => window.hls.levels.map(l=>l.height)')
        write(p,'full_playback_available_heights',available_heights)
        page.locator('#quality-menu .menu-item:not(#qual-opt-auto)').evaluate_all("(nodes,height)=>nodes.find(n=>n.textContent.includes(String(height))).click()",min(available_heights))
        page.evaluate('''() => {let v=document.querySelector("video");window.stabilityEvents=[];
            for(const name of ['play','pause','waiting','stalled','ended','error'])
                v.addEventListener(name,()=>window.stabilityEvents.push({name,time:v.currentTime,at:Date.now(),hidden:document.hidden}));
            v.muted=true;v.currentTime=0;v.playbackRate=1;return v.play()}''')
        started=time.monotonic();timeline=[];deadline=started+duration+600
        while time.monotonic()<deadline:
            page.wait_for_timeout(20000)
            state=page.evaluate("() => {const v=document.querySelector('video');return {time:v.currentTime,ended:v.ended,paused:v.paused,error:v.error?.code,ready:v.readyState,dropped:v.getVideoPlaybackQuality().droppedVideoFrames,total:v.getVideoPlaybackQuality().totalVideoFrames}}")
            state['wall_seconds']=round(time.monotonic()-started,3);timeline.append(state)
            state['playback_cookie_expiry']=[cookie['expires'] for cookie in context.cookies() if cookie['name']=='abood_playback']
            state['failed_playback_resources']=failed_resources[-8:]
            state['media_events']=page.evaluate('window.stabilityEvents.slice(-20)')
            (p.root/'full-playback-timeline.json').write_text(json.dumps(timeline,indent=2))
            assert not state['error'],state
            if len(timeline)>=5 and not state['ended']:
                assert state['time']>timeline[-5]['time']+5,'Playback stalled for over 80 seconds: '+str(state)
            if state['ended']:break
        assert timeline[-1]['ended'] and timeline[-1]['time']>=duration-3,timeline[-1]
        assert not errors,errors
        write(p,'full_playback',{'duration':duration,'wall_seconds':round(time.monotonic()-started,3),'ended':True,'srt_cue_samples':samples,'ass_screenshots':5,'browser':browser.version,'rate':1})
        # Persist and reload a mid-episode resume point.
        page.evaluate('() => {let v=document.querySelector("video");v.currentTime=1200;v.play()}');page.wait_for_timeout(16000)
        page.evaluate('() => document.querySelector("video").pause()')
        page.wait_for_function("() => Object.keys(localStorage).some(k=>k.startsWith('aboodvideo_resume_') && JSON.parse(localStorage[k]||'{}').time>=1200)")
        page.reload(wait_until='networkidle');page.wait_for_timeout(2000)
        page.locator('.toast-action-btn').wait_for(state='visible',timeout=15000)
        page.locator('.toast-action-btn').click()
        page.wait_for_function("() => document.querySelector('video').currentTime > 1190 && document.querySelector('video').currentTime < 1250",timeout=30000)
        write(p,'resume',{'reloaded_time':page.evaluate("document.querySelector('video').currentTime")})
        browser.close()
        # Other installed browser engines: actual launch/play/seek results only.
        for name,engine,options in [('firefox',pw.firefox,{'executable_path':r'C:\Users\abood\AppData\Local\ms-playwright\firefox-1543\firefox\firefox.exe'}),('edge',pw.chromium,{'channel':'msedge'})]:
            try:b=engine.launch(headless=True,**options)
            except Exception as exc:
                write(p,'other_browsers',{'name':name,'result':'UNAVAILABLE','reason':str(exc)[:300]});continue
            try:
                ctx=b.new_context();ctx.add_cookies([{'name':cookie.name,'value':cookie.value,'url':proxy} for cookie in c.cookies.jar]);tab=ctx.new_page()
                assert tab.goto(proxy+'/watch/'+p.real_video,wait_until='networkidle').status==200
                tab.locator('#big-play-btn').click();tab.wait_for_function("() => document.querySelector('video').currentTime>.2",timeout=60000)
                tab.evaluate('() => document.querySelector("video").currentTime=1200');tab.wait_for_function("() => document.querySelector('video').currentTime>=1200 && document.querySelector('video').readyState>=2",timeout=30000)
                write(p,'other_browsers',{'name':name,'result':'PASS','version':b.version,'seek':1200})
            finally:b.close()
        # Metadata trash/restore and protected playback are exercised on this
        # full-length video; actual final removal is the owned disposable volume.
        assert c.post('/api/v1/drive/trash',json={'items':[{'type':'video','id':p.real_video}]}).status_code==200
        assert c.post('/api/v1/drive/restore',json={'items':[{'type':'video','id':p.real_video}]}).status_code==200
        assert c.get('/watch/'+p.real_video).status_code==200
        write(p,'cleanup',{'trash_restore_full_episode':True,'original_media_preserved':True,'final_media_scope':'owned disposable Docker volume only'})
