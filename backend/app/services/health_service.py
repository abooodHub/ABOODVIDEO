"""Health Check Service orchestrating DB, Redis, and Storage probes."""
from typing import Dict, Any
from app.core.config import settings
from app.core.database import check_db_health
from app.core.redis import check_redis_health
from app.utils.disk import probe_storage_health
from app import __version__


class HealthService:
    @staticmethod
    async def get_db_health() -> Dict[str, Any]:
        return await check_db_health()

    @staticmethod
    async def get_redis_health() -> Dict[str, Any]:
        return await check_redis_health()

    @staticmethod
    def get_storage_health() -> Dict[str, Any]:
        try:
            return probe_storage_health()
        except Exception:
            # Never turn an unavailable volume into an unhandled health request
            # or expose local filesystem paths and exception text.
            return {
                "status": "unhealthy",
                "error": "Storage health probe is temporarily unavailable.",
                "code": "STORAGE_UNAVAILABLE",
            }

    @classmethod
    async def get_overall_health(cls) -> Dict[str, Any]:
        db = await cls.get_db_health()
        redis_res = await cls.get_redis_health()
        storage = cls.get_storage_health()

        services_status = {
            "database": db.get("status", "unhealthy"),
            "redis": redis_res.get("status", "unhealthy"),
            "storage": storage.get("status", "unhealthy"),
        }

        # Overall calculation
        if any(s == "unhealthy" for s in services_status.values()):
            overall = "unhealthy"
        elif any(s == "degraded" for s in services_status.values()):
            overall = "degraded"
        else:
            overall = "healthy"

        return {
            "status": overall,
            "version": __version__,
            "services": services_status,
        }
