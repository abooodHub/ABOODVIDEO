import subprocess
import json

def run_remote_ssh(cmd: str):
    full_cmd = ["ssh", "-o", "BatchMode=yes", "abood@127.0.0.1", cmd]
    res = subprocess.run(full_cmd, capture_output=True, text=True, check=True)
    return res.stdout

def run_remote_sql(sql: str):
    # Pass SQL via stdin to avoid shell quoting issues
    ssh_proc = subprocess.Popen(
        ["ssh", "-o", "BatchMode=yes", "abood@127.0.0.1", 
         "docker exec -i aboodvideo_postgres psql -U aboodvideo_user -d aboodvideo_db -A -F ','"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    stdout, stderr = ssh_proc.communicate(input=sql)
    if ssh_proc.returncode != 0:
        raise RuntimeError(f"SQL failed: {stderr}")
    return stdout

if __name__ == "__main__":
    print("Checking active transcodes...")
    print(run_remote_sql("SELECT status, count(*) FROM video_transcodes GROUP BY status;"))
    print("\nChecking video statuses...")
    print(run_remote_sql("SELECT status, count(*) FROM videos GROUP BY status;"))
    print("\nChecking studio packages...")
    print(run_remote_sql("SELECT status, count(*) FROM studio_packages GROUP BY status;"))
