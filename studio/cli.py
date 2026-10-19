"""CLI Tool for ABOODVIDEO Studio."""
import argparse
import json
import sys
import time
from pathlib import Path
from studio import __version__

from studio.config import TEMP_DIR, FONTS_DIR, load_studio_config
from studio.core.encoder import transcode_package_renditions
from studio.core.fonts import analyze_ass_fonts
from studio.core.hardware import get_full_telemetry
from studio.core.packager import build_and_verify_package
from studio.core.probe import probe_video
from studio.core.subtitles import convert_to_webvtt
from studio.core.uploader import test_connection, upload_and_verify_package


def cmd_probe(args):
    """Probe video file and print technical specs."""
    p = Path(args.video)
    data = probe_video(p)
    print(json.dumps(data, indent=2, ensure_ascii=False))


def cmd_fonts(args):
    """Analyze ASS fonts."""
    p = Path(args.ass_file)
    data = analyze_ass_fonts(p)
    print(json.dumps(data, indent=2, ensure_ascii=False))


def cmd_hardware(args):
    """Print hardware and NVENC telemetry."""
    data = get_full_telemetry()
    print(json.dumps(data, indent=2, ensure_ascii=False))


def cmd_run(args):
    """Execute full pipeline: probe -> transcode -> package -> upload -> verify."""
    video_path = Path(args.video).resolve()
    qualities = [q.strip() for q in args.qualities.split(",") if q.strip()]

    print(f"=== ABOODVIDEO Studio v{__version__}: Starting Job for {video_path.name} ===")
    probe_data = probe_video(video_path)
    print(f"-> Source: {probe_data['resolution']}, {probe_data['video_codec']}, {probe_data['fps_display']} fps, {probe_data['duration_formatted']}")

    work_dir = TEMP_DIR / f"cli_{int(time.time())}"
    work_dir.mkdir(parents=True, exist_ok=True)

    def on_progress(p_data):
        stage = p_data.get("stage")
        if stage == "transcoding":
            pct = p_data.get("overall_progress_pct", 0)
            cur = p_data.get("current_rendition")
            fps = p_data.get("fps")
            speed = p_data.get("speed")
            eta = p_data.get("eta_formatted")
            print(f"\r[Transcode {pct:.1f}%] Rendition: {cur} | {fps} fps | {speed} | ETA: {eta}  ", end="", flush=True)
        elif stage == "uploading_files":
            pct = p_data.get("upload_progress_pct", 0)
            print(f"\r[Upload {pct:.1f}%] File: {p_data.get('current_file')} | {p_data.get('upload_speed_mbps')} Mbps  ", end="", flush=True)
        elif stage == "server_verifying":
            print(f"\n-> Server Verifying: {p_data.get('server_status')}")

    # Transcode
    print(f"-> Transcoding qualities: {qualities} using {args.encoder}...")
    sub_path = Path(args.subtitle).resolve() if args.subtitle else None
    t_res = transcode_package_renditions(
        source_path=video_path,
        output_dir=work_dir,
        selected_qualities=qualities,
        encoder_choice=args.encoder,
        subtitle_mode=args.subtitle_mode,
        subtitle_path=sub_path,
        fonts_dir=FONTS_DIR,
        progress_callback=on_progress,
    )
    print("\n-> Transcoding finished successfully!")

    # Subtitles
    declared_subs = []
    if args.subtitle_mode == "softsub" and sub_path:
        sub_dir = work_dir / "subtitles"
        sub_dir.mkdir(parents=True, exist_ok=True)
        vtt_target = sub_dir / "ar.vtt"
        convert_to_webvtt(sub_path, vtt_target)
        declared_subs.append({
            "language_code": "ar",
            "language_name": "العربية",
            "label": "العربية",
            "is_default": True,
            "path": "subtitles/ar.vtt",
        })

    # Packaging
    print("-> Assembling and verifying HLS package...")
    pkg_meta = build_and_verify_package(
        package_dir=work_dir,
        title=args.title or video_path.stem,
        duration_seconds=probe_data["duration_seconds"],
        renditions=t_res["renditions"],
        subtitles=declared_subs,
    )
    print(f"-> Package verified: {pkg_meta['total_files']} files, {pkg_meta['total_bytes_formatted']}")

    if not args.upload:
        print(f"-> Package saved locally at: {work_dir}")
        return

    # Upload
    cfg = load_studio_config()
    server_url = args.server_url or cfg.get("server_url")
    api_key = args.api_key or cfg.get("api_key")
    if not api_key:
        print("Error: API key required for upload. Provide --api-key or configure it.")
        sys.exit(1)

    print(f"-> Uploading to {server_url}...")
    upload_res = upload_and_verify_package(
        server_url=server_url,
        api_key=api_key,
        package_dir=work_dir,
        manifest=pkg_meta["manifest"],
        video_id=args.video_id,
        progress_callback=on_progress,
    )
    print(f"\n=== Video Published Successfully! Status: {upload_res['status']} | Video ID: {upload_res['video_id']} ===")


def main():
    parser = argparse.ArgumentParser(description="ABOODVIDEO Studio CLI")
    subparsers = parser.add_subparsers(dest="command")

    p_probe = subparsers.add_parser("probe", help="Probe video metadata")
    p_probe.add_argument("video", help="Path to video file")

    p_fonts = subparsers.add_parser("fonts", help="Analyze ASS fonts")
    p_fonts.add_argument("ass_file", help="Path to .ass file")

    subparsers.add_parser("hardware", help="Hardware & NVENC telemetry")

    p_run = subparsers.add_parser("run", help="Transcode and upload video")
    p_run.add_argument("video", help="Path to video file")
    p_run.add_argument("--qualities", default="1080p,720p,480p,360p", help="Comma-separated qualities")
    p_run.add_argument("--encoder", default="auto", choices=["auto", "nvenc", "cpu"], help="Encoder choice")
    p_run.add_argument("--subtitle-mode", default="none", choices=["none", "hardsub", "softsub"])
    p_run.add_argument("--subtitle", help="Path to subtitle file")
    p_run.add_argument("--title", help="Video title")
    p_run.add_argument("--upload", action="store_true", help="Upload to server after transcoding")
    p_run.add_argument("--server-url", help="ABOODVIDEO server API URL")
    p_run.add_argument("--api-key", help="Personal API Key")
    p_run.add_argument("--video-id", help="Optional existing video ID")

    args = parser.parse_args()
    if args.command == "probe":
        cmd_probe(args)
    elif args.command == "fonts":
        cmd_fonts(args)
    elif args.command == "hardware":
        cmd_hardware(args)
    elif args.command == "run":
        cmd_run(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
