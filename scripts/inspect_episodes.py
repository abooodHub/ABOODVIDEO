import subprocess

code = """
from app.core.database import SyncSessionLocal
from app.models.api_key import ApiKey
s = SyncSessionLocal()
test_pattern = r"(test|sample|temp|race|traversal|create only|write only|monitor only|fake|dummy|mock|acceptance)"
for k in s.query(ApiKey).filter(~ApiKey.name.op("~*")(test_pattern)).all():
    print(k.id, repr(k.name))
"""

cmd = ["ssh", "-o", "BatchMode=yes", "abood@127.0.0.1", "docker exec -i aboodvideo_api python"]
res = subprocess.run(cmd, input=code, capture_output=True, text=True)
print(res.stdout)
if res.stderr:
    print("ERR:", res.stderr)
