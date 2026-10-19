"""Live Playback Security and Entitlement Acceptance Tests for ABOODVIDEO."""
import re
import urllib.request
import urllib.error
import urllib.parse
import sys
sys.stdout.reconfigure(encoding='utf-8')

BASE_URL = "http://127.0.0.1"
WATCH_URL = f"{BASE_URL}/watch/yGWgWuIX_d6QFeKy4l-22Q"

print(f"--> [STEP 1] Fetching watch page: {WATCH_URL}")
req = urllib.request.Request(WATCH_URL)
with urllib.request.urlopen(req) as resp:
    headers = resp.headers
    raw_cookie = headers.get("Set-Cookie")
    html = resp.read().decode("utf-8")

print(f"Watch page status: {resp.status}")
print(f"Set-Cookie header: {raw_cookie}")

assert raw_cookie, "Set-Cookie header missing from watch page response"
assert "abood_playback=" in raw_cookie, "abood_playback cookie missing"
assert "HttpOnly" in raw_cookie, "HttpOnly flag missing"
assert "SameSite=Lax" in raw_cookie, "SameSite=Lax missing"

# Extract cookie value
cookie_val = None
for part in raw_cookie.split(";"):
    part = part.strip()
    if part.startswith("abood_playback="):
        cookie_val = part
        break

print(f"Extracted cookie: {cookie_val}")

# Extract session token from HTML
m = re.search(r'"playbackToken":\s*"([^"]+)"', html)
assert m, "playbackToken not found in player-config JSON"
playback_token = m.group(1)
print(f"Extracted playbackToken: {playback_token}")
assert playback_token.startswith("s/"), f"Expected session token starting with 's/', got: {playback_token}"

master_url = f"{BASE_URL}/api/v1/playback/{playback_token}/master.m3u8"
print(f"Master playlist URL: {master_url}")

# TEST A: Master request WITHOUT cookie (Copied Master URL outside session)
print("\n--> [TEST A] Copied Master URL without cookie")
try:
    req_no_cookie = urllib.request.Request(master_url)
    urllib.request.urlopen(req_no_cookie)
    print("❌ FAILED: Master request without cookie returned 200 OK (expected 401)")
    sys.exit(1)
except urllib.error.HTTPError as e:
    print(f"✓ PASSED: Copied master URL returned HTTP {e.code} (Reason: {e.reason})")
    assert e.code in (401, 403), f"Expected 401 or 403, got {e.code}"

# TEST B: Master request WITH valid cookie
print("\n--> [TEST B] Authorized Master URL with session cookie")
req_with_cookie = urllib.request.Request(master_url)
req_with_cookie.add_header("Cookie", cookie_val)
with urllib.request.urlopen(req_with_cookie) as resp:
    assert resp.status == 200, f"Expected 200, got {resp.status}"
    master_content = resp.read().decode("utf-8")
    print(f"✓ PASSED: Master playlist retrieved ({len(master_content)} bytes)")
    print("Master content preview:")
    for line in master_content.splitlines()[:10]:
        print(f"   {line}")

# Find variant playlist URI
variants = [l for l in master_content.splitlines() if l and not l.startswith("#")]
assert variants, "No variant streams found in master playlist"
first_variant = variants[0]
variant_url = urllib.parse.urljoin(master_url, first_variant)
print(f"\nTarget variant URL: {variant_url}")

# TEST C: Variant request WITHOUT cookie
print("\n--> [TEST C] Copied Variant URL without cookie")
try:
    req_var_no = urllib.request.Request(variant_url)
    urllib.request.urlopen(req_var_no)
    print("❌ FAILED: Variant request without cookie returned 200 OK (expected 401)")
    sys.exit(1)
except urllib.error.HTTPError as e:
    print(f"✓ PASSED: Copied variant URL returned HTTP {e.code} (Reason: {e.reason})")
    assert e.code in (401, 403)

# TEST D: Variant request WITH cookie
print("\n--> [TEST D] Authorized Variant URL with session cookie")
req_var_with = urllib.request.Request(variant_url)
req_var_with.add_header("Cookie", cookie_val)
with urllib.request.urlopen(req_var_with) as resp:
    assert resp.status == 200
    variant_content = resp.read().decode("utf-8")
    print(f"✓ PASSED: Variant playlist retrieved ({len(variant_content)} bytes)")

# Find first segment
segments = [l for l in variant_content.splitlines() if l.endswith(".ts") or ".ts?" in l]
assert segments, "No segments found in variant playlist"
first_segment = segments[0]
segment_url = urllib.parse.urljoin(variant_url, first_segment)
print(f"\nTarget segment URL: {segment_url}")

# TEST E: Segment request WITHOUT cookie (Copied Segment URL)
print("\n--> [TEST E] Copied Segment URL without cookie")
try:
    req_seg_no = urllib.request.Request(segment_url)
    urllib.request.urlopen(req_seg_no)
    print("❌ FAILED: Segment request without cookie returned 200 OK (expected 401)")
    sys.exit(1)
except urllib.error.HTTPError as e:
    print(f"✓ PASSED: Copied segment URL returned HTTP {e.code} (Reason: {e.reason})")
    assert e.code in (401, 403)

# TEST F: Segment request WITH cookie (Authorized Segment Delivery via X-Accel-Redirect)
print("\n--> [TEST F] Authorized Segment delivery with session cookie")
req_seg_with = urllib.request.Request(segment_url)
req_seg_with.add_header("Cookie", cookie_val)
with urllib.request.urlopen(req_seg_with) as resp:
    assert resp.status == 200
    seg_bytes = resp.read()
    print(f"✓ PASSED: Segment delivered successfully ({len(seg_bytes)} bytes, content-type: {resp.headers.get('Content-Type')})")
    assert len(seg_bytes) > 100000, "Segment size is unexpectedly small"

# TEST G: Copied Subtitle file without cookie
print("\n--> [TEST G] Copied Subtitle URL without cookie")
# Check if subtitle URL is protected
fake_sub_url = f"{BASE_URL}/api/v1/playback/{playback_token}/subtitles/00000000-0000-0000-0000-000000000000/file"
try:
    req_sub_no = urllib.request.Request(fake_sub_url)
    urllib.request.urlopen(req_sub_no)
    print("❌ FAILED: Subtitle request without cookie succeeded")
    sys.exit(1)
except urllib.error.HTTPError as e:
    print(f"✓ PASSED: Subtitle request without cookie returned HTTP {e.code} (Reason: {e.reason})")
    assert e.code in (401, 403)

# TEST H: Key endpoint protection
print("\n--> [TEST H] Key endpoint without cookie")
key_url = f"{BASE_URL}/api/v1/playback/{playback_token}/key"
try:
    req_key_no = urllib.request.Request(key_url)
    urllib.request.urlopen(req_key_no)
    print("❌ FAILED: Key request without cookie succeeded")
    sys.exit(1)
except urllib.error.HTTPError as e:
    print(f"✓ PASSED: Key request without cookie returned HTTP {e.code}")
    assert e.code in (401, 403)

# TEST I: Session Revocation Test (Requirement 18.F)
print("\n--> [TEST I] Session Revocation Test")
import subprocess
sid = playback_token.split("/", 1)[1]
print(f"Revoking session in Redis: sid={sid}")
cmd = f'ssh abood@127.0.0.1 "docker exec aboodvideo_redis redis-cli -n 1 del pb:sess:{sid}"'
subprocess.run(cmd, shell=True, check=True)

# Now request segment WITH cookie - should now FAIL with 401
try:
    req_revoked = urllib.request.Request(segment_url)
    req_revoked.add_header("Cookie", cookie_val)
    urllib.request.urlopen(req_revoked)
    print("❌ FAILED: Request after session revocation SUCCEEDED (expected 401)")
    sys.exit(1)
except urllib.error.HTTPError as e:
    print(f"✓ PASSED: Request after session revocation returned HTTP {e.code} (Reason: {e.reason})")
    assert e.code in (401, 403)

# TEST J: Quality Entitlement Cap (Requirement 5: 1080p user requesting 4K -> 403)
print("\n--> [TEST J] Quality Entitlement Cap Check")
res = subprocess.run(
    ["ssh", "abood@127.0.0.1", "docker exec aboodvideo_api python /app/test_capped_session.py"],
    capture_output=True, text=True, check=True
)
cap_sid, cap_cookie_raw = res.stdout.strip().split("|", 1)
cap_cookie = cap_cookie_raw.split(";")[0].strip()
print(f"Created capped session: sid={cap_sid}, max_quality=1080p, cookie={cap_cookie}")

# Try to request 2160p variant with this 1080p capped session
url_4k = f"{BASE_URL}/api/v1/playback/s/{cap_sid}/2160p/index.m3u8"
try:
    req_4k = urllib.request.Request(url_4k)
    req_4k.add_header("Cookie", cap_cookie)
    urllib.request.urlopen(req_4k)
    print("❌ FAILED: 4K request with 1080p capped session SUCCEEDED (expected 403)")
    sys.exit(1)
except urllib.error.HTTPError as e:
    print(f"✓ PASSED: 4K request for 1080p-capped session returned HTTP {e.code} Forbidden (Reason: {e.reason})")
    assert e.code == 403

# Verify that 1080p IS allowed for this session
url_1080p = f"{BASE_URL}/api/v1/playback/s/{cap_sid}/1080p/index.m3u8"
try:
    req_1080 = urllib.request.Request(url_1080p)
    req_1080.add_header("Cookie", cap_cookie)
    with urllib.request.urlopen(req_1080) as resp:
        print(f"✓ PASSED: 1080p request for 1080p-capped session returned HTTP {resp.status} OK")
        assert resp.status == 200
except Exception as e:
    print(f"❌ FAILED: 1080p request failed: {e}")
    sys.exit(1)

print("\n" + "="*60)
print("ALL LIVE PLAYBACK SECURITY & ENTITLEMENT TESTS PASSED SUCCESSFULLY!")
print("="*60)

