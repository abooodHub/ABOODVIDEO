import subprocess

code = """
import json
from app.core.database import SyncSessionLocal
from app.models.user import User, UserRole
from app.models.folder import Folder
from app.models.series import Series, Season
from app.models.api_key import ApiKey
from app.security.api_key import generate_raw_api_key, hash_api_key

with SyncSessionLocal() as db:
    owner = db.query(User).filter(User.role == UserRole.OWNER, User.is_active == True).first()
    if not owner:
        owner = db.query(User).filter(User.role == UserRole.ADMIN, User.is_active == True).first()
    
    # Check or create ApiKey for owner
    key_rec = db.query(ApiKey).filter(ApiKey.user_id == owner.id, ApiKey.is_active == True).first()
    raw_key = None
    if not key_rec:
        raw_key = generate_raw_api_key()
        key_rec = ApiKey(
            user_id=owner.id,
            name="Studio Phase3A Live Key",
            prefix=raw_key[:12],
            key_hash=hash_api_key(raw_key),
            scopes=["packages:create", "packages:write", "packages:publish", "videos:read"],
            is_active=True,
        )
        db.add(key_rec)
        db.commit()
    else:
        # Create a fresh known raw_key
        raw_key = generate_raw_api_key()
        key_rec = ApiKey(
            user_id=owner.id,
            name="Studio Phase3A Live Key Fresh",
            prefix=raw_key[:12],
            key_hash=hash_api_key(raw_key),
            scopes=["packages:create", "packages:write", "packages:publish", "videos:read"],
            is_active=True,
        )
        db.add(key_rec)
        db.commit()

    # Find or create a safe destination for owner
    folder = db.query(Folder).filter(Folder.user_id == owner.id, Folder.name == "Live Acceptance", Folder.trashed_at.is_(None)).first()
    if not folder:
        folder = Folder(name="Live Acceptance", user_id=owner.id, folder_type="folder")
        db.add(folder)
        db.commit()
        db.refresh(folder)

    series = db.query(Series).filter(Series.owner_id == owner.id, Series.title == "Live Acceptance Series", Series.trashed_at.is_(None)).first()
    if not series:
        series = Series(title="Live Acceptance Series", owner_id=owner.id, parent_folder_id=folder.id)
        db.add(series)
        db.commit()
        db.refresh(series)

    season = db.query(Season).filter(Season.series_id == series.id, Season.season_number == 1, Season.trashed_at.is_(None)).first()
    if not season:
        season = Season(series_id=series.id, season_number=1, display_name="Season 1")
        db.add(season)
        db.commit()
        db.refresh(season)

    out = {
        "user_id": str(owner.id),
        "username": owner.username,
        "api_key": raw_key,
        "folder_id": str(folder.id),
        "folder_name": folder.name,
        "series_id": str(series.id),
        "series_title": series.title,
        "season_id": str(season.id),
        "season_display_name": season.display_name,
        "season_number": season.season_number,
    }
    print("DESTINATION_DATA:" + json.dumps(out))
"""

proc = subprocess.run(
    ["ssh", "-o", "BatchMode=yes", "abood@127.0.0.1", "docker exec -i aboodvideo_api python"],
    input=code,
    capture_output=True,
    text=True,
    check=True
)
print(proc.stdout)
