import subprocess

script = """
import asyncio
from app.core.database import AsyncSessionLocal
from app.services.drive_service import DriveService

async def check():
    async with AsyncSessionLocal() as db:
        root_data = await DriveService.get_scope_contents(db=db, scope_id="root", content_filter="content")
        print("ROOT KEYS:", list(root_data.keys()))
        print("ROOT FOLDERS:", len(root_data.get("folders", [])))
        print("ROOT VIDEOS:", len(root_data.get("videos", [])))

        trash_data = await DriveService.get_scope_contents(db=db, scope_id="trash", content_filter="trash")
        print("TRASH ITEMS:", len(trash_data.get("videos", [])))
        if trash_data.get("videos"):
            print("TRASH VIDEO 0:", trash_data["videos"][0]["name"], trash_data["videos"][0]["id"])

asyncio.run(check())
"""

res = subprocess.run(
    ['ssh', '-o', 'BatchMode=yes', 'abood@127.0.0.1', 'docker exec -i -e PYTHONPATH=/app aboodvideo_backend python'],
    input=script, text=True, capture_output=True
)
print("STDOUT:", res.stdout)
print("STDERR:", res.stderr)
