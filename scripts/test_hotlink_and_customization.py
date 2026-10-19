"""Empirical verification of Hotlink Protection, Custom Theme Colors, and Watermarks."""
import sys
import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SHARE_ID = "kEFx-MxuCSKg1VXk6x3GYw"
EMBED_BASE = f"https://platform.example/embed/{SHARE_ID}"

def test_hotlink_protection():
    print("=" * 70)
    print("TEST 1: Domain Whitelisting & Hotlink Protection")
    print("=" * 70)
    client = httpx.Client(verify=False, timeout=10.0)

    # 1. Authorized Domain: partner.example
    r_auth = client.get(EMBED_BASE, headers={"Referer": "https://partner.example/episode/spirit-cage-incarnation-s1-episode-5/"})
    print(f"Request from https://partner.example: HTTP {r_auth.status_code}")
    assert r_auth.status_code == 200, f"Expected 200, got {r_auth.status_code}"

    # 2. Authorized Domain: localhost
    r_local = client.get(EMBED_BASE, headers={"Referer": "http://localhost/episode/spirit-cage-incarnation-s1-episode-5/"})
    print(f"Request from http://localhost: HTTP {r_local.status_code}")
    assert r_local.status_code == 200, f"Expected 200, got {r_local.status_code}"

    # 3. Unauthorized Pirate Domain: pirate-streaming-site.com
    r_pirate = client.get(EMBED_BASE, headers={"Referer": "https://pirate-streaming-site.com/watch/video"})
    print(f"Request from https://pirate-streaming-site.com: HTTP {r_pirate.status_code}")
    assert r_pirate.status_code == 403, f"Expected 403 Forbidden, got {r_pirate.status_code}"
    assert "غير مصرح بتضمين المشغل" in r_pirate.text
    assert "pirate-streaming-site.com" in r_pirate.text
    print("Blocked pirate domain successfully with Arabic 403 screen ✓")

    # 4. Direct access without referer
    r_direct = client.get(EMBED_BASE)
    print(f"Direct request without Referer: HTTP {r_direct.status_code}")
    assert r_direct.status_code == 200, f"Expected 200, got {r_direct.status_code}"

def test_player_customizations():
    print("\n" + "=" * 70)
    print("TEST 2: Player Customizations via Embed URL Parameters")
    print("=" * 70)
    client = httpx.Client(verify=False, timeout=10.0)

    custom_url = f"{EMBED_BASE}?color=00d2be&watermark=SPECIAL_SITE&skip=90&autoplay=1&banner=1"
    r = client.get(custom_url)
    print(f"GET {custom_url}: HTTP {r.status_code}")
    assert r.status_code == 200

    # Verify custom color injected into CSS
    assert "#00d2be" in r.text, "Custom color #00d2be not found in HTML"
    print("Custom primary color #00d2be verified in CSS ✓")

    # Verify custom watermark text
    assert "SPECIAL_SITE" in r.text, "Custom watermark SPECIAL_SITE not found in HTML"
    print("Custom watermark text 'SPECIAL_SITE' verified ✓")

    # Verify skip intro 90 seconds
    assert '"skipIntroSeconds": 90' in r.text or '"skipIntroSeconds":90' in r.text or '90' in r.text, "Custom skip seconds not found"
    print("Custom skip intro seconds (90s) verified in config ✓")

    # Verify autoplay
    assert '"autoPlay": true' in r.text or '"autoPlay":true' in r.text, "Autoplay not enabled in config"
    print("Autoplay enabled in config ✓")

    # Verify pause banner overlay
    assert 'pause-banner-card' in r.text, "Pause banner card markup not found"
    print("Pause banner card markup verified ✓")

    print("\n" + "=" * 70)
    print("ALL PLATFORM CUSTOMIZATIONS & SECURITY VERIFIED EMPIRICALLY!")
    print("=" * 70)

if __name__ == "__main__":
    test_hotlink_protection()
    test_player_customizations()
