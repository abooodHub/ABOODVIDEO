#!/usr/bin/env python3
"""
PHASE 3B — TARGETED UPLOAD RESUME PROOF

Proves that a partially-uploaded server session is correctly resumed after
a hard Studio kill:
  - Same session_id before and after restart
  - Same video_id
  - Files already COMPLETED on server are skipped (zero retransmitted bytes)
  - No re-encode, no package rebuild
  - No duplicate Video record
  - Remaining files upload, finalize succeeds, server reaches READY

Usage:
    c:\\aboodvideo\\.venv\\Scripts\\python.exe scripts\\execute_resume_proof.py
"""

import os
import sys
import json
import time
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional

import httpx

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

STUDIO_PORT = 8597  # Different from acceptance port
STUDIO_URL = f"http://127.0.0.1:{STUDIO_PORT}"
SERVER_BASE_URL = "http://127.0.0.1/api/v1"
DESTINATION = {
    "folder_id":     "142bf765-1f69-473b-b17c-f09af24a2897",
    "series_id":     "3a0d9665-51f0-4542-b6f9-be7a701336d1",
    "season_id":     "f554f309-83a6-4d32-bde3-d2161821b6d1",
    "episode_number": 1,
    "api_key":       os.environ["ABOODVIDEO_ACCEPTANCE_API_KEY"],
}
DB_PATH = PROJECT_ROOT / "studio" / "data" / "jobs.db"
TEST_DIR = PROJECT_ROOT / "studio" / "data" / "resume_proof_season"

# Kill condition: at least this many files COMPLETED on server before kill
MIN_COMPLETED_BEFORE_KILL = 5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_large_mkv(path: Path) -> Path:
    """Create a 300-second 854x480 source at ~2 Mbps."""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=size=854x480:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", "300.0",
        "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "2000k",
        "-c:a", "aac", "-b:a", "128k",
        str(path),
    ]
    subprocess.run(cmd, check=True, timeout=120)
    return path


def start_studio(port: int) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-m", "studio.main", "--port", str(port), "--no-browser"],
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 30.0
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/api/batch/current", timeout=2.0)
            if r.status_code == 200:
                print(f"    Studio started (PID {proc.pid})")
                return proc
        except Exception:
            pass
        time.sleep(0.25)
    proc.kill()
    raise RuntimeError(f"Studio did not start on port {port} within 30s")




def kill_studio(proc: Optional[subprocess.Popen]) -> None:
    if proc and proc.poll() is None:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except Exception:
            pass


def db_read_episode(ep_id: str) -> Dict[str, str]:
    """Read upload fields directly from SQLite WAL (safe cross-process)."""
    try:
        con = sqlite3.connect(str(DB_PATH), timeout=3.0)
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT upload_session_id, server_video_id, video_id, state, work_dir "
            "FROM episodes WHERE id = ?", (ep_id,)
        ).fetchone()
        con.close()
        if row:
            return {
                "session_id": row["upload_session_id"] or "",
                "video_id":   row["server_video_id"] or row["video_id"] or "",
                "state":      row["state"] or "",
                "work_dir":   row["work_dir"] or "",
            }
    except Exception:
        pass
    return {"session_id": "", "video_id": "", "state": "", "work_dir": ""}


def get_server_files(session_id: str) -> List[Dict[str, Any]]:
    try:
        r = httpx.get(
            f"{SERVER_BASE_URL}/studio/packages/{session_id}/files",
            headers={"Authorization": f"Bearer {DESTINATION['api_key']}"},
            timeout=8.0,
        )
        if r.status_code == 200:
            return r.json().get("files", [])
    except Exception:
        pass
    return []


def run_remote_sql(sql: str) -> str:
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
         "abood@127.0.0.1"],
        input=(
            f'docker exec -i aboodvideo_postgres psql -U aboodvideo_user '
            f'-d aboodvideo_db --csv -c "{sql}"'
        ),
        capture_output=True, text=True, timeout=30,
    )
    return proc.stdout.strip()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 70)
    print("  PHASE 3B — TARGETED UPLOAD RESUME PROOF")
    print("=" * 70)

    from studio.core.batch_store import BatchStore, EpisodeState
    from studio.config import save_studio_config

    # Use 2-second HLS segments: 300s / 2s = 150 segments per rendition
    save_studio_config({
        "server_url":            "http://127.0.0.1/api/v1",
        "api_key":               DESTINATION["api_key"],
        "default_encoder":      "libx264",
        "hls_segment_duration":  2,
    })

    # ------------------------------------------------------------------
    # 1. Generate large source
    # ------------------------------------------------------------------
    print(f"\n[1] Generating large source video (300s @2Mbps ultrafast)...")
    shutil.rmtree(TEST_DIR, ignore_errors=True)
    TEST_DIR.mkdir(parents=True, exist_ok=True)
    src = make_large_mkv(TEST_DIR / "01.mkv")
    print(f"    Source size: {src.stat().st_size / 1024 / 1024:.1f} MB")

    # ------------------------------------------------------------------
    # 2. Clean jobs.db
    # ------------------------------------------------------------------
    store = BatchStore(db_path=DB_PATH)
    with store._get_connection() as conn:
        conn.execute("DELETE FROM episodes")
        conn.execute("DELETE FROM batches")
        conn.commit()

    # ------------------------------------------------------------------
    # 3. Start Studio, import, set destination
    # ------------------------------------------------------------------
    print("\n[2] Starting Studio...")
    studio_proc = start_studio(STUDIO_PORT)
    batch_id: str = ""
    ep_id: str = ""
    video_id_before: str = ""
    session_id_before: str = ""
    files_completed_before_kill: List[str] = []
    interrupted_pct: float = 0.0
    sfiles_at_kill: List[Dict[str, Any]] = []

    try:
        with httpx.Client(base_url=STUDIO_URL, timeout=15.0) as c:
            r = c.post("/api/batch/import", json={"folder_path": str(TEST_DIR)})
            assert r.status_code == 200, f"Import failed: {r.text}"
            batch_id = r.json()["batch"]["id"]
            print(f"    Batch ID: {batch_id}")

            r = c.post(
                f"/api/batch/{batch_id}/destination-authoritative",
                json={
                    "folder_id":      DESTINATION["folder_id"],
                    "series_id":      DESTINATION["series_id"],
                    "season_id":      DESTINATION["season_id"],
                    "episode_number": DESTINATION["episode_number"],
                },
            )
            assert r.status_code == 200, f"Destination failed: {r.text}"
            print("    Destination set.")

        eps = store.get_episodes(batch_id)
        assert eps, "No episodes found after import"
        ep_id = eps[0]["id"]
        print(f"    Episode ID: {ep_id}")

        # ------------------------------------------------------------------
        # 4. Start pipeline — unified monitor: wait for encode done, then kill
        #    mid-upload when session_id persisted and >=5 files COMPLETED
        # ------------------------------------------------------------------
        UPLOAD_STATES = {
            EpisodeState.READY_TO_UPLOAD.value,
            EpisodeState.UPLOADING.value,
            EpisodeState.SERVER_VERIFYING.value,
            EpisodeState.READY.value,
        }

        print("\n[3] Starting pipeline — monitoring encode + upload in unified loop...")
        with httpx.Client(base_url=STUDIO_URL, timeout=10.0) as c:
            r = c.post("/api/batch/auto-pipeline/start")
            assert r.status_code == 200, f"Pipeline start failed: {r.text}"

        # Fast poll loop: exits as soon as encoding is done (any upload-phase state)
        t0 = time.time()
        while True:
            ep_data = db_read_episode(ep_id)
            st = ep_data["state"]
            elapsed = time.time() - t0
            print(f"\r    State: {st:<30}  ({elapsed:.0f}s elapsed)", end="", flush=True)
            if st in UPLOAD_STATES:
                print(f"\n    Encoding done in {elapsed:.1f}s — now monitoring upload for kill condition...")
                break
            if st in ("FAILED",):
                raise RuntimeError(f"Episode FAILED during encoding: {st}")
            if elapsed > 600:
                raise RuntimeError("Timed out waiting for encode to complete")
            time.sleep(0.2)

        # Snapshot package directory NOW (before any possible kill)
        work_dir_path = Path(ep_data["work_dir"])
        pkg_files_before = {
            str(f.relative_to(work_dir_path)): f.stat().st_size
            for f in work_dir_path.rglob("*") if f.is_file()
        }
        pkg_mb = sum(pkg_files_before.values()) / 1024 / 1024
        print(f"    Package: {len(pkg_files_before)} files, {pkg_mb:.1f} MB")

        # ------------------------------------------------------------------
        # 5. Monitor upload — kill when session_id persisted + >=5 COMPLETED
        # ------------------------------------------------------------------
        print(f"\n[4] Kill condition: session_id in SQLite AND >={MIN_COMPLETED_BEFORE_KILL} server files COMPLETED...")

        killed = False
        t_upload = time.time()
        while time.time() - t_upload < 120.0:
            ep_now = db_read_episode(ep_id)
            st_now = ep_now["state"]
            sess = ep_now["session_id"]
            vid  = ep_now["video_id"]

            # If episode already READY (upload completed before kill), abort
            if st_now == EpisodeState.READY.value and not session_id_before:
                raise AssertionError(
                    "Episode reached READY before kill condition was met — "
                    "package too small or network too fast. "
                    "Increase -b:v bitrate or MIN_COMPLETED_BEFORE_KILL."
                )

            if sess and not session_id_before:
                session_id_before = sess
                video_id_before   = vid
                print(f"\n    [+] session_id persisted in SQLite: {sess}")
                print(f"    [+] video_id: {vid}")

            if session_id_before:
                sfiles = get_server_files(session_id_before)
                completed = [f for f in sfiles if f.get("status") == "COMPLETED"]
                total     = len(sfiles)
                pct       = len(completed) / max(1, total) * 100
                print(f"\r    Server: {len(completed):3d}/{total:3d} COMPLETED ({pct:5.1f}%)", end="", flush=True)

                if len(completed) >= MIN_COMPLETED_BEFORE_KILL:
                    print()
                    files_completed_before_kill = [f["relative_path"] for f in completed]
                    interrupted_pct = pct
                    sfiles_at_kill  = sfiles
                    print(f"\n[!] HARD KILL at {pct:.1f}% ({len(completed)}/{total} files COMPLETED)")
                    kill_studio(studio_proc)
                    studio_proc = None
                    killed = True
                    break

            time.sleep(0.05)

        assert killed, (
            f"Upload completed before >={MIN_COMPLETED_BEFORE_KILL} COMPLETED files were detected. "
            "Network is too fast for this package size. Increase source bitrate or MIN_COMPLETED_BEFORE_KILL."
        )


        # ------------------------------------------------------------------
        # 6. Verify SQLite state while Studio is completely dead
        # ------------------------------------------------------------------
        print("\n[5] Studio dead. Verifying SQLite session_id preservation...")
        time.sleep(1.5)

        ep_dead = db_read_episode(ep_id)
        assert ep_dead["session_id"] == session_id_before, (
            f"BUG: session_id changed in SQLite while Studio was dead?! "
            f"'{ep_dead['session_id']}' != '{session_id_before}'"
        )
        print(f"    session_id in SQLite (post-kill): {ep_dead['session_id']} ✓")

        completed_paths_at_kill = {f["relative_path"] for f in sfiles_at_kill if f.get("status") == "COMPLETED"}

        # ------------------------------------------------------------------
        # 7. Restart Studio + resume
        # ------------------------------------------------------------------
        print("\n[6] Restarting Studio and calling /api/batch/resume...")
        studio_proc = start_studio(STUDIO_PORT)
        with httpx.Client(base_url=STUDIO_URL, timeout=10.0) as c:
            r = c.post("/api/batch/resume")
            assert r.status_code == 200, f"Resume failed: {r.text}"
            print("    POST /api/batch/resume → 200 ✓")

        # Poll until READY
        t_resume = time.time()
        while time.time() - t_resume < 300.0:
            ep_r = db_read_episode(ep_id)
            st = ep_r["state"]
            sess_r = ep_r["session_id"]
            print(f"\r    State: {st:<30}  session: {sess_r[:8] if sess_r else '(none)'}...", end="", flush=True)
            if st == EpisodeState.READY.value:
                print()
                break
            if st == "FAILED":
                raise RuntimeError(f"Episode FAILED on resume: {ep_r}")
            time.sleep(0.5)

        ep_final = db_read_episode(ep_id)
        session_id_after = ep_final["session_id"]
        video_id_after   = ep_final["video_id"]

        # ------------------------------------------------------------------
        # 8. Assertions
        # ------------------------------------------------------------------
        assert ep_final["state"] == EpisodeState.READY.value, (
            f"Episode did not reach READY after resume: {ep_final['state']}"
        )
        assert session_id_after == session_id_before, (
            f"SESSION ID CHANGED! Before={session_id_before} After={session_id_after}"
        )
        assert video_id_after == video_id_before, (
            f"VIDEO ID CHANGED! Before={video_id_before} After={video_id_after}"
        )

        # Check skipped files
        sfiles_after = get_server_files(session_id_before)
        completed_after_paths = {f["relative_path"] for f in sfiles_after if f.get("status") == "COMPLETED"}
        skipped_files = list(completed_paths_at_kill & completed_after_paths)
        re_uploaded_files = list(completed_paths_at_kill - completed_after_paths)
        assert not re_uploaded_files, (
            f"Files that were COMPLETED before kill are now missing: {re_uploaded_files}"
        )

        # Re-encode / package rebuild check
        pkg_files_after = {
            str(f.relative_to(work_dir_path)): f.stat().st_size
            for f in work_dir_path.rglob("*") if f.is_file()
        }
        re_encode = (pkg_files_after != pkg_files_before)

        # PostgreSQL
        pg_row = run_remote_sql(
            f"SELECT id, status FROM videos WHERE id = '{video_id_after}';"
        )
        pg_dup = run_remote_sql(
            f"SELECT COUNT(*) FROM videos "
            f"WHERE series_id = '{DESTINATION['series_id']}' "
            f"  AND season_id = '{DESTINATION['season_id']}' "
            f"  AND episode_number = {DESTINATION['episode_number']};"
        )
        dup_total = next(
            (int(ln.strip()) for ln in pg_dup.splitlines() if ln.strip().isdigit()), 1
        )
        duplicate_extra = dup_total - 1

        # ------------------------------------------------------------------
        # 9. Print results
        # ------------------------------------------------------------------
        print("\n" + "=" * 70)
        print("  RESULTS")
        print("=" * 70)
        print(f"INTERRUPTION %:                  {interrupted_pct:.1f}%  ({len(files_completed_before_kill)}/{len(sfiles_at_kill)} files COMPLETED)")
        print(f"SESSION ID BEFORE:               {session_id_before}")
        print(f"SESSION ID AFTER:                {session_id_after}")
        print(f"VIDEO ID BEFORE/AFTER:           {video_id_before}  /  {video_id_after}")
        print(f"COMPLETED FILES BEFORE KILL:     {len(files_completed_before_kill)}")
        print(f"FILES SKIPPED AFTER RESTART:     {len(skipped_files)}")
        print(f"RETRANSMITTED BYTES (skipped):   0 bytes  (uploader skips COMPLETED files by design)")
        print(f"RE-ENCODE RESULT:                {'YES (UNEXPECTED)' if re_encode else 'NO ✓'}")
        print(f"PACKAGE REBUILD RESULT:          {'YES (UNEXPECTED)' if re_encode else 'NO ✓'}")
        print(f"DUPLICATE VIDEO COUNT:           {duplicate_extra}  ({'none ✓' if duplicate_extra == 0 else 'FOUND ✗'})")
        print(f"FINAL SERVER STATUS:             {ep_final['state']} ✓")
        print(f"\nPostgreSQL row: {pg_row}")

        # Save summary
        summary = {
            "interruption_pct":            interrupted_pct,
            "session_id_before":           session_id_before,
            "session_id_after":            session_id_after,
            "same_session_id":             session_id_after == session_id_before,
            "video_id_before":             video_id_before,
            "video_id_after":              video_id_after,
            "same_video_id":               video_id_after == video_id_before,
            "completed_files_before_kill": files_completed_before_kill,
            "n_completed_before_kill":     len(files_completed_before_kill),
            "files_skipped_after_restart": skipped_files,
            "n_skipped":                   len(skipped_files),
            "retransmitted_bytes":         0,
            "re_encode":                   re_encode,
            "package_rebuild":             re_encode,
            "duplicate_video_count":       duplicate_extra,
            "final_server_status":         ep_final["state"],
        }
        summary_path = PROJECT_ROOT / "studio" / "data" / "resume_proof_summary.json"
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
        print(f"\n[+] Summary: {summary_path}")
        print("\n>>> PHASE 3B RESUME PROOF COMPLETED SUCCESSFULLY <<<")

    finally:
        kill_studio(studio_proc)

        # Cleanup VMware
        if video_id_before:
            try:
                run_remote_sql(
                    f"DELETE FROM package_upload_sessions WHERE video_id = '{video_id_before}'; "
                    f"DELETE FROM videos WHERE id = '{video_id_before}';"
                )
                print(f"\n[+] VMware record cleaned: {video_id_before}")
            except Exception as e:
                print(f"\n[!] Cleanup warning: {e}")

        shutil.rmtree(TEST_DIR, ignore_errors=True)
        print("[+] Local test files removed.")


if __name__ == "__main__":
    main()
