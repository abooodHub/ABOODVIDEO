"""Set up a verified owner user for live verification testing."""
import os

from app.core.database import SyncSessionLocal
from app.models.user import User, UserRole
from app.security.hashing import hash_password
from sqlalchemy import select


username = os.environ.get("ABOODVIDEO_SETUP_OWNER_USERNAME", "admin_live")
email = os.environ.get("ABOODVIDEO_SETUP_OWNER_EMAIL", "admin_live@aboodvideo.local")
password = os.environ.get("ABOODVIDEO_SETUP_OWNER_PASSWORD")

if not password:
    raise SystemExit(
        "ABOODVIDEO_SETUP_OWNER_PASSWORD must be set; no password is stored in source."
    )

with SyncSessionLocal() as session:
    user = session.execute(select(User).where(User.username == username)).scalar_one_or_none()
    if not user:
        user = User(
            username=username,
            email=email,
            hashed_password=hash_password(password),
            role=UserRole.OWNER,
            is_active=True,
        )
        session.add(user)
    else:
        user.email = email
        user.hashed_password = hash_password(password)
        user.role = UserRole.OWNER
        user.is_active = True
    session.commit()
    print(f"{username} is ready.")
