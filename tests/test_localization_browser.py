"""Actual localized templates in Chromium, using synthetic offline fixtures."""
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit

import pytest

pytestmark = pytest.mark.skipif(os.getenv('ABOODVIDEO_RUN_BROWSER_TEST') != '1',
                                reason='set ABOODVIDEO_RUN_BROWSER_TEST=1 for Chromium')


@pytest.mark.parametrize('language', ['ar','en'])
@pytest.mark.parametrize('width', [390,1600])
def test_localized_platform_and_studio_layout(language, width, tmp_path):
    root = Path(__file__).resolve().parents[1]
    subprocess.run([sys.executable, str(root / 'scripts/generate_documentation_visuals.py'),
                    '--language', language, '--width', str(width), '--output', str(tmp_path)],
                   cwd=root, check=True, timeout=120)


def test_language_switch_persists_and_runtime_preserves_values():
    from playwright.sync_api import sync_playwright, expect
    from jinja2 import Environment, FileSystemLoader
    from shared.i18n import configure_environment, use_language, WEB_DIR
    root = Path(__file__).resolve().parents[1]
    env = configure_environment(Environment(loader=FileSystemLoader(root/'backend/app/templates')))
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        def route(request_route):
            path = urlsplit(request_route.request.url).path
            if path.startswith('/static/i18n/'):
                target = WEB_DIR / path.rsplit('/',1)[-1]
                request_route.fulfill(status=200,content_type='text/css' if target.suffix=='.css' else 'application/javascript',body=target.read_bytes())
            elif path.startswith('/static/'):
                target = root/'backend/app'/path.lstrip('/')
                request_route.fulfill(status=200 if target.is_file() else 404,
                    content_type='text/css' if target.suffix=='.css' else 'application/javascript',
                    body=target.read_bytes() if target.is_file() else b'')
            else:
                cookie = next((x['value'] for x in context.cookies() if x['name']=='av_language'),'ar')
                selected = parse_qs(urlsplit(request_route.request.url).query).get('lang',[cookie])[0]
                with use_language(selected):
                    html = env.get_template('admin_login.html').render()
                request_route.fulfill(status=200,content_type='text/html',body=html)

        page.route('**/*',route)
        page.goto('http://127.0.0.1:18898/admin/login')
        assert page.locator('html').get_attribute('dir')=='rtl'
        page.locator('[data-language-select]').select_option('en')
        page.wait_for_url('**?lang=en')
        expect(page.locator('html')).to_have_attribute('dir','ltr')
        assert page.evaluate("window.AVi18n.t('إضافة محتوى')")=='Add content'
        result=page.evaluate("""() => {
          const user='إضافة محتوى <img src=x onerror=alert(1)>';
          return {user:AVi18n.tpl`${user}`,label:AVi18n.t('اسم المسلسل'),
            object:AVi18n.t({title:user}),word:AVi18n.deleteWord};
        }""")
        assert result['user']=='إضافة محتوى <img src=x onerror=alert(1)>'
        assert result['object']['title']==result['user']
        assert result['label']=='Series name' and result['word']=='DELETE'
        page.goto('http://127.0.0.1:18898/admin/login')
        expect(page.locator('html')).to_have_attribute('lang','en')
        page.locator('[data-language-select]').select_option('ar')
        page.wait_for_url('**?lang=ar')
        expect(page.locator('html')).to_have_attribute('dir','rtl')
        assert page.evaluate('AVi18n.deleteWord')=='حذف'
        browser.close()
