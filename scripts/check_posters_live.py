import subprocess

def run_in_backend(py_code: str):
    ssh_proc = subprocess.Popen(
        ["ssh", "-o", "BatchMode=yes", "abood@127.0.0.1", 
         "docker exec -i aboodvideo_api python -"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    stdout, stderr = ssh_proc.communicate(input=py_code)
    if ssh_proc.returncode != 0:
        raise RuntimeError(f"Python failed: {stderr}")
    return stdout

code = """
from app.core.database import SyncSessionLocal
from app.models.video import Video
from app.models.video import VideoStatus
from app.api.viewer_routes import resolve_poster_disk_path

with SyncSessionLocal() as s:
    videos = s.query(Video).filter(
        Video.status == VideoStatus.READY,
        Video.trashed_at.is_(None),
    ).all()
    path_set = sum(bool(v.poster_path) for v in videos)
    existing_posters = 0
    missing_posters = 0
    for v in videos:
        p, _ = resolve_poster_disk_path(v)
        if p and p.is_file():
            existing_posters += 1
            print(f"EXISTS: ID={v.id}, title='{v.title}', path='{p}'")
        elif v.poster_path:
            missing_posters += 1
            print(f"MISSING: ID={v.id}, title='{v.title}', poster_path='{v.poster_path}'")

    intentional_fallbacks = len(videos) - existing_posters
    print(f"SUMMARY: ready={len(videos)}, poster_path_set={path_set}, existing_files={existing_posters}, missing_path_files={missing_posters}, intentional_fallbacks={intentional_fallbacks}")
"""

print(run_in_backend(code))
