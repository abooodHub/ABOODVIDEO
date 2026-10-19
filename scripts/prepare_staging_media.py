"""Read original media; write bounded stream-copy excerpts in a new temp folder.

No source media is modified. This is fixture preparation, never a Studio upload.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from studio.core.ffmpeg_finder import get_ffmpeg_path, get_ffprobe_path


def probe(path):
    result = subprocess.run([get_ffprobe_path(), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                            capture_output=True, encoding="utf-8", timeout=60, check=True)
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("h264-ass", "hevc-multiaudio", "hevc-mp4", "srt"):
        parser.add_argument("--"+name, type=Path, required=True)
    parser.add_argument("--hevc10-mkv",type=Path,help="Optional existing 4K HEVC Main 10 MKV")
    parser.add_argument("--start", type=float, default=120)
    parser.add_argument("--seconds", type=float, default=24)
    args = parser.parse_args()
    if not 0 <= args.start or not 1 <= args.seconds <= 60:
        parser.error("Use a nonnegative start and a bounded 1–60 second excerpt")
    if not get_ffmpeg_path() or not get_ffprobe_path():
        parser.error("Existing FFmpeg and FFprobe are required")
    sources = {name: getattr(args, name.replace("-", "_")).resolve(strict=True)
               for name in ("h264-ass", "hevc-multiaudio", "hevc-mp4")}
    if args.hevc10_mkv:
        sources["hevc10-mkv"]=args.hevc10_mkv.resolve(strict=True)
    metadata = {name: probe(path) for name, path in sources.items()}
    for name, data in metadata.items():
        video = next(s for s in data["streams"] if s["codec_type"] == "video")
        expected = "h264" if name == "h264-ass" else "hevc"
        if video["codec_name"] != expected or video["width"] < 3840:
            parser.error(f"{name} must be real 4K {expected}")
        if name in {"h264-ass","hevc-multiaudio"} and not any(s.get("codec_name") == "ass" for s in data["streams"]):
            parser.error(f"{name} requires an embedded ASS track")
        if name == "hevc-multiaudio" and sum(s["codec_type"] == "audio" for s in data["streams"]) < 2:
            parser.error("The multi-audio source needs at least two audio tracks")
        if name == "hevc-mp4" and sources[name].suffix.lower() != ".mp4":
            parser.error("Provide an existing real MP4, not a renamed MKV")
        if name == "hevc10-mkv" and (video.get("pix_fmt")!="yuv420p10le" or sources[name].suffix.lower()!=".mkv"):
            parser.error("The optional Main 10 fixture needs a real yuv420p10le MKV")
    srt = args.srt.resolve(strict=True)
    if srt.suffix.lower() != ".srt":
        parser.error("An existing SRT is required")
    folder = Path(tempfile.mkdtemp(prefix="abv-staging-real-media-"))
    media = folder / "media"; media.mkdir()
    entries = []
    for name, source in sources.items():
        before = source.stat()
        target = media / (name + source.suffix)
        subprocess.run([get_ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-ss", str(args.start), "-i", str(source),
                        "-t", str(args.seconds), "-map", "0:v:0", "-map", "0:a?", "-map", "0:s?", "-map", "0:t?",
                        "-c", "copy", str(target)], capture_output=True, timeout=120, check=True)
        assert source.stat().st_size == before.st_size and source.stat().st_mtime_ns == before.st_mtime_ns
        entries.append({"id": name, "path": str(target), "original": str(source), "original_size": before.st_size,
                        "original_mtime_ns": before.st_mtime_ns, "excerpt_start_seconds": args.start,
                        "requested_excerpt_seconds": args.seconds, "bytes": target.stat().st_size,
                        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "probe": probe(target)})
    shutil.copy2(srt, media / "real-subtitles.srt")
    manifest = folder / "staging-media.json"
    manifest.write_text(json.dumps({"media": entries, "srt": str(media/"real-subtitles.srt"), "srt_original": str(srt),
                                   "srt_sha256": hashlib.sha256(srt.read_bytes()).hexdigest()}, indent=2), encoding="utf-8")
    print("ABV_STAGING_MEDIA_MANIFEST="+str(manifest))


if __name__ == "__main__":
    main()
