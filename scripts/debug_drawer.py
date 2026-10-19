from playwright.sync_api import sync_playwright
import json, subprocess

auth_code = """
import json, secrets, uuid
from datetime import datetime, timedelta, timezone
from app.core.database import SyncSessionLocal
from app.models.user import User, UserRole
from app.models.session import UserSession
from app.security.tokens import hash_token
from app.security.csrf import generate_csrf_token

with SyncSessionLocal() as db:
    admin = db.query(User).filter(User.username == 'admin_live').first()
    token = secrets.token_urlsafe(32)
    csrf = generate_csrf_token()
    db.add(UserSession(
        id=uuid.uuid4(), user_id=admin.id, token_hash=hash_token(token),
        ip_address='127.0.0.1', user_agent='debug',
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=60),
    ))
    db.commit()
    print(json.dumps({'session': token, 'csrf': csrf}))
"""
res = subprocess.run(['ssh', '-o', 'BatchMode=yes', 'abood@127.0.0.1', 'docker exec -i -e PYTHONPATH=/app aboodvideo_api python'], input=auth_code, text=True, capture_output=True, check=True)
auth = json.loads(res.stdout.strip())

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context()
    ctx.add_cookies([
        {'name': 'aboodvideo_session', 'value': auth['session'], 'domain': '127.0.0.1', 'path': '/'},
        {'name': 'aboodvideo_csrf', 'value': auth['csrf'], 'domain': '127.0.0.1', 'path': '/'}
    ])
    page = ctx.new_page()
    page.goto('http://127.0.0.1/preview', wait_until='networkidle')
    target = page.locator("tr.drive-row[data-id='2c36137a-7066-4615-a61e-7b8d9a1dccc1']")
    target.locator('.col-size').click()
    page.wait_for_timeout(600)
    info = page.evaluate("""() => {
        const inp = document.getElementById('drawerWatchUrlInput');
        const box = document.getElementById('drawerWatchLinkBox');
        const tr = document.querySelector("tr.drive-row[data-id='2c36137a-7066-4615-a61e-7b8d9a1dccc1']");
        return {
            inputExists: !!inp,
            inputValue: inp ? inp.value : null,
            boxClass: box ? box.className : null,
            trDataset: tr ? Object.assign({}, tr.dataset) : null,
        };
    }""")
    print('DEBUG RESULT:', json.dumps(info, indent=2))
