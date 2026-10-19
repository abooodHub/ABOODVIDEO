#!/usr/bin/env python3
"""
ABOODVIDEO Isolated Database Restore Verification Script
Restores a PostgreSQL custom-format dump into an isolated temporary database,
validates integrity, schema, and tables, and safely cleans up.
Supports execution via local tools or delegated to the PostgreSQL container.
"""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from urllib.parse import urlparse

def compute_sha256(filepath: str) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()

def run_isolated_restore_test(dump_path: str = None, test_db_name: str = "aboodvideo_restore_test") -> bool:
    pg_container = os.environ.get("PG_CONTAINER", "aboodvideo_postgres")
    pg_user = os.environ.get("POSTGRES_USER", "aboodvideo_user")
    pg_pass = os.environ.get("POSTGRES_PASSWORD", "aboodvideo_password")
    original_db = os.environ.get("POSTGRES_DB", "aboodvideo_db")

    has_local_tools = shutil.which("pg_dump") is not None and shutil.which("pg_restore") is not None
    use_docker = not has_local_tools

    print(f"[*] Isolated restore verification mode: {'Docker Container (' + pg_container + ')' if use_docker else 'Local CLI Tools'}")

    temp_dump = False
    if not dump_path:
        dump_path = f"/tmp/test_dump_{os.getpid()}.dump"
        temp_dump = True
        print(f"[*] Creating temporary custom dump (-Fc) of {original_db} -> {dump_path}...")
        if use_docker:
            dump_cmd = ["docker", "exec", pg_container, "pg_dump", "-U", pg_user, "-d", original_db, "-Fc"]
            with open(dump_path, "wb") as f:
                res = subprocess.run(dump_cmd, stdout=f, stderr=subprocess.PIPE)
            if res.returncode != 0:
                print(f"[!] pg_dump failed via docker: {res.stderr.decode('utf-8')}")
                return False
        else:
            os.environ["PGPASSWORD"] = pg_pass
            dump_cmd = ["pg_dump", "-U", pg_user, "-d", original_db, "-Fc", "-f", dump_path]
            res = subprocess.run(dump_cmd, capture_output=True, text=True)
            if res.returncode != 0:
                print(f"[!] pg_dump failed: {res.stderr}")
                return False

    try:
        # 1. Verify Dump Checksum
        calculated_sha = compute_sha256(dump_path)
        sha_file = f"{dump_path}.sha256"
        if os.path.exists(sha_file):
            with open(sha_file, "r") as f:
                expected_sha = f.read().strip().split()[0]
            if calculated_sha != expected_sha:
                print(f"[!] Checksum mismatch! Expected {expected_sha}, got {calculated_sha}")
                return False
            print(f"[+] SHA-256 integrity verified: {calculated_sha}")
        else:
            print(f"[+] Dump SHA-256 computed: {calculated_sha}")

        # 2. Re-create isolated database
        print(f"[*] Re-creating isolated database {test_db_name}...")
        drop_sql = f"DROP DATABASE IF EXISTS {test_db_name};"
        create_sql = f"CREATE DATABASE {test_db_name};"
        if use_docker:
            subprocess.run(
                ["docker", "exec", pg_container, "psql", "-U", pg_user, "-d", "postgres", "-c", drop_sql],
                capture_output=True, text=True
            )
            res = subprocess.run(
                ["docker", "exec", pg_container, "psql", "-U", pg_user, "-d", "postgres", "-c", create_sql],
                capture_output=True, text=True
            )
        else:
            subprocess.run(
                ["psql", "-U", pg_user, "-d", "postgres", "-c", drop_sql],
                capture_output=True, text=True
            )
            res = subprocess.run(
                ["psql", "-U", pg_user, "-d", "postgres", "-c", create_sql],
                capture_output=True, text=True
            )
        if res.returncode != 0:
            print(f"[!] Failed to recreate test DB: {res.stderr}")
            return False

        # 3. Restore dump into isolated test DB
        print(f"[*] Restoring dump into {test_db_name} using pg_restore...")
        if use_docker:
            with open(dump_path, "rb") as f:
                res = subprocess.run(
                    ["docker", "exec", "-i", pg_container, "pg_restore", "-U", pg_user, "-d", test_db_name, "--no-owner"],
                    stdin=f, capture_output=True, text=True
                )
        else:
            res = subprocess.run(
                ["pg_restore", "-U", pg_user, "-d", test_db_name, "--no-owner", dump_path],
                capture_output=True, text=True
            )
        if res.returncode > 1:
            print(f"[!] pg_restore failed with code {res.returncode}: {res.stderr}")
            return False

        # 4. Validate schema and critical tables
        print(f"[*] Validating restored schema in {test_db_name}...")
        check_sql = """
        SELECT version_num FROM alembic_version;
        SELECT COUNT(*) FROM users;
        SELECT COUNT(*) FROM videos;
        SELECT COUNT(*) FROM video_renditions;
        SELECT COUNT(*) FROM transcode_jobs;
        SELECT COUNT(*) FROM video_subtitles;
        SELECT COUNT(*) FROM api_keys;
        SELECT COUNT(*) FROM package_upload_sessions;
        SELECT COUNT(*) FROM package_files;
        """
        if use_docker:
            res = subprocess.run(
                ["docker", "exec", pg_container, "psql", "-U", pg_user, "-d", test_db_name, "-c", check_sql],
                capture_output=True, text=True
            )
        else:
            res = subprocess.run(
                ["psql", "-U", pg_user, "-d", test_db_name, "-c", check_sql],
                capture_output=True, text=True
            )
        if res.returncode != 0:
            print(f"[!] Validation query failed: {res.stderr}")
            return False

        print(res.stdout)
        print("[+] Isolated restore validation PASSED successfully!")
        return True

    finally:
        # Cleanup
        print(f"[*] Dropping isolated test database {test_db_name}...")
        cleanup_sql = f"DROP DATABASE IF EXISTS {test_db_name};"
        if use_docker:
            subprocess.run(
                ["docker", "exec", pg_container, "psql", "-U", pg_user, "-d", "postgres", "-c", cleanup_sql],
                capture_output=True, text=True
            )
        else:
            subprocess.run(
                ["psql", "-U", pg_user, "-d", "postgres", "-c", cleanup_sql],
                capture_output=True, text=True
            )

        if temp_dump and os.path.exists(dump_path):
            os.remove(dump_path)
            if os.path.exists(f"{dump_path}.sha256"):
                os.remove(f"{dump_path}.sha256")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test isolated database restore")
    parser.add_argument("--dump-file", help="Path to .dump file to verify and restore")
    parser.add_argument("--test-db", default="aboodvideo_restore_test", help="Temporary database name")
    args = parser.parse_args()

    success = run_isolated_restore_test(args.dump_file, args.test_db)
    sys.exit(0 if success else 1)
