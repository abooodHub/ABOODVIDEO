"""One-shot, guarded live UI acceptance for the private Drive Trash workflow.

Run explicitly with --execute. Only the known historical test-title set is
eligible; no permanent-delete control is used. Screenshots are local artifacts.
"""
import argparse
import json
import subprocess
from collections import Counter
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_URL = "http://127.0.0.1"
ARTIFACTS = Path(__file__).resolve().parent.parent / "browser-artifacts" / "product_reset_2"
SAFE_TITLES = {
    "Cancel Test", "Checksum Test", "CSRF Test Valid", "Duplicate Chunk Test",
    "Expired Video", "Fake Video Test", "Missing Chunk Test", "My Awesome Video",
    "Pause Resume Test", "Phase 4 Secure Test Video", "Security Auth Test",
    "Sequential Chunk Test", "Stale Upload", "Studio Package Test Video",
    "Test Source Video", "sample ep01", "sample ep02", "sample ep03",
    "sample medium 1", "sample medium 2",
}


def create_admin_session() -> dict:
    """Mint a short-lived session for an existing active ADMIN; never change a role."""
    code = """
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from app.core.database import SyncSessionLocal
from app.models.user import User, UserRole
from app.models.session import UserSession
from app.security.tokens import hash_token
from app.security.csrf import generate_csrf_token

with SyncSessionLocal() as db:
    admin = (db.query(User)
             .filter(User.role == UserRole.ADMIN, User.is_active.is_(True))
             .order_by(User.created_at.asc()).first())
    if admin is None:
        raise RuntimeError("No active ADMIN exists")
    token = secrets.token_urlsafe(32)
    csrf = generate_csrf_token()
    db.add(UserSession(
        id=uuid.uuid4(), user_id=admin.id, token_hash=hash_token(token),
        ip_address="127.0.0.1", user_agent="drive-live-acceptance",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
    ))
    db.commit()
    print(json.dumps({"session": token, "csrf": csrf}))
"""
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "abood@127.0.0.1", "docker exec -i aboodvideo_api python"],
        input=code, text=True, capture_output=True, check=False,
    )
    if result.returncode:
        raise RuntimeError("Could not create a temporary ADMIN acceptance session")
    for line in result.stdout.splitlines():
        if line.startswith("{") and '"session"' in line:
            return json.loads(line)
    raise RuntimeError("Temporary ADMIN acceptance session was not returned")


def revoke_test_session(token: str) -> None:
    """Remove only the temporary session created by this acceptance run."""
    code = (
        "from app.core.database import SyncSessionLocal\n"
        "from app.models.session import UserSession\n"
        "from app.security.tokens import hash_token\n"
        f"token_hash = hash_token({token!r})\n"
        "with SyncSessionLocal() as db:\n"
        "    row = db.query(UserSession).filter(UserSession.token_hash == token_hash).first()\n"
        "    if row is not None:\n"
        "        db.delete(row)\n"
        "        db.commit()\n"
    )
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "abood@127.0.0.1", "docker exec -i aboodvideo_api python"],
        input=code, text=True, capture_output=True, check=False,
    )
    if result.returncode:
        raise RuntimeError("Could not revoke the temporary live acceptance session")


def visible_ids(page) -> set[str]:
    return set(page.locator(".drive-row").evaluate_all("rows => rows.map(row => row.dataset.id)"))


def open_test_filter(page) -> None:
    page.locator("#btnFilterDropdown").click()
    page.locator('[data-filter="test"]').click()


def wait_for_ids(page, expected: set[str]) -> None:
    page.wait_for_function(
        "expected => { const rows = [...document.querySelectorAll('.drive-row')].map(row => row.dataset.id); "
        "return rows.length === expected.length && rows.every(id => expected.includes(id)); }",
        arg=list(expected), timeout=15000,
    )


def checked_badge(page, expected: int) -> None:
    page.wait_for_function(
        "expected => Number(document.querySelector('#trashCountBadge').textContent) === expected",
        arg=expected, timeout=15000,
    )


def scope_contains(context, item: dict) -> bool:
    if item.get("season_id"):
        scope_type, scope_id = "season", item["season_id"]
    elif item.get("series_id"):
        scope_type, scope_id = "series", item["series_id"]
    elif item.get("folder_id"):
        scope_type, scope_id = "folder", item["folder_id"]
    else:
        scope_type, scope_id = "root", "root"
    params = {"filter": "all"}
    if scope_type != "root":
        params.update({"type": scope_type, "id": scope_id})
    response = context.request.get(f"{BASE_URL}/api/v1/drive/scope", params=params)
    assert response.ok, f"Original {scope_type} scope did not load"
    return item["id"] in {row["id"] for row in response.json()["items"]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="perform the guarded recoverable live Trash acceptance")
    parser.add_argument("--capture-current", action="store_true", help="capture the current restored-test selection without changing data")
    args = parser.parse_args()
    if args.execute == args.capture_current:
        parser.error("Choose exactly one of --execute or --capture-current")

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    auth = create_admin_session()
    errors = []
    csp = []
    summary = {}
    try:
        if args.capture_current:
            with sync_playwright() as runner:
                browser = runner.chromium.launch(headless=True)
                context = browser.new_context(viewport={"width": 1440, "height": 900}, locale="ar-SA")
                context.add_cookies([
                    {"name": "aboodvideo_session", "value": auth["session"], "url": BASE_URL},
                    {"name": "aboodvideo_csrf", "value": auth["csrf"], "url": BASE_URL},
                ])
                page = context.new_page()
                page.goto(f"{BASE_URL}/preview/", wait_until="networkidle")
                open_test_filter(page)
                page.wait_for_function("() => document.querySelectorAll('.drive-row').length === 2")
                page.locator("#selectAllCheckbox").check()
                page.wait_for_timeout(350)  # allow the fixed toolbar transition to settle
                assert page.locator("#multiActionBar").is_visible()
                page.screenshot(path=str(ARTIFACTS / "library_bulk_toolbar_after_acceptance.png"))
                page.set_viewport_size({"width": 390, "height": 844})
                page.wait_for_timeout(350)
                assert page.locator("#mobileSelectAllCheckbox").is_checked()
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                page.screenshot(path=str(ARTIFACTS / "library_mobile_selection.png"))
                browser.close()
            print(f"Refreshed mobile and toolbar screenshots: {ARTIFACTS}")
            return

        with sync_playwright() as runner:
            browser = runner.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 900}, locale="ar-SA")
            context.add_cookies([
                {"name": "aboodvideo_session", "value": auth["session"], "url": BASE_URL},
                {"name": "aboodvideo_csrf", "value": auth["csrf"], "url": BASE_URL},
            ])
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
            page.on("console", lambda message: csp.append(message.text) if "content security policy" in message.text.lower() else None)
            page.goto(f"{BASE_URL}/preview/", wait_until="networkidle")
            page.locator(".drive-row").first.wait_for()
            initial_badge = int(page.locator("#trashCountBadge").inner_text())

            open_test_filter(page)
            page.wait_for_function("() => document.querySelectorAll('.drive-row').length >= 20")
            response = context.request.get(f"{BASE_URL}/api/v1/drive/scope", params={"filter": "test"})
            assert response.ok, "Could not verify live test filter"
            items = response.json()["items"]
            assert len(items) >= 20
            assert len({item["id"] for item in items}) == len(items)
            assert {item["title"] for item in items} <= SAFE_TITLES, "Unexpected title in live test filter; refusing Trash"
            restore_candidates = [item for item in items if not item.get("folder_id") and not item.get("series_id") and not item.get("season_id")]
            assert len(restore_candidates) >= 2, "No two root test records available for original-location verification"
            original_ids = {item["id"] for item in items}
            assert visible_ids(page) == original_ids, "UI rows do not match authoritative filtered records"
            summary["test_titles"] = dict(Counter(item["title"] for item in items))
            summary["initial_badge"] = initial_badge
            summary["bulk_count"] = len(items)
            page.screenshot(path=str(ARTIFACTS / "library_test_filter.png"))

            page.locator("#selectAllCheckbox").check()
            assert page.locator(".drive-row.is-selected").count() == len(items)
            assert page.locator("#selectAllCheckbox").is_checked()
            page.screenshot(path=str(ARTIFACTS / "library_bulk_selected.png"))
            page.locator("#btnMultiTrash").click()
            assert str(len(items)) in page.locator("#deleteModalDesc").inner_text()
            page.screenshot(path=str(ARTIFACTS / "library_bulk_confirm.png"))

            with page.expect_response(lambda response: response.url.endswith("/api/v1/drive/trash") and response.request.method == "POST") as response_info:
                page.locator("#btnConfirmDelete").click()
            trash_response = response_info.value
            assert trash_response.ok, f"Bulk Trash API rejected the request: {trash_response.status} {trash_response.text()}"
            wait_for_ids(page, set())
            checked_badge(page, initial_badge + len(items))
            assert "تم نقل" in page.locator("#toastContainer").inner_text()
            page.screenshot(path=str(ARTIFACTS / "library_after_trash.png"))

            page.locator("#navTrashLink").click()
            page.wait_for_function("expected => document.querySelectorAll('.drive-row').length >= expected", arg=len(items))
            assert original_ids <= visible_ids(page), "Some moved records are missing from Trash"
            page.screenshot(path=str(ARTIFACTS / "trash_view.png"))

            restored = restore_candidates[:2]
            restored_ids = {item["id"] for item in restored}
            for item in restored:
                page.locator(f'.drive-row[data-id="{item["id"]}"] .drive-checkbox').check()
            page.locator("#btnMultiRestore").click()
            page.wait_for_function("ids => ids.every(id => !document.querySelector(`.drive-row[data-id='${id}']`))", arg=list(restored_ids))
            checked_badge(page, initial_badge + len(items) - 2)
            assert all(scope_contains(context, item) for item in restored), "Restored records did not return to original scope"

            page.locator("#navLibraryLink").click()
            open_test_filter(page)
            wait_for_ids(page, restored_ids)
            # Exercise the same Trash path through the single-row context menu,
            # then restore the record so exactly two remain restored at closeout.
            single_id = restored[0]["id"]
            page.locator(f'.drive-row[data-id="{single_id}"] .action-dots-btn').click()
            page.locator("#ctxMenuTrash").click()
            with page.expect_response(lambda response: response.url.endswith("/api/v1/drive/trash") and response.request.method == "POST") as response_info:
                page.locator("#btnConfirmDelete").click()
            assert response_info.value.ok, f"Single-item Trash API rejected the request: {response_info.value.status}"
            wait_for_ids(page, restored_ids - {single_id})
            checked_badge(page, initial_badge + len(items) - 1)
            page.locator("#navTrashLink").click()
            page.locator(f'.drive-row[data-id="{single_id}"] .action-dots-btn').click()
            page.locator("#ctxMenuRestore").click()
            page.wait_for_function("id => !document.querySelector(`.drive-row[data-id='${id}']`)", arg=single_id)
            checked_badge(page, initial_badge + len(items) - 2)
            assert scope_contains(context, restored[0])
            summary["single_item_trash_verified"] = True
            summary["restored_ids"] = sorted(restored_ids)
            summary["left_in_trash"] = len(items) - 2

            page.set_viewport_size({"width": 390, "height": 844})
            page.locator("#mobileToggle").click()
            page.locator("#navLibraryLink").click()
            open_test_filter(page)
            wait_for_ids(page, restored_ids)
            page.locator("#mobileSelectAllCheckbox").check()
            assert page.locator(".drive-row.is-selected").count() == 2
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            assert page.locator("#btnMultiTrash").is_visible()
            page.screenshot(path=str(ARTIFACTS / "library_mobile_selection.png"))
            summary["mobile_overflow"] = False
            summary["console_errors"] = errors
            summary["csp_violations"] = csp
            assert not errors and not csp, "Live browser diagnostics are not clean"
            browser.close()
    finally:
        revoke_test_session(auth["session"])

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Screenshots: {ARTIFACTS}")


if __name__ == "__main__":
    main()
