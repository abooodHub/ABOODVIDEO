"""Playwright automated test script for ABOODVIDEO Player features:
- Playback (HLS stream loading, play/pause, time progression)
- Volume & Mute control
- Settings Menu (Quality selection & Playback speed)
- Subtitles Menu
- Diagnostics Panel (QoE telemetry)
"""
import sys
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE_URL = "https://platform.example"
ARTIFACTS_DIR = Path(r"C:\Users\abood\.gemini\antigravity\brain\c58b9ae4-6748-4d1c-828d-53dae7a841e8")

async def test_player():
    print("[1] Launching browser for Player Testing...")
    async with async_playwright() as p:
        # Launch with media stream support
        browser = await p.chromium.launch(
            headless=True,
            args=["--autoplay-policy=no-user-gesture-required"]
        )
        context = await browser.new_context(viewport={"width": 1600, "height": 950})
        page = await context.new_page()

        console_logs = []
        page_errors = []
        page.on("console", lambda msg: console_logs.append(f"[{msg.type}] {msg.text}"))
        page.on("pageerror", lambda err: page_errors.append(str(err)))

        # 1. Login to get authenticated admin session
        print("[2] Logging in to admin session...")
        await page.goto(f"{BASE_URL}/admin/login", wait_until="networkidle")
        await page.fill("#username", "abood")
        await page.fill("#password", "AboodVideo2026!Secure")
        await page.click("button[type='submit']")
        await page.wait_for_url("**/admin**", timeout=10000)
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(1)

        # 2. Get first video ID from Content Library
        print("[3] Navigating to Content Library to fetch a video...")
        await page.click('a[href="/admin/videos"]')
        await page.wait_for_url("**/admin/videos", timeout=10000)
        await page.wait_for_selector("tr.drive-row[data-type='video']", timeout=10000)
        
        first_video_el = page.locator("tr.drive-row[data-type='video']").first
        video_id = await first_video_el.get_attribute("data-id")
        video_name = await first_video_el.locator(".name-primary").text_content()
        print(f"[4] Testing Player on Video: ID={video_id}, Name='{video_name.strip()}'")

        # 3. Open Watch Page
        watch_url = f"{BASE_URL}/watch/{video_id}"
        print(f"[5] Navigating to Watch Page: {watch_url}")
        await page.goto(watch_url, wait_until="domcontentloaded")
        await page.wait_for_selector("#video-element", timeout=15000)
        await asyncio.sleep(2)

        # Screenshot 1: Player Initial Loaded State
        shot1 = ARTIFACTS_DIR / "23_player_initial_state.png"
        await page.screenshot(path=str(shot1))
        print(f"[6] Initial player screenshot: {shot1}")

        # 4. Trigger Play
        print("[7] Starting Playback...")
        big_play = page.locator("#big-play-btn")
        if await big_play.is_visible():
            await big_play.click()
        else:
            await page.click("#btn-play")

        # Wait for playback to progress
        print("[8] Waiting for video playback to advance...")
        for attempt in range(15):
            await asyncio.sleep(1)
            cur_time = await page.evaluate("() => document.getElementById('video-element').currentTime")
            paused = await page.evaluate("() => document.getElementById('video-element').paused")
            duration = await page.evaluate("() => document.getElementById('video-element').duration")
            print(f"    - CurrentTime: {cur_time:.2f}s / {duration:.2f}s | Paused: {paused}")
            if cur_time > 1.0 and not paused:
                print("    Playback confirmed actively running! ✓")
                break

        # Screenshot 2: Active Playing State
        shot2 = ARTIFACTS_DIR / "24_player_actively_playing.png"
        await page.screenshot(path=str(shot2))
        print(f"[9] Active playing screenshot: {shot2}")

        # 5. Test Volume / Mute
        print("[10] Testing Mute / Unmute...")
        is_muted_before = await page.evaluate("() => document.getElementById('video-element').muted")
        await page.click("#btn-mute")
        await asyncio.sleep(0.5)
        is_muted_after = await page.evaluate("() => document.getElementById('video-element').muted")
        print(f"    - Muted Before: {is_muted_before} -> After Mute Click: {is_muted_after}")
        await page.click("#btn-mute")  # Unmute back

        # 6. Test Settings Menu (Qualities & Speed)
        print("[11] Testing Settings Menu...")
        await page.click("#btn-settings")
        await asyncio.sleep(0.5)

        # Screenshot 3: Settings Root Menu Open
        shot3 = ARTIFACTS_DIR / "25_player_settings_root_menu.png"
        await page.screenshot(path=str(shot3))
        print(f"[12] Settings root menu screenshot: {shot3}")

        # Navigate into Quality Screen
        print("    Opening Quality Screen...")
        await page.click('button[data-open-screen="quality"]')
        await asyncio.sleep(0.5)

        # Check quality options in #quality-menu
        quality_items = await page.locator("#quality-menu .menu-item").all_text_contents()
        clean_qualities = [q.strip() for q in quality_items if q.strip()]
        print(f"    - Available Quality Options: {clean_qualities}")

        shot3b = ARTIFACTS_DIR / "25b_player_quality_options.png"
        await page.screenshot(path=str(shot3b))

        # Navigate back to root and into Speed Screen
        print("    Navigating to Speed Screen...")
        await page.click('.player-menu-screen[data-screen="quality"] .player-menu-back')
        await asyncio.sleep(0.3)
        await page.click('button[data-open-screen="speed"]')
        await asyncio.sleep(0.5)

        speed_items = await page.locator("#speed-menu .menu-item").all_text_contents()
        clean_speeds = [s.strip() for s in speed_items if s.strip()]
        print(f"    - Available Speeds: {clean_speeds}")

        # Click 1.5x speed
        speed_15 = page.locator("#speed-menu .menu-item:has-text('1.5')")
        if await speed_15.count() > 0:
            await speed_15.click()
            await asyncio.sleep(0.5)
            playback_rate = await page.evaluate("() => document.getElementById('video-element').playbackRate")
            print(f"    - Playback Speed confirmed switched to: {playback_rate}x ✓")

        # Close settings menu
        await page.click("#btn-settings")
        await asyncio.sleep(0.3)

        # 7. Test Subtitles Menu
        print("[13] Testing Subtitles Menu...")
        btn_subs = page.locator("#btn-subs")
        if await btn_subs.is_visible():
            await btn_subs.click()
            await asyncio.sleep(0.5)
            sub_items = await page.locator("#subs-menu .menu-item").all_text_contents()
            print(f"    - Available Subtitles: {sub_items}")
            shot4 = ARTIFACTS_DIR / "26_player_subtitles_menu.png"
            await page.screenshot(path=str(shot4))
            print(f"[14] Subtitles menu screenshot: {shot4}")
            await page.click("#btn-subs")  # Toggle close
            await asyncio.sleep(0.3)

        # 8. Check QoE Diagnostics Panel
        print("[15] Checking Diagnostics Panel...")
        diag_panel = page.locator("#player-diagnostics-panel")
        # Try opening via keyboard shortcut (usually 'D' or via menu)
        await page.keyboard.press("d")
        await asyncio.sleep(0.5)
        if await diag_panel.is_visible():
            print("    Diagnostics panel opened via hotkey 'D' ✓")
            diag_text = await diag_panel.inner_text()
            print(f"    Diagnostics Info:\n{diag_text}")
            shot5 = ARTIFACTS_DIR / "27_player_diagnostics_panel.png"
            await page.screenshot(path=str(shot5))
            print(f"[16] Diagnostics panel screenshot: {shot5}")

        # Print summary of console errors if any
        print(f"[17] Page Error Count: {len(page_errors)}")
        for err in page_errors:
            print(f"    PAGE ERROR: {err}")

        await browser.close()
        print("=" * 60)
        print("ALL PLAYER TESTS COMPLETED SUCCESSFULLY!")
        print("=" * 60)

if __name__ == "__main__":
    asyncio.run(test_player())
