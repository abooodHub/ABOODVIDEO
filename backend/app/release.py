"""Non-secret release identity embedded at build time."""
import os
import re

from app import __version__


def release_metadata() -> dict[str, str]:
    commit = os.getenv("ABOODVIDEO_RELEASE_COMMIT", "unknown")
    if re.fullmatch(r"[0-9a-fA-F]{7,40}", commit):
        commit = commit[:12].lower()
    else:
        commit = "unknown"
    return {
        "version": __version__,
        "release": os.getenv("ABOODVIDEO_RELEASE_VERSION", __version__),
        "commit": commit,
    }
