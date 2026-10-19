"""Empirical verification script for Embed & CDN features, hotlink protection, and embed-only mode."""
import os
import sys
import json
import httpx
import re

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE_URL = "http://127.0.0.1"
HEADERS = {"Host": "platform.example"}
SHARE_ID = "kEFx-MxuCSKg1VXk6x3GYw"  # Episode 05 READY

def main():
    print("=" * 70)
    print("TESTING PLATFORM EMBED & CDN ENGINE FEATURES")
    print("=" * 70)

    client = httpx.Client(base_url=BASE_URL, headers=HEADERS, timeout=15.0)

    # 1. Health & Readiness
    print("\n[Step 1] Checking /health and /readiness...")
    h = client.get("/health")
    assert h.status_code == 200, f"Health failed: {h.status_code}"
    r = client.get("/readiness")
    assert r.status_code == 200, f"Readiness failed: {r.status_code}"
    print("Health and Readiness: OK 200 ✓")

    # 2. Login as Admin
    print("\n[Step 2] Authenticating as Admin/Owner...")
    # First get login page to obtain CSRF cookie
    login_page = client.get("/admin/login")
    csrf_token = login_page.cookies.get("aboodvideo_csrf") or ""
    
    login_res = client.post(
        "/api/v1/auth/login",
        json={"username_or_email": os.environ["ABOODVIDEO_ACCEPTANCE_USERNAME"], "password": os.environ["ABOODVIDEO_ACCEPTANCE_PASSWORD"]},
        headers={"X-CSRF-Token": csrf_token}
    )
    print(f"Login Response: HTTP {login_res.status_code}")
    assert login_res.status_code == 200, f"Admin login failed: {login_res.text}"
    print("Admin authentication successful ✓")

    # Update CSRF token and session cookies
    cookie_parts = []
    for sc in login_res.headers.get_list("set-cookie"):
        cookie_parts.append(sc.split(";")[0])
    cookie_hdr = "; ".join(cookie_parts)
    client.headers["Cookie"] = cookie_hdr
    csrf_token = login_res.json().get("csrf_token") or client.cookies.get("aboodvideo_csrf") or csrf_token

    # 3. Read Embed Settings via Admin API
    print("\n[Step 3] Testing GET /api/v1/admin/embed-settings...")
    get_res = client.get("/api/v1/admin/embed-settings")
    print(f"GET /admin/embed-settings: HTTP {get_res.status_code}")
    assert get_res.status_code == 200, f"Failed GET embed settings: {get_res.text}"
    settings_data = get_res.json()
    print("Current Embed Settings:", json.dumps(settings_data, indent=2, ensure_ascii=False))

    # 4. Update Embed Settings via Admin API (PUT)
    print("\n[Step 4] Testing PUT /api/v1/admin/embed-settings...")
    put_payload = {
        "allowed_domains_raw": "partner.example, localhost, 127.0.0.1, partner-donghua.org",
        "embed_only_mode": True,
        "external_redirect_url": "https://partner.example",
        "default_color": "#2563eb",
        "default_watermark": "ABOODVIDEO",
        "default_skip_intro": 85,
        "pause_banner": {
            "enabled": True,
            "title": "قناة الدونغهوا الرسمية",
            "text": "اشترك لمتابعة الإصدارات الحصرية بجودة 4K",
            "link": "https://partner.example"
        }
    }
    put_res = client.put(
        "/api/v1/admin/embed-settings",
        json=put_payload,
        headers={"X-CSRF-Token": csrf_token}
    )
    print(f"PUT /admin/embed-settings: HTTP {put_res.status_code}")
    assert put_res.status_code == 200, f"Failed PUT embed settings: {put_res.text}"
    saved_data = put_res.json()
    assert saved_data["embed_only_mode"] is True
    assert "partner-donghua.org" in saved_data["allowed_domains"]
    print("Embed Settings updated and verified successfully ✓")

    # 5. Hotlink Protection & Whitelisting Tests
    print("\n[Step 5] Testing Hotlink Protection & Domain Whitelisting...")
    embed_url = f"/embed/{SHARE_ID}"

    # 5a. Allowed domain: partner.example
    r_allowed1 = client.get(embed_url, headers={"Referer": "https://partner.example/episode/spirit-cage-5"})
    print(f"   Request from partner.example: HTTP {r_allowed1.status_code}")
    assert r_allowed1.status_code == 200, f"Expected 200, got {r_allowed1.status_code}"

    # 5b. Newly added allowed domain: partner-donghua.org
    r_allowed2 = client.get(embed_url, headers={"Referer": "https://partner-donghua.org/watch/5"})
    print(f"   Request from partner-donghua.org: HTTP {r_allowed2.status_code}")
    assert r_allowed2.status_code == 200, f"Expected 200, got {r_allowed2.status_code}"

    # 5c. Blocked pirate domain: pirate-site.xyz
    r_blocked = client.get(embed_url, headers={"Referer": "https://pirate-site.xyz/watch/pirated"})
    print(f"   Request from pirate-site.xyz: HTTP {r_blocked.status_code}")
    assert r_blocked.status_code == 403, f"Expected 403 Forbidden, got {r_blocked.status_code}"
    assert "غير مصرح بتضمين المشغل" in r_blocked.text
    assert "pirate-site.xyz" in r_blocked.text
    print("   Hotlink shield active: unauthorized pirate site blocked with Arabic 403 screen ✓")

    # 6. Embed-Only Mode (Blocking Direct Platform Watching)
    print("\n[Step 6] Testing Embed-Only Mode (Direct /watch/ Access)...")
    # Using an unauthenticated client to simulate a direct visitor
    unauth_client = httpx.Client(base_url=BASE_URL, headers=HEADERS, timeout=15.0, follow_redirects=False)
    r_direct = unauth_client.get(f"/watch/{SHARE_ID}")
    print(f"   Direct unauthenticated visit to /watch/{SHARE_ID}: HTTP {r_direct.status_code}")
    print(f"   Location Header: {r_direct.headers.get('Location')}")
    assert r_direct.status_code == 307, f"Expected 307 redirect, got {r_direct.status_code}"
    assert r_direct.headers.get("Location") == "https://partner.example"
    print("   Embed-Only Mode successfully redirected direct visitor to partner site ✓")

    # 7. Admin can still view preview
    print("\n[Step 7] Testing Admin preview bypass for /watch/{SHARE_ID}...")
    r_admin_watch = client.get(f"/watch/{SHARE_ID}")
    print(f"   Admin visit to /watch/{SHARE_ID}: HTTP {r_admin_watch.status_code}")
    assert r_admin_watch.status_code == 200
    print("   Admin can preview /watch/ freely ✓")

    # 8. Check Default Pause Banner in Embed Page
    print("\n[Step 8] Checking Default Styling & Pause Banner in Embed...")
    r_embed_page = client.get(embed_url, headers={"Referer": "https://partner.example"})
    assert r_embed_page.status_code == 200
    html_text = r_embed_page.text
    assert "pause-banner-card" in html_text
    assert "قناة الدونغهوا الرسمية" in html_text
    assert "#2563eb" in html_text
    print("   Default accent color #2563eb and default pause banner verified in rendered HTML ✓")

    print("\n" + "=" * 70)
    print("ALL PLATFORM EMBED & CDN FEATURES VERIFIED WITH 100% EMPIRICAL SUCCESS!")
    print("=" * 70)

if __name__ == "__main__":
    main()
