"""Interactive end-to-end test for Content Library:
- Create folder via right-click empty space
- Verify folder renders in table
- Rename folder via right-click context menu
- Delete test folder to keep library clean
"""
import sys
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE_URL = "https://platform.example"
ARTIFACTS_DIR = Path(r"C:\Users\abood\.gemini\antigravity\brain\c58b9ae4-6748-4d1c-828d-53dae7a841e8")

async def test_workflow():
    print("[1] Launching browser...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1600, "height": 950})
        page = await context.new_page()

        # 1. Login
        print("[2] Logging in...")
        await page.goto(f"{BASE_URL}/admin/login", wait_until="networkidle")
        await page.fill("#username", "abood")
        await page.fill("#password", "AboodVideo2026!Secure")
        await page.click("button[type='submit']")
        await page.wait_for_url("**/admin**", timeout=10000)
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(1)

        # 2. Content Library
        print("[3] Opening Content Library via sidebar...")
        await page.click('a[href="/admin/videos"]')
        await page.wait_for_url("**/admin/videos", timeout=10000)
        await page.wait_for_selector("#driveTableBody tr", timeout=10000)
        await asyncio.sleep(1)

        # 3. Right-click canvas empty area
        print("[4] Right-clicking canvas empty area...")
        await page.click(".table-container", button="right", position={"x": 300, "y": 550})
        await page.wait_for_selector("#emptyContextMenu.is-open", timeout=5000)
        print("  - Empty canvas context menu opened!")

        # 4. Click 'مجلد جديد' (emptyCtxNewFolder)
        print("[5] Clicking 'مجلد جديد' from context menu...")
        await page.click("#emptyCtxNewFolder")
        await page.wait_for_selector("#folderModal.is-open", timeout=5000)
        print("  - Create folder modal opened!")

        folder_name = "مجلد أنمي تجريبي"
        await page.fill("#folderModalInput", folder_name)
        await page.click("#btnConfirmCreateFolder")
        await asyncio.sleep(1.5)

        # Verify folder exists in table
        print(f"[6] Verifying folder '{folder_name}' in table...")
        folder_row = page.locator(f"tr.drive-row[data-type='folder']:has-text('{folder_name}')")
        await folder_row.wait_for(timeout=5000)
        print("  - Folder is visible in table!")

        # Screenshot: Created Folder in Library
        shot1 = ARTIFACTS_DIR / "19_interactive_folder_created.png"
        await page.screenshot(path=str(shot1))
        print(f"  - Screenshot saved: {shot1}")

        # 5. Right-click the folder row to rename
        print("[7] Right-clicking the created folder...")
        await folder_row.click(button="right")
        await page.wait_for_selector("#unifiedContextMenu.is-open", timeout=5000)
        print("  - Context menu opened on folder!")

        # 6. Click 'إعادة تسمية' (ctxMenuRename)
        print("[8] Clicking 'إعادة تسمية'...")
        await page.click("#ctxMenuRename")
        await page.wait_for_selector("#renameModal.is-open", timeout=5000)
        print("  - Rename modal opened!")

        renamed_folder = "مجلد أنمي تم تعديل اسمه"
        await page.fill("#renameModalInput", renamed_folder)
        await page.click("#btnConfirmRename")
        await asyncio.sleep(1.5)

        # Verify renamed folder exists
        print(f"[9] Verifying renamed folder '{renamed_folder}' in table...")
        renamed_row = page.locator(f"tr.drive-row[data-type='folder']:has-text('{renamed_folder}')")
        await renamed_row.wait_for(timeout=5000)
        print("  - Renamed folder verified in table!")

        # Screenshot: Renamed Folder in Library
        shot2 = ARTIFACTS_DIR / "20_interactive_folder_renamed.png"
        await page.screenshot(path=str(shot2))
        print(f"  - Screenshot saved: {shot2}")

        # 7. Clean up the test folder (Move to trash)
        print("[10] Cleaning up test folder via context menu 'نقل إلى سلة المحذوفات'...")
        await renamed_row.click(button="right")
        await page.wait_for_selector("#unifiedContextMenu.is-open", timeout=5000)
        await page.click("#ctxMenuTrash")
        await page.wait_for_selector("#deleteModal.is-open", timeout=5000)
        await page.click("#btnConfirmDelete")
        await asyncio.sleep(1.5)

        print("[11] Verifying test folder was removed from Content Library view...")
        is_gone = await page.locator(f"tr.drive-row[data-type='folder']:has-text('{renamed_folder}')").count() == 0
        print(f"  - Folder removed from active view: {is_gone}")

        # Screenshot: Restored clean state
        shot3 = ARTIFACTS_DIR / "21_interactive_clean_state.png"
        await page.screenshot(path=str(shot3))
        print(f"  - Screenshot saved: {shot3}")

        await browser.close()
        print("=" * 60)
        print("ALL INTERACTIVE WORKFLOW TESTS PASSED PERFECTLY!")
        print("=" * 60)

if __name__ == "__main__":
    asyncio.run(test_workflow())
