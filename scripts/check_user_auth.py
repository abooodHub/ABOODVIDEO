import subprocess

script = """
from app.core.database import SyncSessionLocal
from app.models.user import User
from app.security.hashing import verify_password

with SyncSessionLocal() as session:
    users = session.query(User).all()
    for u in users:
        print(f"User: {u.username}, Email: {u.email}, Role: {u.role}, Active: {u.is_active}")
        for p in ['AboodVideo2026!Secure', 'AdminPass2026!', 'abood', 'admin', 'password']:
            if verify_password(p, u.hashed_password):
                print(f"  --> MATCHES PASSWORD: {p}")
"""

res = subprocess.run(
    ['ssh', '-o', 'BatchMode=yes', 'abood@127.0.0.1', 'docker exec -i -e PYTHONPATH=/app aboodvideo_backend python'],
    input=script, text=True, capture_output=True
)
print("STDOUT:", res.stdout)
print("STDERR:", res.stderr)
