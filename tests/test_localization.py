"""Arabic/English UI contracts, with media and user values kept intact."""
import asyncio
import json
from pathlib import Path
import re

import httpx
from jinja2 import Environment, FileSystemLoader, select_autoescape
import pytest
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from shared.i18n import (ROOT, WEB_DIR, TemplateLiterals, catalog, configure_environment,
                         direction, gettext, language, use_language)
from shared.i18n.middleware import LocaleMiddleware

PROJECT = Path(__file__).resolve().parents[1]


def test_catalog_matches_browser_catalog_and_preserves_placeholders():
    values = catalog()
    assert len(values) > 2200
    browser = (WEB_DIR / 'catalog.js').read_text(encoding='utf-8')
    assert json.loads(browser.split('=', 1)[1].strip().rstrip(';')) == values
    for key, value in values.items():
        assert value.strip(), key
        assert set(re.findall(r'\{\d+\}', key)) == set(re.findall(r'\{\d+\}', value)), key


def test_dynamic_system_labels_preserve_values():
    with use_language('en'):
        assert gettext('2/5 مفقود') == '2/5 missing'
        assert gettext('خطوط مفقودة (2): إضافة محتوى.ttf') == 'Missing fonts (2): إضافة محتوى.ttf'


def test_all_template_literals_have_english_translations():
    files = list((PROJECT / 'backend/app/templates').glob('*.html'))
    files.append(PROJECT / 'studio/web/templates/index_v3.html')
    missing = {message for path in files
               for message in TemplateLiterals(path.read_text(encoding='utf-8-sig').replace('\r\n', '\n')).messages
               if message not in catalog()}
    assert not missing


def test_template_translation_never_translates_or_unescapes_user_content(tmp_path):
    source = '''<html lang="ar" dir="rtl"><head></head><body>
      <h1>إضافة محتوى</h1><input placeholder="اسم المسلسل">
      <div>{{ title }}</div><code>إضافة محتوى</code>
      <p>{{ missing |default('اسم المسلسل') }}</p></body></html>'''
    (tmp_path / 'test.html').write_text(source, encoding='utf-8')
    env = configure_environment(Environment(loader=FileSystemLoader(tmp_path), autoescape=select_autoescape()))
    template = env.get_template('test.html')
    with use_language('en'):
        result = template.render(title='إضافة محتوى <script>alert(1)</script>')
        assert '<h1>Add content</h1>' in result
        assert 'lang="en" dir="ltr"' in result
        assert '<div>إضافة محتوى &lt;script&gt;alert(1)&lt;/script&gt;</div>' in result
        assert '<code>إضافة محتوى</code>' in result
        assert '<p>Series name</p>' in result
    result = template.render(title='اسم المسلسل')
    assert 'lang="ar" dir="rtl"' in result
    assert '<h1>إضافة محتوى</h1>' in result


@pytest.mark.asyncio
async def test_request_language_is_isolated_and_preference_is_allowlisted():
    async def view(request):
        await asyncio.sleep(0.005)
        return HTMLResponse(f'{language()}:{direction()}:{gettext("إضافة محتوى")}')
    app = LocaleMiddleware(Starlette(routes=[Route('/', view)]), secure_cookie=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='https://test') as client:
        ar, en = await asyncio.gather(client.get('/?lang=ar'), client.get('/?lang=en'))
        assert ar.text == 'ar:rtl:إضافة محتوى'
        assert en.text == 'en:ltr:Add content'
        assert en.headers['content-language'] == 'en'
        assert 'Cookie' in en.headers['vary']
        assert 'Secure' in en.headers['set-cookie']
        client.cookies.clear()
        await client.get('/?lang=en')
        assert (await client.get('/')).text.startswith('en:ltr:')
        assert (await client.get('/?lang=invalid')).text.startswith('en:ltr:')
        assert 'set-cookie' not in (await client.get('/?lang=invalid')).headers
    assert language() == 'ar'


@pytest.mark.asyncio
async def test_error_translation_preserves_user_fields_and_http_contract():
    async def error(request):
        return JSONResponse({'message':'اسم المسلسل', 'title':'اسم المسلسل',
                             'error':{'message':'إضافة محتوى','code':'KEEP'},
                             'items':[{'title':'إضافة محتوى'}]}, status_code=403)
    app = LocaleMiddleware(Starlette(routes=[Route('/', error)]))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        result = await client.get('/?lang=en')
        assert result.status_code == 403
        assert int(result.headers['content-length']) == len(result.content)
        assert result.json() == {'message':'Series name','title':'اسم المسلسل',
                                 'error':{'message':'Add content','code':'KEEP'},
                                 'items':[{'title':'إضافة محتوى'}]}


def test_studio_language_validation_and_assets():
    from studio.web.routes.api import ConfigUpdateRequest
    from pydantic import ValidationError
    assert ConfigUpdateRequest(language='en').language == 'en'
    with pytest.raises(ValidationError):
        ConfigUpdateRequest(language='fr')
    from fastapi.testclient import TestClient
    from studio.web.app import app
    with TestClient(app, base_url='http://127.0.0.1') as client:
        result = client.get('/?lang=en')
        assert 'lang="en" dir="ltr"' in result.text
        assert 'Local library' in result.text
        assert 'Content-Security-Policy' in result.headers
        assert 'lang="en"' in client.get('/').text
        for asset in ('runtime.js','catalog.js','direction.css'):
            assert client.get('/static/i18n/' + asset).status_code == 200


def test_studio_persists_language_for_a_new_browser_session():
    from fastapi.testclient import TestClient
    from studio.web.app import app
    from studio.config import load_studio_config, save_studio_config
    before = load_studio_config()
    try:
        with TestClient(app, base_url='http://127.0.0.1') as client:
            result = client.post('/api/config', json={'language':'en'},
                                 headers={'Origin':'http://127.0.0.1'})
            assert result.status_code == 200
            client.cookies.clear()
            assert 'lang="en" dir="ltr"' in client.get('/').text
            assert client.post('/api/config',json={'language':'invalid'},
                               headers={'Origin':'http://127.0.0.1'}).status_code == 422
            after = load_studio_config()
            assert all(after[key] == value for key,value in before.items() if key != 'language')
    finally:
        save_studio_config({'language':before.get('language','ar')})


@pytest.mark.parametrize('locale', ['ar','en'])
def test_hidden_public_pages_and_admin_guards_are_unchanged(locale):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app, base_url='http://localhost') as client:
        for path in ('/', '/watch/', '/browse', '/browse?type=series'):
            response = client.get(path, cookies={'av_language':locale}, follow_redirects=False)
            assert response.status_code == 404, path
            assert response.content == b'', path
            assert 'location' not in response.headers
        for path in ('/admin', '/admin/upload'):
            response = client.get(path + '?lang=' + locale, follow_redirects=False)
            assert response.status_code == 302
            assert response.headers['location'] == '/admin/login'
        login = client.get('/admin/login?lang=' + locale)
        assert f'lang="{locale}"' in login.text
        assert 'data-language-select' in login.text
