import subprocess
import json

remote_code = """
import secrets
import uuid
from datetime import datetime, timezone, timedelta
from app.core.database import SyncSessionLocal
from app.models.user import User
from app.models.session import UserSession
from app.security.tokens import hash_token
from app.security.csrf import generate_csrf_token

raw_token = secrets.token_urlsafe(32)
token_hashed = hash_token(raw_token)
csrf_token = generate_csrf_token()

with SyncSessionLocal() as session:
    user = session.query(User).filter(User.email == 'admin_live@aboodvideo.local').first()
    if not user:
        raise RuntimeError("User admin_live@aboodvideo.local not found")
    sess = UserSession(
        id=uuid.uuid4(),
        user_id=user.id,
        token_hash=token_hashed,
        ip_address='127.0.0.1',
        user_agent='playwright',
        expires_at=datetime.now(timezone.utc) + timedelta(days=1)
    )
    session.add(sess)
    session.commit()
    import json
    print(json.dumps({"session": raw_token, "csrf": csrf_token}))
"""

def create_session():
    cmd = [
        "ssh", "-o", "BatchMode=yes",
        "abood@127.0.0.1",
        "docker exec -i aboodvideo_api python"
    ]
    res = subprocess.run(cmd, input=remote_code, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"Error {res.returncode}:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}")
    for line in res.stdout.strip().splitlines():
        if line.startswith("{") and "session" in line:
            return json.loads(line)
    raise RuntimeError(f"Could not parse output: {res.stdout}\nError: {res.stderr}")

if __name__ == "__main__":
    data = create_session()
    print("SESSION:", data["session"])
    print("CSRF:", data["csrf"])
