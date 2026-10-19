"""P0-1 regression coverage for private Drive authorization boundaries."""
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.main import app
from app.models.folder import Folder
from app.models.session import UserSession
from app.models.share import Share
from app.models.user import User, UserRole
from app.models.video import Video, VideoStatus, VideoVisibility
from app.security.hashing import hash_password
from app.security.tokens import hash_token


async def _create_session(db, user: User, *, expired: bool = False) -> str:
    token = secrets.token_urlsafe(32)
    db.add(UserSession(
        id=uuid.uuid4(),
        user_id=user.id,
        token_hash=hash_token(token),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=-1 if expired else 30),
    ))
    return token


@pytest.fixture
async def drive_auth_fixture():
    async with AsyncSessionLocal() as db:
        users = {}
        for label, role in (("owner", UserRole.OWNER), ("admin", UserRole.ADMIN), ("user", UserRole.USER), ("vip", UserRole.VIP), ("other", UserRole.OWNER)):
            user = User(
                id=uuid.uuid4(),
                email=f"drive_auth_{label}_{uuid.uuid4().hex[:8]}@example.test",
                username=f"drive_auth_{label}_{uuid.uuid4().hex[:8]}",
                hashed_password=hash_password("DriveAuthTestPassword2026!"),
                role=role,
                is_active=True,
            )
            db.add(user)
            users[label] = user
        await db.flush()

        own_folder = Folder(id=uuid.uuid4(), user_id=users["owner"].id, name="Owner-only folder")
        other_folder = Folder(id=uuid.uuid4(), user_id=users["other"].id, name="Other-owner secret folder")
        shared_video = Video(
            id=uuid.uuid4(),
            user_id=users["owner"].id,
            title="Shared without admin session",
            source_filename="shared.mp4",
            status=VideoStatus.READY,
            visibility=VideoVisibility.UNLISTED,
            active_generation_id=uuid.uuid4(),
        )
        own_test_video = Video(
            id=uuid.uuid4(), user_id=users["owner"].id, title="Test Source Video owner",
            source_filename="test-owner.mp4", status=VideoStatus.READY, visibility=VideoVisibility.PRIVATE,
            is_test_content=True,
        )
        other_test_video = Video(
            id=uuid.uuid4(), user_id=users["other"].id, title="Test Source Video other",
            source_filename="test-other.mp4", status=VideoStatus.READY, visibility=VideoVisibility.PRIVATE,
            is_test_content=True,
        )
        share = Share(
            id=uuid.uuid4(),
            owner_id=users["owner"].id,
            target_type="video",
            target_id=shared_video.id,
            token=secrets.token_urlsafe(24),
            allow_download=False,
        )
        db.add_all([own_folder, other_folder, shared_video, own_test_video, other_test_video, share])

        tokens = {label: await _create_session(db, user) for label, user in users.items()}
        tokens["expired"] = await _create_session(db, users["owner"], expired=True)
        await db.commit()
        return {"tokens": tokens, "share_token": share.token, "other_folder_id": str(other_folder.id)}


def _client_with_session(token: str) -> AsyncClient:
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost")
    client.cookies.set(settings.SESSION_COOKIE_NAME, token)
    return client


@pytest.mark.asyncio
async def test_drive_api_requires_active_owner_or_admin_session(drive_auth_fixture):
    """No cookie, invalid/expired cookies, USER, and VIP are all denied."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        assert (await client.get("/api/v1/drive/tree")).status_code == 401
        client.cookies.set(settings.SESSION_COOKIE_NAME, "not-a-real-session")
        assert (await client.get("/api/v1/drive/tree")).status_code == 401

    for role in ("expired", "user", "vip"):
        async with _client_with_session(drive_auth_fixture["tokens"][role]) as client:
            expected = 401 if role == "expired" else 403
            assert (await client.get("/api/v1/drive/tree")).status_code == expected


@pytest.mark.asyncio
async def test_drive_api_allows_owner_and_admin_and_scopes_owner_tree(drive_auth_fixture):
    """Management roles work; an owner tree excludes another owner's resource names and counts."""
    async with _client_with_session(drive_auth_fixture["tokens"]["owner"]) as client:
        response = await client.get("/api/v1/drive/tree")
        assert response.status_code == 200
        assert "Owner-only folder" in response.text
        assert "Other-owner secret folder" not in response.text

    async with _client_with_session(drive_auth_fixture["tokens"]["admin"]) as client:
        response = await client.get("/api/v1/drive/tree")
        assert response.status_code == 200
        assert "Other-owner secret folder" in response.text


@pytest.mark.asyncio
async def test_drive_scope_only_lists_mutable_owner_records(drive_auth_fixture):
    async with _client_with_session(drive_auth_fixture["tokens"]["owner"]) as client:
        test_scope = await client.get("/api/v1/drive/scope?filter=test")
        assert test_scope.status_code == 200
        assert "Test Source Video owner" in test_scope.text
        assert "Test Source Video other" not in test_scope.text

        root_scope = await client.get("/api/v1/drive/scope?filter=all")
        assert root_scope.status_code == 200
        assert "Other-owner secret folder" not in root_scope.text
        assert "Test Source Video other" not in root_scope.text

        foreign_scope = await client.get(
            f"/api/v1/drive/scope?id={drive_auth_fixture['other_folder_id']}&type=folder&filter=all"
        )
        assert foreign_scope.status_code == 403

    async with _client_with_session(drive_auth_fixture["tokens"]["admin"]) as client:
        test_scope = await client.get("/api/v1/drive/scope?filter=test")
        assert test_scope.status_code == 200
        assert "Test Source Video owner" in test_scope.text
        assert "Test Source Video other" in test_scope.text


@pytest.mark.asyncio
async def test_preview_library_is_hidden_but_public_share_remains_anonymous(drive_auth_fixture):
    """The obsolete preview root stays hidden, including for signed-in owners."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        preview = await client.get("/preview", follow_redirects=False)
        assert preview.status_code == 404
        assert preview.content == b""
        assert preview.headers["cache-control"] == "no-store"
        assert "location" not in preview.headers
        share = await client.get(f"/preview/share/{drive_auth_fixture['share_token']}")
        assert share.status_code == 200

    async with _client_with_session(drive_auth_fixture["tokens"]["owner"]) as client:
        preview = await client.get("/preview", follow_redirects=False)
        assert preview.status_code == 404
        assert preview.content == b""
        assert "location" not in preview.headers
