import subprocess

script = """
import asyncio
from app.core.redis import get_cache_client

async def reset_rate_limit():
    r = get_cache_client()
    keys = await r.keys('rate_limit:*')
    print('Found keys:', [k.decode() if isinstance(k, bytes) else str(k) for k in keys])
    if keys:
        deleted = await r.delete(*keys)
        print(f'Deleted {deleted} rate limit keys.')
    else:
        print('No rate limit keys found.')

asyncio.run(reset_rate_limit())
"""

res = subprocess.run(
    ['ssh', '-o', 'BatchMode=yes', 'abood@127.0.0.1', 'docker exec -i -e PYTHONPATH=/app aboodvideo_backend python'],
    input=script, text=True, capture_output=True
)
print("STDOUT:", res.stdout)
print("STDERR:", res.stderr)
