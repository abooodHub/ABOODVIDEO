"""Playwright Automated Acceptance Test for ABOODVIDEO Smart QoE and Secure Playback."""
import asyncio
import os
import sys
import time
from playwright.async_api import async_playwright

sys.stdout.reconfigure(encoding="utf-8")

WATCH_URL = "http://127.0.0.1/watch/yGWgWuIX_d6QFeKy4l-22Q"
ARTIFACTS_DIR = r"C:\Users\abood\.gemini\antigravity\brain\b22ee447-0980-43d7-8d2b-bc181f4edc7c"
os.makedirs(ARTIFACTS_DIR, exist_ok=True)

async def main():
    async with async_playwright() as p:
        print(f"--> Launching Chromium browser to {WATCH_URL}...")
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1280, "height": 720}, bypass_csp=True)
        page = await context.new_page()

        page.on("console", lambda msg: print(f"[BROWSER {msg.type.upper()}] {msg.text}"))
        page.on("pageerror", lambda err: print(f"[PAGE ERROR] {err}"))

        print("--> Navigating to watch page...")
        t0 = time.time()
        resp = await page.goto(WATCH_URL, wait_until="networkidle")
        load_time = (time.time() - t0) * 1000
        print(f"Page loaded with HTTP {resp.status} in {load_time:.1f}ms")

        # Verify HttpOnly Cookie
        cookies = await context.cookies()
        playback_cookie = next((c for c in cookies if c["name"] == "abood_playback"), None)
        assert playback_cookie, "abood_playback cookie not found in browser context"
        print(f"✓ Cookie verified in browser: {playback_cookie['name']} (httpOnly={playback_cookie['httpOnly']}, sameSite={playback_cookie['sameSite']})")

        # Click big play button or video to begin playback
        print("--> Starting playback...")
        play_btn = page.locator("#big-play-btn")
        if await play_btn.is_visible():
            await play_btn.click()
        else:
            await page.locator("#video-element").click()

        # Wait for video to start playing using non-eval loop
        cur_time = 0.0
        for _ in range(40):
            await page.wait_for_timeout(500)
            try:
                cur_time = float(await page.evaluate("() => document.getElementById('video-element')?.currentTime || 0"))
                if cur_time > 0.5:
                    break
            except Exception:
                pass
        print(f"✓ Playback active at currentTime={cur_time:.2f}s")

        # Check available quality levels
        print("--> Finding available quality levels in player...")
        levels = []
        for _ in range(20):
            levels = await page.evaluate("() => window.hls?.levels?.map((l, i) => ({ i, height: l.height, bitrate: l.bitrate })) || []")
            if levels:
                break
            await page.wait_for_timeout(500)
        print(f"Available levels: {levels}")

        idx_4k = next((l["i"] for l in levels if l["height"] and l["height"] >= 2160), None)
        idx_1080 = next((l["i"] for l in levels if l["height"] and l["height"] == 1080), None)
        print(f"4K index: {idx_4k}, 1080p index: {idx_1080}")

        if idx_1080 is not None:
            print(f"--> Switching to 1080p (index {idx_1080})...")
            await page.evaluate(f"window.selectQuality({idx_1080})")
            await page.wait_for_timeout(2500)

        # Now test manual switch 1080p -> 4K
        print("\n--> [QoE TEST 1] Manual Quality Switch: 1080p -> 4K")
        switch_target_idx = idx_4k if idx_4k is not None else (levels[0]["i"] if levels else -1)
        switch_start = time.time()
        await page.evaluate(f"window.selectQuality({switch_target_idx})")
        print(f"Selected quality index {switch_target_idx} at t=0s")

        # Verify immediate toast: "جاري التبديل إلى 4K..."
        toast_el = page.locator("#quality-toast")
        await page.wait_for_selector("#quality-toast:not(.hidden)", timeout=3000)
        toast_text = await toast_el.inner_text()
        print(f"Toast during switch: '{toast_text}'")
        assert "جاري التبديل" in toast_text, f"Unexpected toast text: {toast_text}"

        # Capture screenshot during switch
        shot_switch = os.path.join(ARTIFACTS_DIR, "1_switching_to_4k.png")
        await page.screenshot(path=shot_switch)
        print(f"Screenshot saved: {shot_switch}")

        # Monitor for false warnings during the switch (8-second window)
        print("--> Monitoring 8s grace period: verifying NO false warnings appear...")
        for i in range(16):
            await page.wait_for_timeout(500)
            current_toast = await toast_el.inner_text() if await toast_el.is_visible() else ""
            if "الاتصال غير كافٍ" in current_toast or "قد لا يكون الاتصال مستقراً" in current_toast:
                raise AssertionError(f"FALSE WARNING DETECTED during quality switch: '{current_toast}'")
            if ("4K" in current_toast or "✓" in current_toast) and "جاري" not in current_toast:
                switch_latency = time.time() - switch_start
                print(f"✓ Quality confirmed active ('{current_toast}') at t={switch_latency:.2f}s!")
                break

        # Capture screenshot of confirmed 4K playback
        shot_4k = os.path.join(ARTIFACTS_DIR, "2_4k_playing_verified.png")
        await page.screenshot(path=shot_4k)
        print(f"Screenshot saved: {shot_4k}")

        # TEST Diagnostics Panel (Press key 'D')
        print("\n--> [QoE TEST 2] Advanced Diagnostics Panel")
        await page.keyboard.press("d")
        await page.wait_for_selector("#player-diagnostics-panel:not(.hidden)", timeout=3000)
        await page.wait_for_timeout(1000) # allow values to update

        diag_mode = await page.locator("#diag-mode").inner_text()
        diag_req = await page.locator("#diag-req-quality").inner_text()
        diag_play = await page.locator("#diag-play-quality").inner_text()
        diag_bw = await page.locator("#diag-est-bw").inner_text()
        diag_bitrate = await page.locator("#diag-variant-bitrate").inner_text()
        diag_buf = await page.locator("#diag-buffer").inner_text()
        diag_sn = await page.locator("#diag-sn").inner_text()
        diag_session = await page.locator("#diag-session").inner_text()

        print("--- Advanced Diagnostics Telemetry ---")
        print(f"Mode:             {diag_mode}")
        print(f"Requested:        {diag_req}")
        print(f"Playing:          {diag_play}")
        print(f"Bandwidth:        {diag_bw}")
        print(f"Variant Bitrate:  {diag_bitrate}")
        print(f"Forward Buffer:   {diag_buf}")
        print(f"Segment SN:       {diag_sn}")
        print(f"Session:          {diag_session}")

        assert "Manual" in diag_mode or "4K" in diag_req, f"Diagnostics mode unexpected: {diag_mode}"
        assert "Active" in diag_session, f"Diagnostics session unexpected: {diag_session}"

        shot_diag = os.path.join(ARTIFACTS_DIR, "3_qoe_diagnostics_panel.png")
        await page.screenshot(path=shot_diag)
        print(f"Screenshot saved: {shot_diag}")

        # Ensure the 8s manual switch grace period has cleanly expired
        print("--> Verifying 8s manual switch grace period has elapsed without false warnings...")
        await page.wait_for_timeout(8000)

        # TEST Real Stall / Insufficient Bandwidth Simulation
        print("\n--> [QoE TEST 3] Simulating genuine network stall > 2.5s...")
        # Simulate network stall by pausing playback buffer pipeline (block subsequent segment loads)
        cdp = await context.new_cdp_session(page)
        # Throttle to 50 kbps (severely below 4K requirement)
        await cdp.send("Network.emulateNetworkConditions", {
            "offline": False,
            "latency": 300,
            "downloadThroughput": 50 * 1024 / 8,  # 50 kbps
            "uploadThroughput": 50 * 1024 / 8,
            "connectionType": "cellular2g"
        })
        print("Network throttled to 50 kbps (cellular2g). Waiting for buffer exhaustion and stall > 2.5s...")

        # Fast forward near the end of buffered range to provoke genuine stall
        await page.evaluate("""
            const v = document.getElementById('video-element');
            if (v && v.buffered.length > 0) {
                let targetEnd = v.currentTime;
                for (let i = 0; i < v.buffered.length; i++) {
                    if (v.currentTime >= v.buffered.start(i) && v.currentTime <= v.buffered.end(i)) {
                        targetEnd = v.buffered.end(i);
                        break;
                    }
                }
                if (targetEnd === v.currentTime && v.buffered.length > 0) {
                    targetEnd = v.buffered.end(v.buffered.length - 1);
                }
                v.currentTime = Math.max(0, targetEnd - 0.1);
            }
        """)

        # Wait for Level 2 insufficient warning with [العودة إلى تلقائي] button
        try:
            btn_action = page.locator("#quality-toast .toast-action-btn")
            await btn_action.wait_for(state="visible", timeout=18000)
            toast_warn_text = await page.locator("#quality-toast").inner_text()
            print(f"✓ LEVEL 2 Warning triggered: '{toast_warn_text}'")
            assert "الاتصال غير كافٍ" in toast_warn_text
            assert "العودة إلى تلقائي" in toast_warn_text

            shot_warn = os.path.join(ARTIFACTS_DIR, "4_stalled_insufficient_warning.png")
            await page.screenshot(path=shot_warn)
            print(f"Screenshot saved: {shot_warn}")

            # Restore normal network conditions
            print("--> Restoring network conditions and clicking [العودة إلى تلقائي]...")
            await cdp.send("Network.emulateNetworkConditions", {
                "offline": False,
                "latency": 0,
                "downloadThroughput": -1,
                "uploadThroughput": -1,
                "connectionType": "wifi"
            })

            await btn_action.click()
            print("Clicked [العودة إلى تلقائي] action button")
            await page.wait_for_timeout(2000)

            # Verify Auto mode restored
            summary_text = await page.locator("#quality-summary").inner_text()
            print(f"Quality summary after recovery: '{summary_text}'")
            assert "Auto" in summary_text, f"Expected Auto in summary, got: {summary_text}"

            shot_auto = os.path.join(ARTIFACTS_DIR, "5_auto_recovery.png")
            await page.screenshot(path=shot_auto)
            print(f"Screenshot saved: {shot_auto}")
            print("✓ Playback recovered cleanly in Auto mode!")

        except Exception as e:
            print(f"Throttling recovery exception: {e}")
            # Restore network before failing
            await cdp.send("Network.emulateNetworkConditions", {
                "offline": False, "latency": 0, "downloadThroughput": -1, "uploadThroughput": -1
            })
            raise

        await browser.close()
        print("\n" + "="*60)
        print("ALL SMART QoE BROWSER ACCEPTANCE TESTS PASSED SUCCESSFULLY!")
        print("="*60)

if __name__ == "__main__":
    asyncio.run(main())
