"""Unit and Integration tests for Video Drive & File Manager."""
import uuid
import pytest
from app.services.drive_service import DriveService, format_bytes, format_duration, get_video_quality
from app.models.folder import Folder
from app.models.video import Video, VideoStatus, VideoVisibility


def test_drive_service_format_helpers():
    assert format_bytes(0) == "0 MB"
    assert format_bytes(1048576) == "1 MB"
    assert "GB" in format_bytes(3 * 1024 * 1024 * 1024)

    assert format_duration(None) == "—"
    assert format_duration(65) == "01:05"
    assert format_duration(3665) == "01:01:05"


@pytest.mark.asyncio
async def test_drive_folder_lifecycle(db_session, test_user):
    # 1. Create root folder
    f1 = await DriveService.create_folder(db_session, test_user.id, "Donghua")
    assert f1.name == "Donghua"
    assert f1.parent_id is None

    # 2. Create nested child folder
    f2 = await DriveService.create_folder(db_session, test_user.id, "Spirit Cage", parent_id=f1.id)
    assert f2.name == "Spirit Cage"
    assert f2.parent_id == f1.id

    # 3. Create season folder
    f3 = await DriveService.create_folder(db_session, test_user.id, "الموسم 1", parent_id=f2.id)
    assert f3.parent_id == f2.id

    # 4. Verify breadcrumbs
    crumbs = await DriveService.get_breadcrumbs(db_session, "folder", f3.id)
    assert len(crumbs) == 4
    assert crumbs[0]["name"] == "مكتبة المحتوى"
    assert crumbs[1]["name"] == "Donghua"
    assert crumbs[2]["name"] == "Spirit Cage"
    assert crumbs[3]["name"] == "الموسم 1"

    # 5. Rename folder
    renamed = await DriveService.rename_entity(db_session, "folder", f3.id, "الموسم الأول")
    assert renamed["name"] == "الموسم الأول"

    # 6. Test Tree
    tree_data = await DriveService.get_folder_tree(db_session, test_user.id)
    assert tree_data["total_folders"] >= 3
    assert any(node["name"] == "Donghua" for node in tree_data["tree"])
