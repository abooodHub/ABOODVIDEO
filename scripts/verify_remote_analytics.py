"""Empirical remote verification for Analytics, Watermark, and Inspector APIs."""
import sys
import json
import httpx
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE_URL = "http://127.0.0.1"
HEADERS = {"Host": "platform.example"}

def main():
    print("=" * 60)
    print("Verifying Remote Production Platform Analytics & Features")
    print("=" * 60)

    client = httpx.Client(base_url=BASE_URL, headers=HEADERS, timeout=10.0)

    # 1. Health & Readiness
    h = client.get("/health")
    print(f"1. Health Check:     HTTP {h.status_code} -> {h.json().get('status')}")
    r = client.get("/readiness")
    print(f"2. Readiness Check:  HTTP {r.status_code} -> DB={r.json().get('database')} Redis={r.json().get('redis')}")

    # 3. Verify Embed Page has Watermark, Skip Intro, and Next Episode Card
    share_id = "oZ-3bk9v-pIGnu_TEdA1lw"
    print(f"\n3. Checking Embed HTML for UX & Player Enhancements for share_id {share_id}...")
    embed_page = client.get(f"/embed/{share_id}")
    print(f"   GET /embed/{share_id}: HTTP {embed_page.status_code}")
    embed_html = embed_page.text
    has_skip = "btn-skip-intro" in embed_html
    has_wm = "player-watermark" in embed_html
    has_next = "next-episode-card" in embed_html
    print(f"   - Skip Intro Button:    {'FOUND ✓' if has_skip else 'MISSING ✗'}")
    print(f"   - Watermark Overlay:    {'FOUND ✓' if has_wm else 'MISSING ✗'}")
    print(f"   - Next Episode Card:    {'FOUND ✓' if has_next else 'MISSING ✗'}")

    # 4. Simulate an authentic embed playback heartbeat with analytics telemetry
    print(f"\n4. Sending Test Playback Heartbeat...")
    import re
    match = re.search(r'["\']?playbackToken["\']?\s*:\s*["\']([^"\']+)["\']', embed_html)
    playback_token = match.group(1) if match else None
    print(f"   Extracted Token:  {playback_token}")

    if playback_token:
        raw_cookie = embed_page.headers.get("set-cookie", "").split(";")[0]
        # Send heartbeat with telemetry: 1080p, desktop, 15MB bandwidth
        hb_res = client.post(
            f"/api/v1/playback/{playback_token}/event",
            headers={"Cookie": raw_cookie},
            json={
                "event": "heartbeat",
                "position": 95.0,
                "duration": 25.0,
                "bytes_received": 15728640, # 15 MB
                "quality": "1080p",
                "device_type": "desktop",
                "referrer": "https://partner.example/watch/ling-cage-ep-3"
            }
        )
        print(f"   Heartbeat Event:  HTTP {hb_res.status_code} -> {hb_res.text}")

    print("\n" + "=" * 60)
    print("Verification Completed!")
    print("=" * 60)

if __name__ == "__main__":
    main()
