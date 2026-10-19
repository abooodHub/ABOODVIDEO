import requests
import time
import re

session = requests.Session()

# 1. Fetch watch page to get cookie & session URL
t0 = time.perf_counter()
r_watch = session.get('http://127.0.0.1/watch/yGWgWuIX_d6QFeKy4l-22Q')
t_watch = (time.perf_counter() - t0) * 1000

m = re.search(r'"playbackToken":\s*"([^"]+)"', r_watch.text)
sid = m.group(1) if m else None
print(f"Watch Page Latency: {t_watch:.2f}ms, Session: {sid}")

# 2. Measure Master Playlist Authorization Latency
master_url = f"http://127.0.0.1/api/v1/playback/{sid}/master.m3u8"
latencies_master = []
for _ in range(5):
    t0 = time.perf_counter()
    r = session.get(master_url)
    latencies_master.append((time.perf_counter() - t0) * 1000)
avg_master = sum(latencies_master) / len(latencies_master)
print(f"Master Playlist Latency (avg 5): {avg_master:.2f}ms (min: {min(latencies_master):.2f}ms)")

# 3. Find variant & measure Variant Playlist Latency
variant_url = f"http://127.0.0.1/api/v1/playback/{sid}/2160p/index.m3u8"
latencies_var = []
for _ in range(5):
    t0 = time.perf_counter()
    r = session.get(variant_url)
    latencies_var.append((time.perf_counter() - t0) * 1000)
avg_var = sum(latencies_var) / len(latencies_var)
print(f"Variant Playlist Latency (avg 5): {avg_var:.2f}ms (min: {min(latencies_var):.2f}ms)")

# 4. Find first segment & measure Segment Delivery Latency + Throughput
seg_match = re.search(r"([a-zA-Z0-9_\-]+\.ts)", r.text)
if seg_match:
    seg_name = seg_match.group(1)
    seg_url = f"http://127.0.0.1/api/v1/playback/{sid}/2160p/{seg_name}"
    latencies_seg = []
    throughputs = []
    for _ in range(5):
        t0 = time.perf_counter()
        r_seg = session.get(seg_url)
        dt = time.perf_counter() - t0
        latencies_seg.append(dt * 1000)
        bytes_len = len(r_seg.content)
        mbps = (bytes_len * 8) / (dt * 1_000_000)
        throughputs.append(mbps)
    avg_seg = sum(latencies_seg) / len(latencies_seg)
    avg_mbps = sum(throughputs) / len(throughputs)
    print(f"Segment Delivery Latency (avg 5): {avg_seg:.2f}ms (min: {min(latencies_seg):.2f}ms)")
    print(f"Segment Throughput: {avg_mbps:.2f} Mbps (segment size: {bytes_len/1024/1024:.2f} MB)")
