"""Privacy-conscious country aggregation and playback country policy."""
import re

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as aioredis

from app.core.network import is_trusted_proxy
from app.models.setting import SystemSetting

GEO_POLICY_KEY = "playback.geo_block_policy"
GEO_POLICY_CACHE_KEY = "settings:playback:geo_block_policy"
_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")


def trusted_country_code(request: Request) -> str | None:
    """Read Cloudflare's country header only from the trusted Nginx hop."""
    peer = request.client.host if request.client else None
    if not is_trusted_proxy(peer):
        return None
    value = (request.headers.get("CF-IPCountry") or "").strip().upper()
    if not _COUNTRY_RE.fullmatch(value) or value in {"XX", "T1"}:
        return None
    return value


async def get_geo_policy(db: AsyncSession, redis_conn: aioredis.Redis) -> dict:
    cached = await redis_conn.get(GEO_POLICY_CACHE_KEY)
    if cached:
        try:
            import json
            policy = json.loads(cached)
            return {"enabled": bool(policy.get("enabled")), "blocked_countries": policy.get("blocked_countries", [])}
        except (ValueError, TypeError):
            pass

    row = await db.get(SystemSetting, GEO_POLICY_KEY)
    policy = {"enabled": False, "blocked_countries": []}
    if row:
        try:
            import json
            value = json.loads(row.value)
            policy = {
                "enabled": value.get("enabled") is True,
                "blocked_countries": sorted({c for c in value.get("blocked_countries", []) if isinstance(c, str) and _COUNTRY_RE.fullmatch(c)}),
            }
        except (ValueError, TypeError, AttributeError):
            pass
    import json
    await redis_conn.setex(GEO_POLICY_CACHE_KEY, 30, json.dumps(policy))
    return policy


async def enforce_geo_policy(request: Request, db: AsyncSession, redis_conn: aioredis.Redis) -> None:
    policy = await get_geo_policy(db, redis_conn)
    if not policy["enabled"]:
        return
    country = trusted_country_code(request)
    # Fail closed while enabled: an absent/invalid country is not silently allowed.
    if country is None or country in policy["blocked_countries"]:
        raise HTTPException(status_code=403, detail="Playback is unavailable from this location")
