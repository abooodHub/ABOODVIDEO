"""Opt-in deployed-host authentication browser acceptance."""
import os
import secrets
import uuid

import pytest


@pytest.mark.skipif(os.getenv("ABOODVIDEO_RUN_LIVE_BROWSER_TEST") != "1", reason="acceptance host only")
def test_live_login_and_logout_with_disposable_admin() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    from sqlalchemy import select
    from app.core.database import SyncSessionLocal
    from app.models.session import UserSession
    from app.models.user import User, UserRole
    from app.security.hashing import hash_password

    username = f"phase6_browser_{uuid.uuid4().hex[:12]}"
    password = secrets.token_urlsafe(32)
    user_id = uuid.uuid4()
    with SyncSessionLocal() as db:
        db.add(User(id=user_id, email=f"{username}@acceptance.invalid", username=username, hashed_password=hash_password(password), role=UserRole.ADMIN, is_active=True))
        db.commit()
    responses, page_errors, csp_errors = [], [], []
    try:
        with sync_api.sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.on("console", lambda message: csp_errors.append(message.text) if "content security policy" in message.text.lower() else None)
            page.on("response", lambda response: responses.append(response.status) if response.url.endswith("/api/v1/auth/login") else None)
            page.goto("http://127.0.0.1/admin/login", wait_until="networkidle")
            page.locator("#username").fill(username)
            page.locator("#password").fill("incorrect-password")
            page.locator("#submit-btn").click()
            sync_api.expect(page.locator("#login-alert")).to_be_visible()
            assert responses[-1] == 401
            assert page.locator("#submit-btn").is_enabled()
            page.locator("#password").fill(password)
            page.locator("#submit-btn").click()
            page.wait_for_url("**/admin")
            assert responses[-1] == 200
            page.locator("#logout-btn").click()
            page.wait_for_url("**/admin/login")
            assert not page_errors
            assert not csp_errors
            browser.close()
        with SyncSessionLocal() as db:
            assert db.execute(select(UserSession).where(UserSession.user_id == user_id)).scalar_one_or_none() is None
    finally:
        with SyncSessionLocal() as db:
            user = db.get(User, user_id)
            if user:
                db.delete(user)
                db.commit()
