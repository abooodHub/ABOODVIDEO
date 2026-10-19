"""Create and remove an owned disposable stack, then run isolated pytest.

Usage: python scripts/run_real_integration.py [pytest arguments]
Existing containers, ports, networks and volumes are never replaced.
"""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from real_platform_support import IMAGE, LABEL, NETWORK, VOLUME, assert_owned, docker

# Match the repository Compose dependencies, including their distro/build.
POSTGRES_IMAGE = "postgres:16.4-alpine3.20"
REDIS_IMAGE = "redis:7.2.5-alpine3.20"


def main():
    run_id = uuid.uuid4().hex
    owned = []
    evidence = Path(tempfile.mkdtemp(prefix="abv-integration-run-"))
    print(f"INTEGRATION_EVIDENCE={evidence}", flush=True)
    docker("version", "--format", "{{.Server.Version}}")
    docker("info", "--format", "{{.OSType}}")
    if os.getenv("ABV_INTEGRATION_IMAGE"):
        if docker("image","inspect","--format",'{{ index .Config.Labels "abv.purpose" }}',IMAGE) != LABEL:
            raise RuntimeError("The supplied staging candidate is not labelled for disposable integration")
    # Refuse collisions before making any changes.
    names = {line.lstrip("/") for line in docker("ps", "-a", "--format", "{{.Names}}").splitlines()}
    reserved = {"abv-f16-pg", "abv-f16-redis", "abv-real-e2e-api", "abv-real-e2e-worker-1", "abv-real-e2e-worker-2"}
    if names & reserved:
        raise RuntimeError("Reserved integration container name is already in use")
    if NETWORK in docker("network", "ls", "--format", "{{.Name}}").splitlines():
        raise RuntimeError("Reserved integration network is already in use")
    if VOLUME in docker("volume", "ls", "--format", "{{.Name}}").splitlines():
        raise RuntimeError("Reserved integration volume is already in use")
    for port in (25432, 26379, 28080, 28081, 28082, 28083, 28443):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))
    try:
        if IMAGE not in docker("image", "ls", "--format", "{{.Repository}}:{{.Tag}}").splitlines():
            build = evidence / "build"; build.mkdir()
            shutil.copy2(ROOT / "backend/requirements.txt", build / "requirements.txt")
            (build / "Dockerfile").write_text(
                "FROM python:3.12-slim\nWORKDIR /app\n"
                "RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libpq5 procps && rm -rf /var/lib/apt/lists/*\n"
                "COPY requirements.txt /tmp/requirements.txt\nRUN pip install --no-cache-dir -r /tmp/requirements.txt\n",
                encoding="utf-8",
            )
            docker("build", "--label", f"abv.purpose={LABEL}", "-t", IMAGE, str(build), timeout=900)
        docker("network", "create", "--label", f"abv.purpose={LABEL}", "--label", f"abv.run={run_id}", NETWORK)
        owned.append(("network", NETWORK))
        docker("volume", "create", "--label", f"abv.purpose={LABEL}", "--label", f"abv.run={run_id}", VOLUME)
        owned.append(("volume", VOLUME))
        docker("run", "-d", "--name", "abv-f16-pg", "--network", NETWORK, "--network-alias", "pg",
               "--label", f"abv.purpose={LABEL}", "--label", f"abv.run={run_id}", "-p", "127.0.0.1:25432:5432",
               "-e", "POSTGRES_USER=fixture", "-e", "POSTGRES_PASSWORD=fixture_only", "-e", "POSTGRES_DB=aboodvideo_test", POSTGRES_IMAGE)
        owned.append(("container", "abv-f16-pg"))
        docker("run", "-d", "--name", "abv-f16-redis", "--network", NETWORK, "--network-alias", "redis",
               "--label", f"abv.purpose={LABEL}", "--label", f"abv.run={run_id}", "-p", "127.0.0.1:26379:6379", REDIS_IMAGE,
               "redis-server","--save","60","1","--loglevel","notice","--maxmemory","512mb","--maxmemory-policy","volatile-lru")
        owned.append(("container", "abv-f16-redis"))
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            # The image's initialization server listens on a Unix socket only;
            # wait for the final TCP server, not that short-lived bootstrap.
            result = subprocess.run(["docker", "exec", "abv-f16-pg", "pg_isready", "-h", "127.0.0.1", "-U", "fixture"], capture_output=True)
            if result.returncode == 0: break
            time.sleep(.2)
        else: raise RuntimeError("Disposable PostgreSQL did not become ready")
        if docker("exec", "abv-f16-redis", "redis-cli", "ping") != "PONG":
            raise RuntimeError("Disposable Redis did not become ready")
        docker("exec", "abv-f16-pg", "createdb", "-U", "fixture", "aboodvideo_e2e_test")
        env = dict(os.environ, ABV_RUN_REAL_INTEGRATION="1", ABOODVIDEO_RUN_BROWSER_TEST="1",
                   ABOODVIDEO_RUN_LIVE_BROWSER_TEST="0", ABV_INTEGRATION_RUN_ID=run_id)
        args = sys.argv[1:] or ["tests", "-q", "--tb=short", "--show-capture=no"]
        # A generic full-suite invocation must never opt into the live server test.
        args += ["--ignore=" + str(ROOT / "tests/test_admin_auth_live_browser_acceptance.py"), "--junitxml=" + str(evidence / "results.xml")]
        with (evidence / "results.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen([sys.executable, str(ROOT / "scripts/run_f16_isolated.py"), *args], cwd=ROOT, env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
            try:
                for line in process.stdout:
                    print(line, end="", flush=True); log.write(line); log.flush()
                return process.wait()
            finally:
                if process.poll() is None:
                    process.terminate(); process.wait(timeout=30)
    finally:
        # Remove only containers created in this invocation or attached to its
        # new exclusive network. Every deletion requires the purpose label.
        if ("network", NETWORK) in owned:
            network = json.loads(docker("network", "inspect", NETWORK))[0]
            assert network["Labels"].get("abv.run") == run_id
            for container in network["Containers"]:
                assert_owned(container)
                docker("rm", "-f", "-v", container)
            # Also catch stopped containers, absent from the active endpoint list.
            for container in docker("ps", "-a", "--filter", f"label=abv.run={run_id}", "--format", "{{.ID}}").splitlines():
                assert_owned(container)
                docker("rm", "-f", "-v", container)
        for kind, name in reversed(owned):
            if kind == "volume":
                assert docker("volume", "inspect", "--format", '{{ index .Labels "abv.run" }}', name) == run_id
                docker("volume", "rm", name)
            elif kind == "network":
                docker("network", "rm", name)
        print("DISPOSABLE_STACK_REMOVED=YES", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
