"""Verify Episode 04 Playback via Embed & Authenticated HLS Stream."""
import sys
import re
import httpx

SHARE_ID = "cw9xxQR2juDGQmRU9jESQw"

def main():
    print(f"Testing Episode 04 playback for share_id: {SHARE_ID}")
    client = httpx.Client(verify=False, timeout=15.0)

    # 1. Load embed page
    embed_url = f"https://platform.example/embed/{SHARE_ID}"
    r = client.get(embed_url)
    print(f"Embed Status: HTTP {r.status_code}")
    assert r.status_code == 200, f"Embed failed with {r.status_code}"

    # Extract playbackUrl
    m = re.search(r'"playbackUrl":\s*"([^"]+)"', r.text)
    if not m:
        print("ERROR: playbackUrl not found in embed HTML!")
        sys.exit(1)
    
    playback_path = m.group(1)
    print(f"Playback Path: {playback_path}")

    # Extract cookie
    cookie_header = {}
    for c_name, c_val in client.cookies.items():
        if "playback" in c_name:
            cookie_header["Cookie"] = f"{c_name}={c_val}"
            print(f"Captured Cookie: {c_name}")

    # 2. Request master.m3u8
    m3u8_url = f"https://platform.example{playback_path}"
    r_m3u8 = client.get(m3u8_url, headers=cookie_header)
    print(f"Master Playlist Status: HTTP {r_m3u8.status_code}")
    assert r_m3u8.status_code == 200, f"m3u8 failed with {r_m3u8.status_code}"
    
    print("\nMaster Playlist Content:")
    renditions = []
    for line in r_m3u8.text.splitlines():
        if "RESOLUTION=" in line or line.endswith(".m3u8"):
            print("  ", line)
            if line.endswith(".m3u8"):
                renditions.append(line)

    # 3. Test a rendition playlist (e.g. 1080p or 480p)
    if renditions:
        first_rendition = renditions[0]
        base_dir = playback_path.rsplit("/", 1)[0]
        rendition_url = f"https://platform.example{base_dir}/{first_rendition}"
        r_rend = client.get(rendition_url, headers=cookie_header)
        print(f"\nRendition ({first_rendition}) Status: HTTP {r_rend.status_code}")
        assert r_rend.status_code == 200, f"Rendition failed with {r_rend.status_code}"
        
        # Test first .ts segment
        ts_lines = [l for l in r_rend.text.splitlines() if l.endswith(".ts") or l.endswith(".m4s")]
        if ts_lines:
            ts_file = ts_lines[0]
            ts_url = f"https://platform.example{base_dir}/{first_rendition.rsplit('/', 1)[0]}/{ts_file}"
            r_ts = client.get(ts_url, headers=cookie_header)
            print(f"Segment ({ts_file}) Status: HTTP {r_ts.status_code} ({len(r_ts.content)} bytes)")
            assert r_ts.status_code == 200, f"Segment failed with {r_ts.status_code}"

    print("\n" + "=" * 60)
    print("EPISODE 04 PLAYBACK VERIFIED EMPIRICALLY!")
    print(f"Share ID: {SHARE_ID}")
    print("=" * 60)

if __name__ == "__main__":
    main()
