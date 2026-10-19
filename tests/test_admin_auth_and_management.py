"""Automated Tests for Admin Authentication, Sessions, CSRF, and Video Management."""
import uuid
import secrets
from datetime import datetime, timezone, timedelta
import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from app.main import app
from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.user import User, UserRole
from app.models.video import Video, VideoStatus, VideoVisibility
from app.models.session import UserSession
from app.security.hashing import hash_password
from app.security.tokens import hash_token


@pytest.fixture
async def admin_fixture():
    """Create test owner, test admin, and regular user in database."""
    async with AsyncSessionLocal() as session:
        owner_pass = "OwnerSecurePass123!"
        admin_pass = "AdminSecurePass123!"
        user_pass = "UserSecurePass123!"

        owner = User(
            id=uuid.uuid4(),
            email=f"owner_{uuid.uuid4().hex[:6]}@aboodvideo.local",
            username=f"owner_{uuid.uuid4().hex[:6]}",
            hashed_password=hash_password(owner_pass),
            role=UserRole.OWNER,
            is_active=True,
        )
        admin = User(
            id=uuid.uuid4(),
            email=f"admin_{uuid.uuid4().hex[:6]}@aboodvideo.local",
            username=f"admin_{uuid.uuid4().hex[:6]}",
            hashed_password=hash_password(admin_pass),
            role=UserRole.ADMIN,
            is_active=True,
        )
        reg_user = User(
            id=uuid.uuid4(),
            email=f"user_{uuid.uuid4().hex[:6]}@aboodvideo.local",
            username=f"user_{uuid.uuid4().hex[:6]}",
            hashed_password=hash_password(user_pass),
            role=UserRole.USER,
            is_active=True,
        )
        session.add_all([owner, admin, reg_user])
        await session.commit()
        return {
            "owner": owner,
            "owner_pass": owner_pass,
            "admin": admin,
            "admin_pass": admin_pass,
            "user": reg_user,
            "user_pass": user_pass,
        }


@pytest.mark.asyncio
async def test_admin_login_success(admin_fixture):
    """Test 1: Owner can login, receives HttpOnly session cookie and CSRF cookie."""
    owner = admin_fixture["owner"]
    password = admin_fixture["owner_pass"]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        res = await client.post(
            "/api/v1/auth/login",
            json={"username_or_email": owner.username, "password": password},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["success"] is True
        assert "csrf_token" in data
        assert data["user"]["role"] == "OWNER"
        assert settings.SESSION_COOKIE_NAME in res.cookies
        assert settings.CSRF_COOKIE_NAME in res.cookies
        assert res.headers.get("Cache-Control") == "no-store"


@pytest.mark.asyncio
async def test_admin_login_invalid_password(admin_fixture):
    """Test 2: Invalid password returns 401 Unauthorized."""
    owner = admin_fixture["owner"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        res = await client.post(
            "/api/v1/auth/login",
            json={"username_or_email": owner.username, "password": "WrongPassword!"},
        )
        assert res.status_code == 401
        assert res.json()["error"]["code"] == "AUTH_INVALID_CREDENTIALS"
        assert res.json()["error"]["message"] == "Invalid username or password."


@pytest.mark.asyncio
async def test_admin_login_invalid_username_has_the_same_generic_response(admin_fixture):
    """The browser must not learn whether a username exists."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        res = await client.post(
            "/api/v1/auth/login",
            json={"username_or_email": "does-not-exist", "password": "WrongPassword!"},
        )
        assert res.status_code == 401
        assert res.json()["error"]["code"] == "AUTH_INVALID_CREDENTIALS"
        assert res.json()["error"]["message"] == "Invalid username or password."


@pytest.mark.asyncio
async def test_regular_user_login_denied_admin(admin_fixture):
    """Active viewers can sign in; administrative APIs remain forbidden."""
    user = admin_fixture["user"]
    password = admin_fixture["user_pass"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        res = await client.post(
            "/api/v1/auth/login",
            json={"username_or_email": user.username, "password": password},
        )
        assert res.status_code == 200
        assert (await client.get("/api/v1/admin/videos")).status_code == 403


@pytest.mark.asyncio
async def test_admin_logout_revokes_session(admin_fixture):
    """Test 4: Logout revokes session from database and deletes cookies."""
    owner = admin_fixture["owner"]
    password = admin_fixture["owner_pass"]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        login_res = await client.post(
            "/api/v1/auth/login",
            json={"username_or_email": owner.username, "password": password},
        )
        assert login_res.status_code == 200

        # Logout
        assert (await client.post("/api/v1/auth/logout")).status_code == 403
        logout_res = await client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": client.cookies.get(settings.CSRF_COOKIE_NAME)})
        assert logout_res.status_code == 200

        # Attempt to access protected profile should now fail
        me_res = await client.get("/api/v1/auth/me")
        assert me_res.status_code == 401


@pytest.mark.asyncio
async def test_admin_ui_redirects_unauthenticated():
    """Test 5: Unauthenticated access to /admin, /admin/upload, /admin/videos redirects to /admin/login."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        for route in ("/admin", "/admin/upload", "/admin/videos", "/admin/settings"):
            res = await client.get(route, follow_redirects=False)
            assert res.status_code == 302
            assert "/admin/login" in res.headers["location"]


@pytest.mark.asyncio
async def test_legacy_public_upload_path_is_hidden():
    """The obsolete public entry must not disclose the admin upload URL."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        res = await client.get("/upload", follow_redirects=False)
        assert res.status_code == 404
        assert res.content == b""
        assert "location" not in res.headers


@pytest.mark.asyncio
async def test_authenticated_owner_can_access_admin_ui(admin_fixture):
    """Test 7: Authenticated Owner can access /admin, /admin/upload, and /admin/videos with 200 OK."""
    owner = admin_fixture["owner"]
    password = admin_fixture["owner_pass"]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        login_res = await client.post(
            "/api/v1/auth/login",
            json={"username_or_email": owner.username, "password": password},
        )
        assert login_res.status_code == 200

        for route in ("/admin", "/admin/upload", "/admin/videos", "/admin/settings"):
            res = await client.get(route)
            assert res.status_code == 200
            assert "text/html" in res.headers["content-type"]


@pytest.mark.asyncio
async def test_csrf_protection_on_state_changing_requests(admin_fixture):
    """Test 8: State-changing requests with session cookie require valid X-CSRF-Token header."""
    owner = admin_fixture["owner"]
    password = admin_fixture["owner_pass"]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        login_res = await client.post(
            "/api/v1/auth/login",
            json={"username_or_email": owner.username, "password": password},
        )
        csrf_token = login_res.json()["csrf_token"]

        # Call initiate without CSRF token header -> should fail with 403
        bad_csrf_res = await client.post(
            "/api/v1/uploads/initiate",
            json={
                "title": "CSRF Test",
                "filename": "test.mp4",
                "file_size_bytes": 1048576,
                "chunk_size_bytes": 1048576,
            },
        )
        assert bad_csrf_res.status_code == 403
        assert "csrf" in bad_csrf_res.json()["detail"].lower()

        # Call with matching X-CSRF-Token header -> should succeed with 201
        good_csrf_res = await client.post(
            "/api/v1/uploads/initiate",
            json={
                "title": "CSRF Test Valid",
                "filename": "test.mp4",
                "file_size_bytes": 1048576,
                "chunk_size_bytes": 1048576,
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert good_csrf_res.status_code == 201


@pytest.mark.asyncio
async def test_video_visibility_and_management(admin_fixture):
    """Test 9: Video visibility policies (PRIVATE, UNLISTED, PASSWORD_PROTECTED), Argon2id passwords, and deletion."""
    owner = admin_fixture["owner"]
    password = admin_fixture["owner_pass"]

    async with AsyncSessionLocal() as session:
        # Create a test video in database
        video = Video(
            id=uuid.uuid4(),
            user_id=owner.id,
            title="Initial Test Video",
            source_filename="init.mp4",
            source_size_bytes=5000000,
            status=VideoStatus.UPLOADED,
            visibility=VideoVisibility.PRIVATE,
        )
        session.add(video)
        await session.commit()
        vid_id = video.id

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        # Login
        login_res = await client.post(
            "/api/v1/auth/login",
            json={"username_or_email": owner.username, "password": password},
        )
        csrf = login_res.json()["csrf_token"]

        # 1. Update visibility to UNLISTED -> auto generates share_id
        patch_res = await client.patch(
            f"/api/v1/admin/videos/{vid_id}",
            json={"visibility": "UNLISTED"},
            headers={"X-CSRF-Token": csrf},
        )
        assert patch_res.status_code == 200
        data = patch_res.json()
        assert data["visibility"] == "UNLISTED"
        assert data["share_id"] is not None
        old_share_id = data["share_id"]

        # 2. Rotate share link
        rot_res = await client.patch(
            f"/api/v1/admin/videos/{vid_id}",
            json={"rotate_share_link": True},
            headers={"X-CSRF-Token": csrf},
        )
        assert rot_res.status_code == 200
        new_share_id = rot_res.json()["share_id"]
        assert new_share_id != old_share_id

        # 3. Set PASSWORD_PROTECTED with viewer password (Argon2id)
        pass_res = await client.patch(
            f"/api/v1/admin/videos/{vid_id}",
            json={"visibility": "PASSWORD_PROTECTED", "viewer_password": "SecretViewerPass123!"},
            headers={"X-CSRF-Token": csrf},
        )
        assert pass_res.status_code == 200
        assert pass_res.json()["visibility"] == "PASSWORD_PROTECTED"
        assert pass_res.json()["has_viewer_password"] is True

        # Verify password is not plaintext in DB
        async with AsyncSessionLocal() as session:
            v_db = (await session.execute(select(Video).where(Video.id == vid_id))).scalar_one()
            assert v_db.viewer_password_hash.startswith("$argon2id$")
            assert "SecretViewerPass123!" not in v_db.viewer_password_hash

        # 4. Delete video
        del_res = await client.delete(
            f"/api/v1/admin/videos/{vid_id}",
            headers={"X-CSRF-Token": csrf},
        )
        assert del_res.status_code == 200

        # Verify gone
        get_res = await client.get(f"/api/v1/admin/videos/{vid_id}")
        assert get_res.status_code == 404


@pytest.mark.asyncio
async def test_admin_settings_never_exposes_secrets(admin_fixture):
    """Test 10: Verify /admin/settings never leaks secrets, credentials, or keys."""
    owner = admin_fixture["owner"]
    password = admin_fixture["owner_pass"]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        await client.post(
            "/api/v1/auth/login",
            json={"username_or_email": owner.username, "password": password},
        )
        res = await client.get("/admin/settings")
        assert res.status_code == 200
        content = res.text

        # 1. Secret key & hashes
        assert settings.SECRET_KEY not in content
        # 2. Database passwords & credentials
        assert settings.POSTGRES_PASSWORD not in content
        assert settings.DATABASE_URL not in content
        assert settings.DATABASE_URL_SYNC not in content
        # 3. Redis credentials
        if settings.REDIS_PASSWORD:
            assert settings.REDIS_PASSWORD not in content
        # 4. User passwords
        assert password not in content
        assert owner.hashed_password not in content


@pytest.mark.asyncio
async def test_session_cookie_attributes_and_csrf_isolation(admin_fixture):
    """Test 11: Verify cookie attributes (HttpOnly, SameSite, Path) and CSRF token isolation."""
    owner = admin_fixture["owner"]
    password = admin_fixture["owner_pass"]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        login_res = await client.post(
            "/api/v1/auth/login",
            json={"username_or_email": owner.username, "password": password},
        )
        assert login_res.status_code == 200

        # Inspect raw Set-Cookie headers
        set_cookie_headers = login_res.headers.get_list("set-cookie")
        session_cookie_header = None
        csrf_cookie_header = None

        for h in set_cookie_headers:
            if settings.SESSION_COOKIE_NAME in h:
                session_cookie_header = h
            elif settings.CSRF_COOKIE_NAME in h:
                csrf_cookie_header = h

        assert session_cookie_header is not None, "Session cookie header must exist"
        assert csrf_cookie_header is not None, "CSRF cookie header must exist"

        # Session cookie MUST have HttpOnly, SameSite=strict, Path=/
        assert "httponly" in session_cookie_header.lower()
        assert "samesite=strict" in session_cookie_header.lower()
        assert "path=/" in session_cookie_header.lower()

        # CSRF cookie MUST NOT be HttpOnly (client JS reads it)
        assert "httponly" not in csrf_cookie_header.lower()
        assert "samesite=strict" in csrf_cookie_header.lower()

        # Verify that CSRF token is NOT the raw session token
        session_val = login_res.cookies.get(settings.SESSION_COOKIE_NAME)
        csrf_val = login_res.cookies.get(settings.CSRF_COOKIE_NAME)
        assert session_val != csrf_val, "CSRF token must not be the session token"


@pytest.mark.asyncio
async def test_video_deletion_corrupted_path_traversal_safety(admin_fixture, tmp_path):
    """Test 12: Video deletion with corrupted DB path values never deletes files outside storage root."""
    owner = admin_fixture["owner"]
    password = admin_fixture["owner_pass"]

    # Create a canary file outside storage
    canary_file = tmp_path / "important_outside_file.txt"
    canary_file.write_text("SENSITIVE_OUTSIDE_DATA")
    assert canary_file.exists()

    async with AsyncSessionLocal() as session:
        bad_video = Video(
            id=uuid.uuid4(),
            user_id=owner.id,
            title="Corrupted Path Video",
            source_filename="corrupted.mp4",
            source_path=str(canary_file),
            status=VideoStatus.UPLOADED,
            visibility=VideoVisibility.PRIVATE,
        )
        session.add(bad_video)
        await session.commit()
        bad_vid_id = bad_video.id

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        login_res = await client.post(
            "/api/v1/auth/login",
            json={"username_or_email": owner.username, "password": password},
        )
        csrf = login_res.json()["csrf_token"]

        del_res = await client.delete(
            f"/api/v1/admin/videos/{bad_vid_id}",
            headers={"X-CSRF-Token": csrf},
        )
        # An invalid path must fail closed: the external file and its database
        # record are both preserved so an operator can repair the bad path.
        assert del_res.status_code == 500

        # Canary file outside storage MUST still exist!
        assert canary_file.exists()
        assert canary_file.read_text() == "SENSITIVE_OUTSIDE_DATA"
    async with AsyncSessionLocal() as session:
        retained = await session.scalar(select(Video).where(Video.id == bad_vid_id))
        assert retained is not None
