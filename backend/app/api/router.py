"""Main API Router aggregation."""
from fastapi import APIRouter
from app.api.v1.health import router as health_router
from app.api.v1.auth import router as auth_router
from app.api.v1.direct_import import router as direct_import_router
from app.api.v1.uploads import router as uploads_router
from app.api.v1.admin_videos import router as admin_videos_router
from app.api.v1.admin_transcodes import router as admin_transcodes_router
from app.api.v1.playback import router as playback_router
from app.api.v1.watch import router as watch_router
from app.api.v1.admin_subtitles import router as admin_subtitles_router
from app.api.v1.admin_subtitles import admin_subtitles_router as admin_subtitles_index_router
from app.api.v1.studio_keys import router as studio_keys_router
from app.api.v1.studio_packages import router as studio_packages_router
from app.api.v1.studio_processing import router as studio_processing_router
from app.api.v1.admin_quality_settings import router as admin_quality_settings_router
from app.api.v1.admin_transfer_settings import router as admin_transfer_settings_router
from app.api.v1.drive import router as drive_router
from app.api.v1.admin_dashboard import router as admin_dashboard_router
from app.api.v1.admin_inspector import router as admin_inspector_router
from app.api.v1.admin_analytics import router as admin_analytics_router
from app.api.v1.admin_embed_settings import router as admin_embed_settings_router

from app.api.v1.admin_users import router as admin_users_router
from app.api.v1.admin_audit import router as admin_audit_router
from app.api.v1.external_identity import router as external_identity_router
from app.api.v1.admin_storage import router as admin_storage_router
api_router = APIRouter()
from app.api.v1.studio_sources import router as studio_sources_router
api_router.include_router(studio_sources_router)

# Include version 1 routers
api_router.include_router(health_router)
api_router.include_router(auth_router)
api_router.include_router(direct_import_router)
api_router.include_router(uploads_router)
api_router.include_router(admin_videos_router)
api_router.include_router(admin_quality_settings_router)
api_router.include_router(admin_transfer_settings_router)
api_router.include_router(admin_embed_settings_router)
api_router.include_router(admin_transcodes_router)
api_router.include_router(playback_router)
api_router.include_router(watch_router)
api_router.include_router(admin_subtitles_router)
api_router.include_router(admin_subtitles_index_router)
api_router.include_router(studio_keys_router, prefix="/studio/keys")
api_router.include_router(studio_keys_router, prefix="/admin/keys")
api_router.include_router(studio_packages_router)
api_router.include_router(studio_processing_router)
api_router.include_router(drive_router)
api_router.include_router(admin_dashboard_router)
api_router.include_router(admin_inspector_router)
api_router.include_router(admin_analytics_router)

api_router.include_router(admin_users_router)

api_router.include_router(admin_audit_router)

api_router.include_router(external_identity_router)

api_router.include_router(admin_storage_router)
