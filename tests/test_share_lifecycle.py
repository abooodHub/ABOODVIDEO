"""P0-3 share lifecycle coverage against the isolated pytest database."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.database import AsyncSessionLocal
from app.main import app
from app.models.folder import Folder
from app.models.series import Series, Season
from app.models.share import Share
from app.models.user import User, UserRole
from app.models.video import Video, VideoStatus
from app.security.hashing import hash_password
from app.services.drive_service import DriveService


@pytest.fixture
async def shared_tree():
    async with AsyncSessionLocal() as db:
        owner = User(id=uuid.uuid4(), email=f"share_{uuid.uuid4().hex}@test.local", username=f"share_{uuid.uuid4().hex}", hashed_password=hash_password("ShareTestPassword2026!"), role=UserRole.OWNER, is_active=True)
        folder = Folder(id=uuid.uuid4(), user_id=owner.id, name="Shared folder")
        series = Series(id=uuid.uuid4(), owner_id=owner.id, parent_folder_id=folder.id, title="Shared series")
        season = Season(id=uuid.uuid4(), series_id=series.id, season_number=1, display_name="Season 1")
        video = Video(id=uuid.uuid4(), user_id=owner.id, title="Shared episode", source_filename="shared.mp4", status=VideoStatus.READY, series_id=series.id, season_id=season.id)
        unrelated = Video(id=uuid.uuid4(), user_id=owner.id, title="Unrelated", source_filename="unrelated.mp4", status=VideoStatus.READY)
        db.add_all([owner, folder, series, season, video, unrelated])
        await db.commit()
        return {"owner": owner.id, "folder": folder.id, "series": series.id, "season": season.id, "video": video.id, "unrelated": unrelated.id}


@pytest.mark.asyncio
async def test_video_share_unavailable_in_trash_restores_and_is_removed_on_permanent_delete(shared_tree):
    async with AsyncSessionLocal() as db:
        tree = shared_tree
        share = await DriveService.create_share(db, tree["owner"], "video", tree["video"], allow_download=True)
        assert await DriveService.get_share_by_token(db, share.token)
        await DriveService.trash_items(db, [{"id": str(tree["video"]), "type": "video"}], tree["owner"])
        assert await DriveService.get_share_by_token(db, share.token) is None
        await DriveService.restore_items(db, [{"id": str(tree["video"]), "type": "video"}], tree["owner"])
        assert await DriveService.get_share_by_token(db, share.token)
        await DriveService.trash_items(db, [{"id": str(tree["video"]), "type": "video"}], tree["owner"])
        await DriveService.permanent_delete_items(db, [{"id": str(tree["video"]), "type": "video"}], tree["owner"])
        assert await db.get(Share, share.id) is None


@pytest.mark.asyncio
async def test_folder_permanent_delete_removes_folder_share_and_preserves_detached_series_shares(shared_tree):
    async with AsyncSessionLocal() as db:
        tree = shared_tree
        season_share = await DriveService.create_share(db, tree["owner"], "season", tree["season"])
        video_share = await DriveService.create_share(db, tree["owner"], "video", tree["video"])
        folder_share = await DriveService.create_share(db, tree["owner"], "folder", tree["folder"])
        series_share = await DriveService.create_share(db, tree["owner"], "series", tree["series"])
        unrelated_share = await DriveService.create_share(db, tree["owner"], "video", tree["unrelated"])

        await DriveService.trash_items(db, [{"id": str(tree["folder"]), "type": "folder"}], tree["owner"])
        assert await DriveService.get_share_by_token(db, folder_share.token) is None
        await DriveService.restore_items(db, [{"id": str(tree["folder"]), "type": "folder"}], tree["owner"])
        assert await DriveService.get_share_by_token(db, folder_share.token)

        await DriveService.trash_items(db, [{"id": str(tree["folder"]), "type": "folder"}], tree["owner"])
        await DriveService.permanent_delete_items(db, [{"id": str(tree["folder"]), "type": "folder"}], tree["owner"])
        # Folder deletion follows direct folder membership. The Series FK is
        # SET NULL, so the series, season, and related videos remain valid at
        # the library root and their independent shares must remain usable.
        assert await db.get(Share, folder_share.id) is None
        for share in (season_share, video_share, series_share):
            assert await db.get(Share, share.id) is not None
            assert await DriveService.get_share_by_token(db, share.token) is not None
        assert await db.get(Share, unrelated_share.id) is not None


@pytest.mark.asyncio
async def test_revoked_expired_and_orphan_shares_are_safely_unavailable(shared_tree):
    async with AsyncSessionLocal() as db:
        tree = shared_tree
        revoked = await DriveService.create_share(db, tree["owner"], "video", tree["video"])
        await DriveService.revoke_share(db, revoked.token)
        assert await DriveService.get_share_by_token(db, revoked.token) is None

        expired = Share(id=uuid.uuid4(), owner_id=tree["owner"], target_type="video", target_id=tree["video"], token=uuid.uuid4().hex, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        orphan = Share(id=uuid.uuid4(), owner_id=tree["owner"], target_type="video", target_id=uuid.uuid4(), token=uuid.uuid4().hex)
        db.add_all([expired, orphan])
        await db.commit()
        assert await DriveService.get_share_by_token(db, expired.token) is None
        assert await DriveService.get_share_by_token(db, orphan.token) is None
        assert orphan.id in await DriveService.reconcile_orphan_shares(db)


@pytest.mark.asyncio
async def test_download_requires_share_permission_effective_policy_and_active_target(shared_tree):
    async with AsyncSessionLocal() as db:
        tree = shared_tree
        no_download = await DriveService.create_share(db, tree["owner"], "video", tree["video"], allow_download=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        response = await client.get(f"/preview/share/{no_download.token}/download/{tree['video']}")
        assert response.status_code == 403

    async with AsyncSessionLocal() as db:
        video = await db.get(Video, tree["video"])
        video.allow_download_policy = "DENY"
        await db.commit()
        denied_by_policy = await DriveService.create_share(db, tree["owner"], "video", tree["video"], allow_download=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        response = await client.get(f"/preview/share/{denied_by_policy.token}/download/{tree['video']}")
        assert response.status_code == 403

    async with AsyncSessionLocal() as db:
        await DriveService.trash_items(db, [{"id": str(tree["video"]), "type": "video"}], tree["owner"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        response = await client.get(f"/preview/share/{denied_by_policy.token}/download/{tree['video']}")
        assert response.status_code == 404
