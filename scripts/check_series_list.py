import subprocess

script = """
import asyncio
from app.core.database import AsyncSessionLocal
from app.api.v1.admin_videos import admin_videos_series_list
from app.models.user import User

async def main():
    async with AsyncSessionLocal() as s:
        # call admin_videos_series_list
        res = await admin_videos_series_list(db=s)
        print("Series list:", res)

asyncio.run(main())
"""

res = subprocess.run(
    ['ssh', '-o', 'BatchMode=yes', 'abood@127.0.0.1', 'docker exec -i -e PYTHONPATH=/app aboodvideo_backend python'],
    input=script, text=True, capture_output=True
)
print("STDOUT:", res.stdout)
print("STDERR:", res.stderr)
