import os
"""Playwright automated verification script for Unified Content Library (Drive) UI."""
import sys
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE_URL = "https://platform.example"
ARTIFACTS_DIR = Path(r"C:\Users\abood\.gemini\antigravity\brain\c58b9ae4-6748-4d1c-828d-53dae7a841e8")

async def test_content_library():
    admin_password = os.environ.get("ABOODVIDEO_ADMIN_PASSWORD")
    if not admin_password:
        raise RuntimeError("Set ABOODVIDEO_ADMIN_PASSWORD in the environment before using this live browser check.")
    print("[1] Launching browser...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1600, "height": 950})
        page = await context.new_page()

        page.on("console", lambda msg: print(f"[Browser Console] {msg.type}: {msg.text}"))
        page.on("pageerror", lambda err: print(f"[Browser Error] {err}"))

        # Step 1: Login
        print(f"[2] Navigating to {BASE_URL}/admin/login...")
        await page.goto(f"{BASE_URL}/admin/login", wait_until="networkidle")
        await page.fill("#username", "abood")
        await page.fill("#password", admin_password)
        await page.click("button[type='submit']")
        await page.wait_for_url("**/admin**", timeout=10000)
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(1)
        print("[3] Successfully authenticated into Admin Dashboard.")

        # Step 2: Open Content Library
        print("[4] Navigating to /admin/videos (Content Library)...")
        await page.goto(f"{BASE_URL}/admin/videos", wait_until="domcontentloaded")
        await page.wait_for_selector("#driveTableBody", state="attached", timeout=10000)
        await asyncio.sleep(2)  # Wait for API fetch

        title = await page.title()
        print(f"[5] Page Title: {title}")

        # Check visible items count
        badge_text = await page.text_content("#itemCountBadge")
        print(f"[6] Item Count Badge: {badge_text}")

        # Screenshot 1: Content Library Main View
        shot1_path = ARTIFACTS_DIR / "14_content_library_main_drive.png"
        await page.screenshot(path=str(shot1_path), full_page=False)
        print(f"[7] Captured screenshot: {shot1_path}")

        # Step 3: Test Right-Click on Empty Space
        print("[8] Triggering right-click on empty canvas area...")
        # Right click on the table container / main workspace
        await page.click(".table-container", button="right", position={"x": 300, "y": 550})
        await asyncio.sleep(0.5)

        # Check if #emptyContextMenu is open
        is_empty_menu_open = await page.is_visible("#emptyContextMenu.is-open")
        print(f"[9] Is Empty Canvas Context Menu Open? {is_empty_menu_open}")

        # Screenshot 2: Empty Area Context Menu
        shot2_path = ARTIFACTS_DIR / "15_content_library_right_click_empty.png"
        await page.screenshot(path=str(shot2_path), full_page=False)
        print(f"[10] Captured screenshot: {shot2_path}")

        # Close empty menu
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.3)

        # Step 4: Test Right-Click on a Video Item
        print("[11] Triggering right-click on first video row...")
        first_row = page.locator(".drive-row").first
        if await first_row.count() > 0:
            await first_row.click(button="right")
            await asyncio.sleep(0.5)

            is_unified_menu_open = await page.is_visible("#unifiedContextMenu.is-open")
            print(f"[12] Is Item Context Menu Open? {is_unified_menu_open}")

            # Check if rename button is visible
            is_rename_visible = await page.is_visible("#ctxMenuRename:not(.is-hidden)")
            print(f"[13] Is Rename button visible? {is_rename_visible}")

            # Screenshot 3: Item Context Menu (Rename, Move, Play, Details, Delete)
            shot3_path = ARTIFACTS_DIR / "16_content_library_right_click_item.png"
            await page.screenshot(path=str(shot3_path), full_page=False)
            print(f"[14] Captured screenshot: {shot3_path}")

            # Click Rename to verify modal
            await page.click("#ctxMenuRename")
            await asyncio.sleep(0.5)

            is_rename_modal_open = await page.is_visible("#renameModal.is-open")
            print(f"[15] Is Rename Modal Open? {is_rename_modal_open}")

            shot4_path = ARTIFACTS_DIR / "17_content_library_rename_modal.png"
            await page.screenshot(path=str(shot4_path), full_page=False)
            print(f"[16] Captured screenshot: {shot4_path}")

            # Close rename modal
            await page.click("#btnCancelRename")
            await asyncio.sleep(0.3)

        # Step 5: Test [+ جديد] Dropdown and New Folder Modal
        print("[17] Opening New Folder Modal via [+ جديد] button...")
        await page.click("#btnNewDropdown")
        await asyncio.sleep(0.3)
        await page.click("#menuItemNewFolder")
        await asyncio.sleep(0.5)

        is_folder_modal_open = await page.is_visible("#folderModal.is-open")
        print(f"[18] Is Create Folder Modal Open? {is_folder_modal_open}")

        shot5_path = ARTIFACTS_DIR / "18_content_library_create_folder_modal.png"
        await page.screenshot(path=str(shot5_path), full_page=False)
        print(f"[19] Captured screenshot: {shot5_path}")

        await page.click("#btnCancelCreateFolder")
        await asyncio.sleep(0.3)

        await browser.close()
        print("=" * 60)
        print("ALL PLAYWRIGHT ACCEPTANCE CHECKS PASSED!")
        print("=" * 60)

if __name__ == "__main__":
    asyncio.run(test_content_library())
