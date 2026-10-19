"""P0-2 logical Trash, Restore, bulk normalization, and permanent-delete coverage."""
import uuid

import pytest
from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models.folder import Folder
from app.models.series import Series, Season
from app.models.user import User, UserRole
from app.models.video import Video, VideoStatus
from app.security.hashing import hash_password
from app.services.drive_service import DriveService


@pytest.fixture
async def trash_tree(tmp_path):
    async with AsyncSessionLocal() as db:
        owner = User(id=uuid.uuid4(), email=f"trash_{uuid.uuid4().hex}@test.local", username=f"trash_{uuid.uuid4().hex}", hashed_password=hash_password("TrashTestPassword2026!"), role=UserRole.OWNER, is_active=True)
        folder = Folder(id=uuid.uuid4(), user_id=owner.id, name="Trash root")
        series = Series(id=uuid.uuid4(), owner_id=owner.id, parent_folder_id=folder.id, title="Trash series")
        season = Season(id=uuid.uuid4(), series_id=series.id, season_number=1, display_name="Season 1")
        video_a = Video(id=uuid.uuid4(), user_id=owner.id, title="Episode A", source_filename="a.mp4", status=VideoStatus.READY, series_id=series.id, season_id=season.id, folder_id=folder.id)
        video_b = Video(id=uuid.uuid4(), user_id=owner.id, title="Episode B", source_filename="b.mp4", status=VideoStatus.READY, series_id=series.id, season_id=season.id)
        standalone_path = tmp_path / "keep-me.mp4"
        standalone_path.write_bytes(b"media-must-remain")
        standalone = Video(id=uuid.uuid4(), user_id=owner.id, title="Standalone", source_filename="standalone.mp4", source_path=str(standalone_path), status=VideoStatus.READY)
        other_owner = User(id=uuid.uuid4(), email=f"other_{uuid.uuid4().hex}@test.local", username=f"other_{uuid.uuid4().hex}", hashed_password=hash_password("TrashTestPassword2026!"), role=UserRole.OWNER, is_active=True)
        other_video = Video(id=uuid.uuid4(), user_id=other_owner.id, title="Other owner's video", source_filename="other.mp4", status=VideoStatus.READY)
        db.add_all([owner, folder, series, season, video_a, video_b, standalone, other_owner, other_video])
        await db.commit()
        return {"owner": owner.id, "folder": folder.id, "series": series.id, "season": season.id, "video_a": video_a.id, "video_b": video_b.id, "standalone": standalone.id, "other_video": other_video.id, "path": standalone_path}


@pytest.mark.asyncio
async def test_parent_trash_cascades_and_restore_preserves_one_operation(trash_tree):
    async with AsyncSessionLocal() as db:
        tree = trash_tree
        count, summary = await DriveService.trash_items(db, [
            {"id": str(tree["folder"]), "type": "folder"},
            {"id": str(tree["video_a"]), "type": "فيديو"},
        ])
        assert count == 2
        assert summary == {"folders": 1, "series": 0, "seasons": 0, "videos": 1}
        assert (await db.get(Video, tree["video_a"])).trashed_at is not None
        assert (await db.get(Video, tree["video_b"])).trashed_at is None
        assert (await db.get(Series, tree["series"])).trashed_at is None

        restored = await DriveService.restore_items(db, [{"id": str(tree["folder"]), "type": "folder"}])
        assert restored == 2
        assert (await db.get(Video, tree["video_a"])).trashed_at is None
        assert (await db.get(Video, tree["video_b"])).trashed_at is None


@pytest.mark.asyncio
async def test_legacy_video_label_and_invalid_bulk_are_atomic(trash_tree):
    async with AsyncSessionLocal() as db:
        standalone = trash_tree["standalone"]
        count, _ = await DriveService.trash_items(db, [{"id": str(standalone), "type": "فيديو تجريبي"}])
        assert count == 1
        await DriveService.restore_items(db, [{"id": str(standalone), "type": "video"}])
        with pytest.raises(ValueError):
            await DriveService.trash_items(db, [
                {"id": str(standalone), "type": "video"},
                {"id": str(uuid.uuid4()), "type": "folder"},
            ])
        assert (await db.get(Video, standalone)).trashed_at is None


@pytest.mark.asyncio
async def test_permanent_delete_requires_trash_and_never_removes_media(trash_tree):
    async with AsyncSessionLocal() as db:
        standalone = trash_tree["standalone"]
        with pytest.raises(ValueError):
            await DriveService.permanent_delete_items(db, [{"id": str(standalone), "type": "video"}])
        await DriveService.trash_items(db, [{"id": str(standalone), "type": "حلقة"}])
        assert await DriveService.permanent_delete_items(db, [{"id": str(standalone), "type": "video"}]) == 1
        assert trash_tree["path"].read_bytes() == b"media-must-remain"


@pytest.mark.asyncio
async def test_permanent_folder_delete_removes_its_trashed_logical_subtree(trash_tree):
    async with AsyncSessionLocal() as db:
        tree = trash_tree
        assert (await DriveService.trash_items(db, [{"id": str(tree["folder"]), "type": "folder"}], tree["owner"]))[0] == 2
        assert await DriveService.permanent_delete_items(db, [{"id": str(tree["folder"]), "type": "folder"}], tree["owner"]) == 2
        assert await db.get(Folder, tree["folder"]) is None
        assert await db.get(Series, tree["series"]) is not None
        assert await db.get(Season, tree["season"]) is not None
        assert await db.get(Video, tree["video_a"]) is None
        assert await db.get(Video, tree["video_b"]) is not None


@pytest.mark.asyncio
async def test_series_season_and_video_operations_normalize_nested_bulk(trash_tree):
    async with AsyncSessionLocal() as db:
        tree = trash_tree
        count, summary = await DriveService.trash_items(db, [
            {"id": str(tree["series"]), "type": "series"},
            {"id": str(tree["season"]), "type": "season"},
            {"id": str(tree["video_a"]), "type": "video"},
        ], tree["owner"])
        assert count == 4
        assert summary == {"folders": 0, "series": 1, "seasons": 1, "videos": 2}
        assert await DriveService.restore_items(db, [{"id": str(tree["series"]), "type": "series"}], tree["owner"]) == 4

        assert (await DriveService.trash_items(db, [{"id": str(tree["season"]), "type": "season"}], tree["owner"]))[0] == 3
        assert await DriveService.restore_items(db, [{"id": str(tree["season"]), "type": "season"}], tree["owner"]) == 3
        assert (await DriveService.trash_items(db, [{"id": str(tree["video_a"]), "type": "video"}], tree["owner"]))[0] == 1


@pytest.mark.asyncio
async def test_unrelated_bulk_is_atomic_owned_and_hidden_from_normal_tree(trash_tree):
    async with AsyncSessionLocal() as db:
        tree = trash_tree
        with pytest.raises(ValueError, match="not owned"):
            await DriveService.trash_items(db, [
                {"id": str(tree["standalone"]), "type": "video"},
                {"id": str(tree["other_video"]), "type": "video"},
            ], tree["owner"])
        assert (await db.get(Video, tree["standalone"])).trashed_at is None

        count, _ = await DriveService.trash_items(db, [
            {"id": str(tree["folder"]), "type": "folder"},
            {"id": str(tree["standalone"]), "type": "video"},
        ], tree["owner"])
        assert count == 3
        tree_view = await DriveService.get_folder_tree(db, tree["owner"])
        assert all(item["id"] != str(tree["folder"]) for item in tree_view["tree"])
        trash_view = await DriveService.get_scope_contents(db, tree["owner"], "trash", "trash")
        assert len(trash_view["trash_items"]) == 3


@pytest.mark.asyncio
async def test_library_folders_nest_move_and_keep_episode_metadata(trash_tree):
    async with AsyncSessionLocal() as db:
        tree = trash_tree
        a = await DriveService.create_folder(db, tree["owner"], "A")
        b = await DriveService.create_folder(db, tree["owner"], "B", a.id, owner_id=tree["owner"])
        c = await DriveService.create_folder(db, tree["owner"], "C", b.id, owner_id=tree["owner"])
        destination = await DriveService.create_folder(db, tree["owner"], "Ready")
        video = await db.get(Video, tree["video_a"])
        metadata = (video.series_id, video.season_id, video.episode_number, video.season_number)

        count, _ = await DriveService.move_items(
            db, [{"id": str(b.id), "type": "folder"}], destination.id, owner_id=tree["owner"]
        )
        assert count == 1
        assert (await db.get(Folder, c.id)).parent_id == b.id
        assert (await db.get(Folder, b.id)).parent_id == destination.id

        await DriveService.move_items(
            db, [{"id": str(video.id), "type": "video"}], destination.id, owner_id=tree["owner"]
        )
        moved_video = await db.get(Video, video.id)
        assert moved_video.folder_id == destination.id
        assert (moved_video.series_id, moved_video.season_id, moved_video.episode_number, moved_video.season_number) == metadata

        with pytest.raises(ValueError, match="نفسه أو إلى أحد مجلداته الفرعية"):
            await DriveService.move_items(
                db, [{"id": str(destination.id), "type": "folder"}], c.id, owner_id=tree["owner"]
            )
        assert (await db.get(Folder, destination.id)).parent_id is None


@pytest.mark.asyncio
async def test_folder_trash_uses_only_folder_membership_and_reports_summary(trash_tree):
    async with AsyncSessionLocal() as db:
        tree = trash_tree
        nested = await DriveService.create_folder(db, tree["owner"], "Nested", tree["folder"], owner_id=tree["owner"])
        video_a = await db.get(Video, tree["video_a"])
        video_a.folder_id = nested.id
        await db.commit()
        summary = await DriveService.get_descendant_summary(db, "folder", tree["folder"], tree["owner"])
        assert summary["videos_count"] == 1
        assert summary["folders_count"] == 1
        assert summary["series_count"] == 1
        assert (await DriveService.trash_items(db, [{"id": str(tree["folder"]), "type": "folder"}], tree["owner"]))[0] == 3
        assert (await db.get(Video, tree["video_a"])).trashed_at is not None
        assert (await db.get(Video, tree["video_b"])).trashed_at is None
        assert (await db.get(Series, tree["series"])).trashed_at is None


@pytest.mark.asyncio
async def test_folder_browser_and_search_use_organization_without_losing_metadata(trash_tree):
    async with AsyncSessionLocal() as db:
        tree = trash_tree
        parent = await DriveService.create_folder(db, tree["owner"], "Donghua")
        nested = await DriveService.create_folder(db, tree["owner"], "Spirit Cage", parent.id, owner_id=tree["owner"])
        video = await db.get(Video, tree["video_a"])
        video.folder_id = nested.id
        await db.commit()

        inside = await DriveService.get_scope_contents(db, tree["owner"], str(nested.id), "folder")
        assert any(item["id"] == str(video.id) for item in inside["items"])
        parent_contents = await DriveService.get_scope_contents(db, tree["owner"], str(parent.id), "folder")
        assert any(item["type"] == "folder" and item["name"] == "Spirit Cage" for item in parent_contents["items"])

        search = await DriveService.get_scope_contents(db, tree["owner"], str(parent.id), "folder", "Episode")
        titles = {item["title"] for item in search["items"]}
        assert titles == {"Episode A", "Episode B"}
        result_a = next(item for item in search["items"] if item["title"] == "Episode A")
        assert result_a["location_path"] == "مكتبة المحتوى / Donghua / Spirit Cage"
