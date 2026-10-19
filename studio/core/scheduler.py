"""Resource-bounded scheduling; do not queue work beyond available slots."""
import time
import logging
from concurrent.futures import ThreadPoolExecutor


def run_resource_scheduler(pending, process, classify, limits, stopped):
    claimed, active, results = set(), {}, {}
    with ThreadPoolExecutor(max_workers=sum(limits.values()), thread_name_prefix="StudioEncode") as pool:
        while True:
            for future, (episode, resource) in list(active.items()):
                if future.done():
                    try:
                        results[episode["id"]] = future.result()
                    except Exception:
                        logging.getLogger(__name__).exception("Scheduled episode failed: %s", episode["id"])
                        results[episode["id"]] = False
                    del active[future]
            halted = stopped() or any(result is False for result in results.values())
            remaining = [ep for ep in pending() if ep["id"] not in claimed]
            if not halted:
                for episode in remaining:
                    resource = classify(episode)
                    if sum(kind == resource for _, kind in active.values()) >= limits[resource]:
                        continue
                    if stopped():
                        break
                    claimed.add(episode["id"])
                    active[pool.submit(process, episode)] = (episode, resource)
            if not active and (halted or not remaining):
                return results
            time.sleep(0.02)
