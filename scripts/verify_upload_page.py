"""Verify upload page with Playwright after removal of upload-sidebar."""
import sys
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE_URL = "https://platform.example"
ARTIFACTS_DIR = Path(r"C:\Users\abood\.gemini\antigravity\brain\c58b9ae4-6748-4d1c-828d-53dae7a841e8")

async def test_upload_page():
    print("[1] Launching browser...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1600, "height": 950})
        page = await context.new_page()

        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))

        # Login
        print("[2] Logging in...")
        await page.goto(f"{BASE_URL}/admin/login", wait_until="networkidle")
        await page.fill("#username", "abood")
        await page.fill("#password", "AboodVideo2026!Secure")
        await page.click("button[type='submit']")
        await page.wait_for_url("**/admin**", timeout=10000)
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(1)

        # Open /admin/upload
        print("[3] Opening /admin/upload via sidebar link...")
        await page.click('a[href="/admin/upload"]')
        await page.wait_for_url("**/admin/upload", timeout=10000)
        await page.wait_for_selector("#dropzone", timeout=10000)
        await asyncio.sleep(1)

        # Check if .upload-sidebar exists
        sidebar_count = await page.locator(".upload-sidebar").count()
        print(f"[4] .upload-sidebar count: {sidebar_count} (Expected: 0)")

        # Check for page errors
        print(f"[5] JavaScript Page errors count: {len(errors)}")
        for err in errors:
            print(f"    ERROR: {err}")

        # Capture screenshot
        shot_path = ARTIFACTS_DIR / "22_upload_page_no_sidebar.png"
        await page.screenshot(path=str(shot_path))
        print(f"[6] Screenshot saved: {shot_path}")

        await browser.close()
        assert sidebar_count == 0, "upload-sidebar should be removed"
        assert len(errors) == 0, f"Page had errors: {errors}"
        print("=" * 60)
        print("UPLOAD PAGE VERIFICATION PASSED!")
        print("=" * 60)

if __name__ == "__main__":
    asyncio.run(test_upload_page())
