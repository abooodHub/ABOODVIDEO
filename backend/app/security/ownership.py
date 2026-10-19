"""Shared ownership boundary for owner-managed records."""
from fastapi import HTTPException
from app.models.user import UserRole


def owner_scope(user):
    return None if user.role == UserRole.ADMIN else user.id


def require_video_owner(video, user):
    scope = owner_scope(user)
    if video is None or (scope is not None and video.user_id != scope):
        raise HTTPException(status_code=404, detail="Video not found")
    return video
