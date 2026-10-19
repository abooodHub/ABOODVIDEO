"""Create owner account and Studio API key on clean server, then configure Studio DPAPI."""
import subprocess
import json
import re
import os
from getpass import getpass
from pathlib import Path
from studio.config import save_studio_config, load_studio_config
from studio.core.uploader import test_connection

remote_script = """
import uuid
from app.core.database import SyncSessionLocal
from app.models.user import User, UserRole
from app.models.api_key import ApiKey
from app.security.hashing import hash_password
from app.security.api_key import generate_raw_api_key, hash_api_key

with SyncSessionLocal() as session:
    user = session.query(User).filter(User.username == 'abood').first()
    if not user:
        user = User(
            id=uuid.uuid4(),
            email='admin@aboodvideo.local',
            username='abood',
            hashed_password=hash_password(owner_password),
            role=UserRole.OWNER,
            is_active=True
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        print(f"Created owner user: {user.username} ({user.id})")
    else:
        print(f"Existing owner user: {user.username} ({user.id})")
    
    # Create permanent Studio API Key
    raw_key = generate_raw_api_key()
    prefix = raw_key[:12]
    key_hash = hash_api_key(raw_key)
    
    api_key = ApiKey(
        id=uuid.uuid4(),
        user_id=user.id,
        name="ABOODVIDEO Studio 0.3.0 Key",
        prefix=prefix,
        key_hash=key_hash,
        scopes=["packages:create", "packages:write", "packages:publish"],
        is_active=True,
        expires_at=None
    )
    session.add(api_key)
    session.commit()
    print(f"CREATED_STUDIO_KEY:{raw_key}")
"""

def setup_key():
    owner_password = os.environ.get("ABOODVIDEO_SETUP_OWNER_PASSWORD") or getpass("New owner password: ")
    if len(owner_password) < 16:
        raise ValueError("Supply a unique owner password of at least 16 characters")
    cmd = [
        "ssh", "-o", "BatchMode=yes",
        "abood@127.0.0.1",
        "docker exec -i aboodvideo_backend python"
    ]
    # Password goes over SSH stdin, never the command line or a source file.
    payload = "owner_password = " + repr(owner_password) + "\n" + remote_script
    res = subprocess.run(cmd, input=payload, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError("Remote account setup failed; inspect the server logs privately")
    
    raw_key = None
    for line in res.stdout.splitlines():
        if line.startswith("CREATED_STUDIO_KEY:"):
            raw_key = line.split(":", 1)[1].strip()
            break
            
    if not raw_key:
        raise RuntimeError("The server did not return a Studio key")
    
    print("Studio API Key created successfully on remote server!")
    
    # Test connection
    server_url = "https://platform.example/api/v1"
    conn = test_connection(server_url, raw_key)
    print("test_connection result ok:", conn.get("ok"))
    assert conn.get("ok") is True, f"Connection test failed: {conn}"
    
    # Save to Studio DPAPI config
    save_studio_config({
        "server_url": server_url,
        "api_key": raw_key,
        "first_run_complete": True,
    })
    
    # Verify loaded config
    cfg = load_studio_config()
    assert cfg.get("server_url") == server_url
    assert bool(cfg.get("api_key")), "API key failed to load after DPAPI save"
    print("Studio config updated with DPAPI encrypted API key and verified!")

if __name__ == "__main__":
    setup_key()
