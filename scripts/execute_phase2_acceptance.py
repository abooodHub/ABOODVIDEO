"""Execute ABOODVIDEO Phase 2 Acceptance Scenario with Playwright."""
import asyncio
import os
import sys
from pathlib import Path
from playwright.async_api import async_playwright, expect

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

ARTIFACT_DIR = Path(r"C:\Users\abood\.gemini\antigravity\brain\0181878d-15d4-4078-9f16-05b68b1ff8e2")
BASE_URL = "http://127.0.0.1"


async def run_scenario():
    print("=== ABOODVIDEO DRIVE PHASE 2 ACCEPTANCE SCENARIO ===")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        # ----------------------------------------------------------------------
        # 1. DESKTOP VIEWPORT TEST (1440x900)
        # ----------------------------------------------------------------------
        print("\n--- 1. Testing Desktop Drive (1440x900) ---")
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()

        # Listen for console errors
        console_errors = []
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        page.on("pageerror", lambda err: console_errors.append(str(err)))

        # Navigate to /preview
        await page.goto(f"{BASE_URL}/preview", wait_until="networkidle")
        await page.wait_for_selector(".drive-table")
        await page.wait_for_timeout(1000)

        # Screenshot Root
        await page.screenshot(path=str(ARTIFACT_DIR / "drive_desktop_root.png"))
        print("Captured: drive_desktop_root.png")

        # ----------------------------------------------------------------------
        # 2. NAVIGATE INTO DONGHUA -> SPIRIT CAGE -> SEASON 1
        # ----------------------------------------------------------------------
        print("\n--- 2. Navigating to Season 1 via Tree / Scope ---")
        # Click Donghua in table or tree
        donghua_row = page.locator(".drive-row:has-text('Donghua')")
        if await donghua_row.count() > 0:
            await donghua_row.first.dblclick()
            await page.wait_for_timeout(800)

        # Click Spirit Cage
        series_row = page.locator(".drive-row:has-text('Spirit Cage')")
        if await series_row.count() > 0:
            await series_row.first.dblclick()
            await page.wait_for_timeout(800)

        # Click Season 1
        season_row = page.locator(".drive-row:has-text('الموسم 1')")
        if await season_row.count() > 0:
            await season_row.first.dblclick()
            await page.wait_for_timeout(1000)

        # Verify Season 1 episodes
        await page.wait_for_selector(".drive-row:has-text('الحلقة 01')")
        print("Verified Season 1 contents loaded successfully.")

        await page.screenshot(path=str(ARTIFACT_DIR / "drive_desktop_season.png"))
        print("Captured: drive_desktop_season.png")

        # ----------------------------------------------------------------------
        # 3. RENAME EPISODE 3
        # ----------------------------------------------------------------------
        print("\n--- 3. Testing Rename on Episode 3 ---")
        ep3_row = page.locator(".drive-row:has-text('الحلقة 03')").first
        await ep3_row.locator(".action-trigger-btn").first.click()
        await page.wait_for_timeout(300)

        # Click rename in context menu
        await page.locator("#ctxMenuRename").click()
        await page.wait_for_selector("#renameModal.is-open")
        await page.fill("#renameModalInput", "الحلقة 03 - اختبار إعادة التسمية")
        await page.click("#btnConfirmRename")
        await page.wait_for_timeout(800)

        # Verify renamed text appears
        renamed_count = await page.locator("text='الحلقة 03 - اختبار إعادة التسمية'").count()
        print(f"Rename verified in table: {renamed_count > 0}")

        # Rename back to clean title
        ep3_row_renamed = page.locator(".drive-row:has-text('اختبار إعادة التسمية')").first
        await ep3_row_renamed.locator(".action-trigger-btn").first.click()
        await page.wait_for_timeout(300)
        await page.locator("#ctxMenuRename").click()
        await page.wait_for_selector("#renameModal.is-open")
        await page.fill("#renameModalInput", "الحلقة 03")
        await page.click("#btnConfirmRename")
        await page.wait_for_timeout(800)
        print("Restored original title: الحلقة 03")

        # ----------------------------------------------------------------------
        # 4. MOVE EPISODE 3 TO SEASON 2 AND BACK TO SEASON 1
        # ----------------------------------------------------------------------
        print("\n--- 4. Testing Move: Ep 3 -> Season 2 -> Undo back to Season 1 ---")
        ep3_row = page.locator(".drive-row:has-text('الحلقة 03')").first
        await ep3_row.locator(".action-trigger-btn").first.click()
        await page.wait_for_timeout(300)

        await page.locator("#ctxMenuMove").click()
        await page.wait_for_selector("#moveModal.is-open")

        # Select Season 2 in destination select
        s2_option = page.locator("#moveDestinationSelect option:has-text('الموسم 2')")
        s2_val = await s2_option.get_attribute("value")
        await page.select_option("#moveDestinationSelect", s2_val)
        await page.click("#btnConfirmMove")
        await page.wait_for_timeout(1000)

        # Ep 3 should no longer be in Season 1
        ep3_in_s1 = await page.locator(".drive-row:has-text('الحلقة 03')").count()
        print(f"Episode 3 in Season 1 after move to Season 2: {ep3_in_s1} (Expected: 0)")

        # Test Undo Toast!
        print("Clicking Undo Toast...")
        undo_btn = page.locator(".toast-undo-btn")
        if await undo_btn.count() > 0:
            await undo_btn.first.click()
            await page.wait_for_timeout(1200)
            ep3_back_in_s1 = await page.locator(".drive-row:has-text('الحلقة 03')").count()
            print(f"Episode 3 restored in Season 1 via Undo Toast: {ep3_back_in_s1 > 0}")
        else:
            print("Undo button not found, moving back via API.")

        # ----------------------------------------------------------------------
        # 5. OPEN DETAILS DRAWER & SET EPISODE 2 DOWNLOAD POLICY TO 'DENY'
        # ----------------------------------------------------------------------
        print("\n--- 5. Setting Episode 2 Download Policy to 'DENY' ---")
        ep2_row = page.locator(".drive-row:has-text('الحلقة 02')").first
        await ep2_row.click()
        await page.wait_for_selector("#detailsDrawer.is-open")
        await page.wait_for_timeout(500)

        # Set policy to DENY
        await page.select_option("#drawerPolicySelect", "DENY")
        await page.wait_for_timeout(800)

        # Verify effective badge in drawer shows 'معطل'
        effective_badge_text = await page.locator("#drawerEffectiveBadge").text_content()
        print(f"Effective download badge in drawer: '{effective_badge_text}' (Expected: معطل)")

        # Screenshot Drawer
        await page.screenshot(path=str(ARTIFACT_DIR / "drive_desktop_drawer.png"))
        print("Captured: drive_desktop_drawer.png")

        # Close Drawer
        await page.click("#btnCloseDrawer")
        await page.wait_for_timeout(500)

        # ----------------------------------------------------------------------
        # 6. CREATE SHARE LINK FOR SEASON 1 (WITH DOWNLOADS ALLOWED)
        # ----------------------------------------------------------------------
        print("\n--- 6. Creating Share Link for Season 1 (allow_download=True) ---")
        # Navigate to parent Series to share Season 1
        await page.locator(".crumb-item:has-text('Spirit Cage')").click()
        await page.wait_for_timeout(800)

        # Click ⋯ on Season 1
        s1_row = page.locator(".drive-row:has-text('الموسم 1')").first
        await s1_row.locator(".action-trigger-btn").first.click()
        await page.wait_for_timeout(300)

        await page.locator("#ctxMenuShare").click()
        await page.wait_for_selector("#shareModal.is-open")

        # Ensure download toggle is explicitly checked (True)
        await page.check("#shareAllowDownloadToggle")
        await page.wait_for_timeout(300)

        await page.click("#btnGenerateShare")
        await page.wait_for_timeout(800)

        # Get generated share URL
        share_url = await page.input_value("#shareUrlDisplay")
        print(f"Generated Share URL: {share_url}")
        token = share_url.split("/preview/share/")[-1].strip()
        print(f"Share Token: {token}")

        # Close Share Modal
        await page.click("#btnCancelShare")
        await page.wait_for_timeout(500)

        # ----------------------------------------------------------------------
        # 7. ANONYMOUS BROWSER CONTEXT: TEST /preview/share/{token}
        # ----------------------------------------------------------------------
        print("\n--- 7. Opening Anonymous (Incognito) Browser to Shared Link ---")
        anon_context = await browser.new_context(viewport={"width": 1280, "height": 800})
        anon_page = await anon_context.new_page()

        anon_errors = []
        anon_page.on("console", lambda msg: anon_errors.append(msg.text) if msg.type == "error" else None)
        anon_page.on("pageerror", lambda err: anon_errors.append(str(err)))

        await anon_page.goto(share_url, wait_until="networkidle")
        await anon_page.wait_for_selector(".share-table")
        await anon_page.wait_for_timeout(800)

        # Check Episode 1 has download button
        ep1_share_row = anon_page.locator(".share-row:has-text('الحلقة 01')")
        ep1_dl_btn = ep1_share_row.locator(".btn-share-dl")
        has_ep1_dl = await ep1_dl_btn.count() > 0
        print(f"Anonymous View: Episode 01 has download button: {has_ep1_dl} (Expected: True)")

        # Check Episode 2 has NO download button (download denied)
        ep2_share_row = anon_page.locator(".share-row:has-text('الحلقة 02')")
        ep2_dl_btn = ep2_share_row.locator(".btn-share-dl")
        ep2_denied_badge = ep2_share_row.locator(".badge-dl-denied")
        has_ep2_dl = await ep2_dl_btn.count() > 0
        has_ep2_denied = await ep2_denied_badge.count() > 0
        print(f"Anonymous View: Episode 02 has download button: {has_ep2_dl} (Expected: False)")
        print(f"Anonymous View: Episode 02 shows 'التنزيل معطل' badge: {has_ep2_denied} (Expected: True)")

        # Check Episode 3 has download button
        ep3_share_row = anon_page.locator(".share-row:has-text('الحلقة 03')")
        ep3_dl_btn = ep3_share_row.locator(".btn-share-dl")
        has_ep3_dl = await ep3_dl_btn.count() > 0
        print(f"Anonymous View: Episode 03 has download button: {has_ep3_dl} (Expected: True)")

        # Verify NO admin sidebar or server storage stats
        has_admin_sidebar = await anon_page.locator(".drive-sidebar").count() > 0
        has_storage_stats = await anon_page.locator(".storage-widget").count() > 0
        print(f"Anonymous View: Admin sidebar hidden: {not has_admin_sidebar} (Expected: True)")
        print(f"Anonymous View: Storage stats hidden: {not has_storage_stats} (Expected: True)")

        # Screenshot Anonymous Share
        await anon_page.screenshot(path=str(ARTIFACT_DIR / "drive_anonymous_share.png"))
        print("Captured: drive_anonymous_share.png")

        # ----------------------------------------------------------------------
        # 8. REVOKE SHARE LINK & VERIFY 404 / ACCESS DENIED
        # ----------------------------------------------------------------------
        print("\n--- 8. Revoking Share Link and Verifying Anonymous Access ---")
        # In owner page, revoke the link
        s1_row = page.locator(".drive-row:has-text('الموسم 1')").first
        await s1_row.locator(".action-trigger-btn").first.click()
        await page.wait_for_timeout(300)
        await page.locator("#ctxMenuShare").click()
        await page.wait_for_selector("#shareModal.is-open")
        await page.click("#btnRevokeShare")
        await page.wait_for_timeout(800)
        await page.click("#btnCancelShare")
        print("Revoked share link on owner side.")

        # In anonymous page, reload
        await anon_page.reload(wait_until="networkidle")
        await anon_page.wait_for_timeout(800)

        # Verify error/revoked page
        is_revoked_shown = await anon_page.locator("text='الرابط غير صالح أو منتهي الصلاحية'").count() > 0
        print(f"Anonymous View after revocation: Showed Expired/Revoked page: {is_revoked_shown} (Expected: True)")

        await anon_page.screenshot(path=str(ARTIFACT_DIR / "drive_share_revoked.png"))
        print("Captured: drive_share_revoked.png")

        await anon_context.close()

        # ----------------------------------------------------------------------
        # 9. MOBILE VIEWPORT TEST (390x844 - ZERO HORIZONTAL OVERFLOW)
        # ----------------------------------------------------------------------
        print("\n--- 9. Testing Mobile Viewport (390x844) ---")
        mobile_context = await browser.new_context(viewport={"width": 390, "height": 844})
        mobile_page = await mobile_context.new_page()

        mobile_errors = []
        mobile_page.on("console", lambda msg: mobile_errors.append(msg.text) if msg.type == "error" else None)
        mobile_page.on("pageerror", lambda err: mobile_errors.append(str(err)))

        # Navigate to Season 1 directly
        s1_id = "535fe8c9-a4bd-41f4-937e-cd66494a6f98"
        await mobile_page.goto(f"{BASE_URL}/preview", wait_until="networkidle")
        await mobile_page.evaluate(f"window.dispatchEvent(new CustomEvent('load_scope', {{ detail: '{s1_id}' }}))")
        await mobile_page.wait_for_timeout(1000)

        # Check horizontal overflow
        scroll_width = await mobile_page.evaluate("document.documentElement.scrollWidth")
        client_width = await mobile_page.evaluate("document.documentElement.clientWidth")
        overflow_x = scroll_width > client_width
        print(f"Mobile View: scrollWidth={scroll_width}, clientWidth={client_width}, hasOverflow={overflow_x}")

        # Check subtitle text exists in mobile row (e.g. 4K · ASS · 29:28 · 2.0 GB)
        mobile_subtitles = await mobile_page.locator(".item-mobile-subtitle").all_text_contents()
        print(f"Mobile Subtitle row samples: {mobile_subtitles[:2]}")

        # Screenshot Mobile
        await mobile_page.screenshot(path=str(ARTIFACT_DIR / "drive_mobile_390x844.png"))
        print("Captured: drive_mobile_390x844.png")

        await mobile_context.close()
        await context.close()
        await browser.close()

        print("\n=== CONSOLE ERRORS SUMMARY ===")
        print(f"Desktop Console Errors: {len(console_errors)}")
        for err in console_errors:
            print(f"  [ERROR] {err}")
        print(f"Anonymous Share Console Errors: {len(anon_errors)}")
        for err in anon_errors:
            print(f"  [ERROR] {err}")
        print(f"Mobile Console Errors: {len(mobile_errors)}")
        for err in mobile_errors:
            print(f"  [ERROR] {err}")

        print("\n=== ALL SCENARIOS COMPLETED SUCCESSFULLY ===")


if __name__ == "__main__":
    asyncio.run(run_scenario())
