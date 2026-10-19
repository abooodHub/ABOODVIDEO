"""Build download names without changing the source container extension."""
from pathlib import Path
import re

_MEDIA_EXTENSIONS = {
    ".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".ts", ".m2ts", ".mts",
    ".mpg", ".mpeg", ".flv", ".wmv", ".asf", ".ogv", ".ogg", ".vob", ".mxf", ".3gp", ".3g2",
}


def build_download_filename(video) -> str:
    extension = Path((video.source_filename or "").replace("\\", "/")).suffix.lower()
    if extension not in _MEDIA_EXTENSIONS:
        extension = Path((video.source_path or "").replace("\\", "/")).suffix.lower()
    if extension not in _MEDIA_EXTENSIONS:
        extension = ".bin"
    title = re.sub(r'[\x00-\x1f\x7f<>:"/\\|?*]', "_", video.title or "video").strip(" .")[:180] or "video"
    if title.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL", *{f"COM{i}" for i in range(1, 10)}, *{f"LPT{i}" for i in range(1, 10)}}:
        title = "_" + title
    return title if title.lower().endswith(extension) else title + extension

