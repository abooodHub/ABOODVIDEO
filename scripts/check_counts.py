import subprocess

script = """
from app.core.database import SyncSessionLocal
from app.models.video import Video
from app.models.folder import Folder

with SyncSessionLocal() as s:
    v_count = s.query(Video).filter(Video.trashed_at.is_(None)).count()
    t_count = s.query(Video).filter(Video.trashed_at.is_not(None)).count()
    f_count = s.query(Folder).count()
    print(f"Active Videos: {v_count}, Trashed: {t_count}, Folders: {f_count}")
"""

res = subprocess.run(
    ['ssh', '-o', 'BatchMode=yes', 'abood@127.0.0.1', 'docker exec -i -e PYTHONPATH=/app aboodvideo_backend python'],
    input=script, text=True, capture_output=True
)
print(res.stdout)
