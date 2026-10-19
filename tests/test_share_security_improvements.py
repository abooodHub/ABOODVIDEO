"""Real HTTP/DB regressions for the six defects found in the local audit."""
import asyncio
from datetime import datetime, timedelta, timezone
import re
from types import SimpleNamespace
from urllib.parse import unquote
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.main import create_application
from app.models.folder import Folder
from app.models.series import Series
from app.models.share import Share
from app.models.video import Video, VideoStatus, VideoVisibility
from app.security.hashing import hash_password, verify_password
from app.security.playback_token import VIEWER_COOKIE_NAME
from app.security.share_access import SHARE_COOKIE_NAME
from app.services.download_names import build_download_filename
from test_hidden_home import admin_fixture, playable_video, synthetic_media, PageLinks

PASSWORD = "SharePassword2026!"


@pytest.fixture
async def context(playable_video, synthetic_media, admin_fixture, monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "SERVE_MEDIA_VIA_NGINX", False)
    video = playable_video
    async with AsyncSessionLocal() as db:
        row = await db.get(Video, video.id)
        row.source_path = str(synthetic_media / "sample.mp4")
        row.allow_download_policy = "ALLOW"
        await db.commit()
    app = create_application()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as owner, \
               AsyncClient(transport=transport, base_url="http://localhost") as guest:
        login = await owner.post("/api/v1/auth/login", json={
            "username_or_email": admin_fixture["owner"].username,
            "password": admin_fixture["owner_pass"],
        })
        assert login.status_code == 200
        yield SimpleNamespace(app=app, transport=transport, video=video, owner=owner, guest=guest,
                              headers={settings.CSRF_HEADER_NAME: login.json()["csrf_token"]},
                              users=admin_fixture, media=synthetic_media)


async def create_share(ctx, **options):
    payload = {"target_type": "video", "target_id": str(ctx.video.id), **options}
    response = await ctx.owner.post("/api/v1/drive/shares", json=payload, headers=ctx.headers)
    assert response.status_code == 200, response.text
    return response.json()


def playback_url(response):
    assert response.status_code == 200, response.text
    return re.search(r'"playbackUrl":\s*"([^"]+)"', response.text).group(1)


async def test_password_covers_page_info_watch_and_both_download_routes(context):
    ctx = context
    share = await create_share(ctx, password=PASSWORD, allow_download=True)
    token = share["token"]
    async with AsyncSessionLocal() as db:
        stored = await db.scalar(select(Share).where(Share.token == token))
        assert stored.password_hash != PASSWORD and verify_password(PASSWORD, stored.password_hash)
    page = await ctx.guest.get(share["share_url"])
    assert page.status_code == 200 and 'name="password"' in page.text
    assert ctx.video.title not in page.text
    info_url = f"/api/v1/drive/shares/{token}/info"
    download_paths = [f"/api/v1/drive/shares/{token}/download/{ctx.video.id}",
                      f"/preview/share/{token}/download/{ctx.video.id}"]
    for path in (info_url, *download_paths):
        assert (await ctx.guest.get(path)).status_code == 401
    watch_path = f"/preview/share/{token}/watch/{ctx.video.id}"
    assert 'id="video-element"' not in (await ctx.guest.get(watch_path)).text
    verify_path = f"/api/v1/drive/shares/{token}/verify-password"
    wrong = await ctx.guest.post(verify_path, json={"password": "wrong"})
    assert wrong.status_code == 401 and SHARE_COOKIE_NAME not in wrong.cookies
    ctx.guest.cookies.set(SHARE_COOKIE_NAME, "fixed-cookie")
    unlocked = await ctx.guest.post(verify_path, json={"password": PASSWORD})
    assert unlocked.status_code == 200
    assert unlocked.cookies[SHARE_COOKIE_NAME] != "fixed-cookie"
    assert "httponly" in unlocked.headers["set-cookie"].lower()
    info = await ctx.guest.get(info_url)
    assert info.status_code == 200 and ctx.video.title in info.text
    assert info.headers["cache-control"] == "no-store"
    for path in download_paths:
        response = await ctx.guest.get(path)
        assert response.status_code == 200
        assert response.content == (ctx.media / "sample.mp4").read_bytes()
        assert ".mp4" in unquote(response.headers["content-disposition"])
    assert ctx.video.title in (await ctx.guest.get(share["share_url"])).text
    watch = await ctx.guest.get(watch_path)
    master = playback_url(watch)
    assert (await ctx.guest.get(master)).status_code == 200
    base = master.rsplit("/", 1)[0]
    assert (await ctx.guest.get(base + "/360p/segment000.ts")).status_code == 200
    assert (await ctx.guest.get(base + "/360p/sample.mp4")).status_code == 200
    async with AsyncClient(transport=ctx.transport, base_url="http://localhost") as stranger:
        assert (await stranger.get(info_url)).status_code == 401
        assert (await stranger.get(master)).status_code in (401, 403)


async def test_password_form_and_rate_limit(context, monkeypatch):
    ctx = context
    share = await create_share(ctx, password=PASSWORD)
    path = share["share_url"] + "/unlock"
    wrong = await ctx.guest.post(path, data={"password": "wrong"})
    assert wrong.status_code == 401 and 'name="password"' in wrong.text
    correct = await ctx.guest.post(path, data={"password": PASSWORD}, follow_redirects=False)
    assert correct.status_code == 303 and correct.headers["location"] == share["share_url"]
    assert ctx.video.title in (await ctx.guest.get(share["share_url"])).text
    monkeypatch.setattr(settings, "RATE_LIMIT_VIEWER_PASSWORD_ATTEMPTS", 2)
    api = f"/api/v1/drive/shares/{share['token']}/verify-password"
    for _ in range(2):
        assert (await ctx.guest.post(api, json={"password": "wrong"})).status_code == 401
    blocked = await ctx.guest.post(api, json={"password": PASSWORD})
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) > 0
    assert SHARE_COOKIE_NAME not in blocked.cookies


@pytest.mark.parametrize("visibility", [VideoVisibility.PUBLIC, VideoVisibility.UNLISTED, VideoVisibility.PRIVATE])
async def test_share_watch_is_scoped_and_direct_private_watch_stays_closed(context, visibility):
    ctx = context
    async with AsyncSessionLocal() as db:
        row = await db.get(Video, ctx.video.id)
        row.visibility = visibility
        await db.commit()
    share = await create_share(ctx)
    page = await ctx.guest.get(share["share_url"])
    links = PageLinks()
    links.feed(page.text)
    watch_path = f"/preview/share/{share['token']}/watch/{ctx.video.id}"
    viewer = ctx.guest
    if visibility == VideoVisibility.PRIVATE:
        assert watch_path not in links.hrefs
        assert (await ctx.guest.get(watch_path)).status_code == 404
        assert (await ctx.guest.get("/watch/" + ctx.video.share_id)).status_code == 404
        viewer = ctx.owner
    else:
        assert watch_path in links.hrefs
    master = playback_url(await viewer.get(watch_path))
    assert (await viewer.get(master)).status_code == 200
    assert (await ctx.guest.get(f"/preview/share/{share['token']}/watch/{uuid.uuid4()}")).status_code == 404
    assert (await ctx.guest.get(f"/preview/share/{share['token']}/download/{ctx.video.id}")).status_code == 403
    if visibility == VideoVisibility.PRIVATE:
        logout = await ctx.owner.post("/api/v1/auth/logout", headers=ctx.headers)
        assert logout.status_code == 200
        assert (await ctx.owner.get(master)).status_code in (401, 403)


async def test_share_password_does_not_bypass_the_videos_own_password(context):
    ctx = context
    async with AsyncSessionLocal() as db:
        row = await db.get(Video, ctx.video.id)
        row.visibility = VideoVisibility.PASSWORD_PROTECTED
        row.viewer_password_hash = hash_password("VideoPassword2026!")
        await db.commit()
    share = await create_share(ctx, password=PASSWORD)
    unlock = f"/api/v1/drive/shares/{share['token']}/verify-password"
    assert (await ctx.guest.post(unlock, json={"password": PASSWORD})).status_code == 200
    path = f"/preview/share/{share['token']}/watch/{ctx.video.id}"
    prompt = await ctx.guest.get(path)
    assert 'id="password-form"' in prompt.text
    assert 'id="video-element"' not in prompt.text
    verified = await ctx.guest.post(f"/api/v1/watch/{ctx.video.share_id}/verify-password",
                                    json={"password": "VideoPassword2026!"})
    assert verified.status_code == 200
    master = playback_url(await ctx.guest.get(path))
    assert (await ctx.guest.get(master)).status_code == 200


@pytest.mark.parametrize("change", ["revoke", "expire", "password", "move", "trash"])
async def test_media_authorization_rechecks_the_share_and_scope(context, change):
    ctx = context
    async with AsyncSessionLocal() as db:
        folder = Folder(id=uuid.uuid4(), user_id=ctx.video.user_id, name="Scoped audit folder")
        db.add(folder)
        await db.flush()
        video = await db.get(Video, ctx.video.id)
        video.folder_id = folder.id
        await db.commit()
    share = await create_share(ctx, target_type="folder", target_id=str(folder.id), password=PASSWORD)
    unlock = f"/api/v1/drive/shares/{share['token']}/verify-password"
    assert (await ctx.guest.post(unlock, json={"password": PASSWORD})).status_code == 200
    master = playback_url(await ctx.guest.get(f"/preview/share/{share['token']}/watch/{ctx.video.id}"))
    assert (await ctx.guest.get(master)).status_code == 200
    async with AsyncSessionLocal() as db:
        stored = await db.scalar(select(Share).where(Share.token == share["token"]))
        video = await db.get(Video, ctx.video.id)
        if change == "revoke":
            stored.revoked_at = datetime.now(timezone.utc)
        elif change == "expire":
            stored.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        elif change == "password":
            stored.password_hash = hash_password("ChangedSharePassword!")
        elif change == "move":
            video.folder_id = None
        else:
            video.trashed_at = datetime.now(timezone.utc)
        await db.commit()
    assert (await ctx.guest.get(master)).status_code in (401, 403)


async def test_password_update_preserves_omitted_password_and_invalidates_old_grants(context):
    ctx = context
    share = await create_share(ctx, password=PASSWORD)
    verify = f"/api/v1/drive/shares/{share['token']}/verify-password"
    assert (await ctx.guest.post(verify, json={"password": PASSWORD})).status_code == 200
    await create_share(ctx, allow_download=True)
    info = f"/api/v1/drive/shares/{share['token']}/info"
    assert (await ctx.guest.get(info)).status_code == 200
    changed = await create_share(ctx, password="ChangedSharePassword!")
    assert changed["token"] == share["token"]
    assert (await ctx.guest.get(info)).status_code == 401
    assert (await ctx.guest.post(verify, json={"password": PASSWORD})).status_code == 401
    assert (await ctx.guest.post(verify, json={"password": "ChangedSharePassword!"})).status_code == 200
    await create_share(ctx, password=None)
    async with AsyncClient(transport=ctx.transport, base_url="http://localhost") as stranger:
        assert (await stranger.get(info)).status_code == 200


async def test_unlock_is_share_specific_and_secure_cookie_is_supported(context, monkeypatch):
    ctx = context
    first = await create_share(ctx, password=PASSWORD)
    async with AsyncSessionLocal() as db:
        folder = Folder(id=uuid.uuid4(), user_id=ctx.video.user_id, name="Other protected share")
        db.add(folder)
        await db.commit()
    second = await create_share(ctx, target_type="folder", target_id=str(folder.id), password=PASSWORD)
    monkeypatch.setattr(settings, "SESSION_COOKIE_SECURE", True)
    async with AsyncClient(transport=ctx.transport, base_url="https://localhost") as guest:
        response = await guest.post(f"/api/v1/drive/shares/{first['token']}/verify-password",
                                    json={"password": PASSWORD})
        assert response.status_code == 200
        assert "secure" in response.headers["set-cookie"].lower()
        assert (await guest.get(f"/api/v1/drive/shares/{first['token']}/info")).status_code == 200
        assert (await guest.get(f"/api/v1/drive/shares/{second['token']}/info")).status_code == 401


async def test_share_request_rejects_invalid_expiry_and_oversized_password(context):
    ctx = context
    for options in ({"expires_days": -1}, {"password": "p" * 1025}):
        response = await ctx.owner.post("/api/v1/drive/shares", headers=ctx.headers,
                                       json={"target_type": "video", "target_id": str(ctx.video.id), **options})
        assert response.status_code == 422


async def test_expired_share_renewal_and_active_lookup_do_not_conflict(context):
    ctx = context
    async with AsyncSessionLocal() as db:
        expired = Share(owner_id=ctx.video.user_id, target_type="video", target_id=ctx.video.id,
                        token=uuid.uuid4().hex, expires_at=datetime.now(timezone.utc) - timedelta(days=1))
        db.add(expired)
        await db.commit()
    lookup = f"/api/v1/drive/shares/target/video/{ctx.video.id}"
    assert (await ctx.owner.get(lookup)).json()["exists"] is False
    first = await create_share(ctx, expires_days=1)
    second = await create_share(ctx, expires_days=2)
    assert first["token"] == second["token"]
    assert (await ctx.owner.get(lookup)).json()["token"] == first["token"]
    async with AsyncSessionLocal() as db:
        assert await db.get(Share, expired.id) is not None


async def test_concurrent_share_creation_keeps_one_active_link(context):
    ctx = context
    results = await asyncio.gather(*(create_share(ctx) for _ in range(4)))
    assert len({result["token"] for result in results}) == 1
    async with AsyncSessionLocal() as db:
        count = await db.scalar(select(func.count()).select_from(Share).where(Share.target_id == ctx.video.id))
        assert count == 1


@pytest.mark.parametrize("structured", [False, True])
async def test_series_neighbors_stay_within_owner_and_active_series(context, structured):
    ctx = context
    series_name = "Scoped series " + uuid.uuid4().hex
    async with AsyncSessionLocal() as db:
        series = Series(id=uuid.uuid4(), owner_id=ctx.video.user_id, title=series_name)
        other_series = Series(id=uuid.uuid4(), owner_id=ctx.video.user_id, title=series_name)
        db.add_all([series, other_series])
        await db.flush()
        lead = await db.get(Video, ctx.video.id)
        lead.series_name = series_name
        lead.series_id = series.id if structured else None
        lead.episode_number = 1
        rows = []
        for label, owner_id, trashed, series_id in (
            ("allowed", ctx.video.user_id, None, lead.series_id),
            ("other-owner", ctx.users["admin"].id, None, lead.series_id),
            ("trashed", ctx.video.user_id, datetime.now(timezone.utc), lead.series_id),
            ("other-series", ctx.video.user_id, None, other_series.id),
        ):
            row = Video(id=uuid.uuid4(), user_id=owner_id, title=label, source_filename="sample.mp4",
                        series_name=series_name, series_id=series_id, episode_number=2,
                        status=VideoStatus.READY, visibility=VideoVisibility.UNLISTED,
                        active_generation_id=uuid.uuid4(), share_id=uuid.uuid4().hex, trashed_at=trashed)
            db.add(row)
            rows.append(row)
        await db.commit()
    page = await ctx.guest.get("/watch/" + ctx.video.share_id)
    assert page.status_code == 200
    assert rows[0].share_id in page.text
    for row in rows[1:]:
        assert row.share_id not in page.text


@pytest.mark.parametrize("state", ["private", "trashed", "processing", "revoked", "no-generation", "missing"])
async def test_password_verification_hides_unavailable_videos(context, state):
    ctx = context
    async with AsyncSessionLocal() as db:
        row = await db.get(Video, ctx.video.id)
        row.visibility = VideoVisibility.PASSWORD_PROTECTED
        row.viewer_password_hash = hash_password(PASSWORD)
        if state == "private":
            row.visibility = VideoVisibility.PRIVATE
        elif state == "trashed":
            row.trashed_at = datetime.now(timezone.utc)
        elif state == "processing":
            row.status = VideoStatus.PROCESSING
        elif state == "revoked":
            row.share_revoked = True
        elif state == "no-generation":
            row.active_generation_id = None
        await db.commit()
    share_id = uuid.uuid4().hex if state == "missing" else ctx.video.share_id
    from app.core.redis import get_session_client
    await get_session_client().set(f"ratelimit:pw:127.0.0.1:{share_id}",
                                   settings.RATE_LIMIT_VIEWER_PASSWORD_ATTEMPTS, ex=60)
    response = await ctx.guest.post(f"/api/v1/watch/{share_id}/verify-password", json={"password": PASSWORD})
    assert response.status_code == 404
    assert VIEWER_COOKIE_NAME not in response.cookies


@pytest.mark.parametrize("filename,title,expected", [
    ("a.mp4", "Video", "Video.mp4"), ("a.mkv", "Video", "Video.mkv"),
    (r"C:\source\a.MP4", "فيديو", "فيديو.mp4"),
    ("a.webm", "Video.webm", "Video.webm"),
    ("a.mp4", "../bad\r\nname", "_bad__name.mp4"),
])
def test_download_names_keep_source_extension_and_safe_title(filename, title, expected):
    video = SimpleNamespace(source_filename=filename, source_path=None, title=title)
    assert build_download_filename(video) == expected


async def test_owner_download_preserves_mp4_extension(context):
    response = await context.owner.get(f"/api/v1/drive/download/{context.video.id}")
    assert response.status_code == 200
    assert ".mp4" in unquote(response.headers["content-disposition"])
    assert response.content == (context.media / "sample.mp4").read_bytes()
