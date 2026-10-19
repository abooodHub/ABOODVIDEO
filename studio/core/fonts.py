"""ASS Subtitle Font Inspection, MKV Extraction, ZIP Import, and Font Packaging System."""
import hashlib
import json
import logging
import os
import re
import shutil
import struct
import subprocess
import zipfile
from pathlib import Path
from typing import Dict, Any, List, Set, Tuple, Optional

try:
    import winreg
except ImportError:
    winreg = None

from studio.config import FONTS_DIR, MANAGED_FONTS_DIR, TEMP_DIR, USER_DATA_DIR, load_studio_config
from studio.core.process import run_headless

logger = logging.getLogger("aboodvideo.studio.fonts")
FONT_LIBRARY_INDEX_FILE = USER_DATA_DIR / "font_library_index.json"
FONT_EXTENSIONS = {".ttf", ".otf", ".ttc"}
FONT_LIBRARY_INDEX_SCHEMA = 2


def _store_font_bytes(target_dir: Path, filename: str, data: bytes) -> Path:
    """Preserve same-named existing fonts, including package files from prior runs."""
    name = Path(filename).name
    ext = Path(name).suffix.casefold()
    if ext not in FONT_EXTENSIONS:
        raise ValueError("Only TTF, OTF and TTC fonts are supported")
    sha = hashlib.sha256(data).hexdigest()
    # Use a portable name if uploaded metadata contains Windows-invalid characters.
    if re.search(r'[<>:"/\\|?*\x00-\x1f]', name):
        name = f"font-{sha}{ext}"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / name
    if target.exists() and (not target.is_file() or compute_sha256(target) != sha):
        target = target_dir / f"font-{sha}{ext}"
    try:
        with target.open("xb") as output:
            output.write(data)
    except FileExistsError:
        if not target.is_file() or compute_sha256(target) != sha:
            raise ValueError("Font destination already contains different data")
    return target


def extract_font_names_from_ttf(path: Path) -> Set[str]:
    """
    Pure Python parser extracting Font Family and Full Name from TTF / OTF / TTC name table.
    Works without external dependencies.
    """
    names: Set[str] = set()
    try:
        with open(path, "rb") as f:
            header = f.read(12)
            if len(header) < 12:
                return names

            # Check if TTC (TrueType Collection)
            if header[:4] == b"ttcf":
                f.seek(8)
                num_fonts = struct.unpack(">I", f.read(4))[0]
                offsets = [struct.unpack(">I", f.read(4))[0] for _ in range(min(num_fonts, 8))]
            else:
                offsets = [0]

            for font_offset in offsets:
                f.seek(font_offset)
                font_hdr = f.read(12)
                if len(font_hdr) < 12:
                    continue
                num_tables = struct.unpack(">H", font_hdr[4:6])[0]

                table_data = f.read(num_tables * 16)
                for i in range(num_tables):
                    entry = table_data[i * 16 : (i + 1) * 16]
                    tag = entry[:4]
                    if tag == b"name":
                        tbl_offset = struct.unpack(">I", entry[8:12])[0]
                        tbl_length = struct.unpack(">I", entry[12:16])[0]

                        f.seek(tbl_offset)
                        n_data = f.read(min(tbl_length, 65536))
                        if len(n_data) < 6:
                            continue

                        _, count, string_offset = struct.unpack(">HHH", n_data[:6])
                        rec_offset = 6
                        for _ in range(count):
                            if rec_offset + 12 > len(n_data):
                                break
                            platform_id, encoding_id, _, name_id, length, str_offset = struct.unpack(
                                ">HHHHHH", n_data[rec_offset : rec_offset + 12]
                            )
                            rec_offset += 12

                            if name_id in (1, 4, 16):
                                start = string_offset + str_offset
                                end = start + length
                                if end <= len(n_data):
                                    raw = n_data[start:end]
                                    # OpenType names use platform-specific encodings.
                                    # Even-length Macintosh names also decode as UTF-16,
                                    # but produce gibberish and hide valid family aliases.
                                    encodings = ("mac_roman", "utf-8", "latin-1") if platform_id == 1 else ("utf-16-be", "utf-8", "latin-1")
                                    for enc in encodings:
                                        try:
                                            val = raw.decode(enc).strip("\x00").strip()
                                            if val:
                                                names.add(val)
                                            break
                                        except Exception:
                                            continue
    except Exception:
        pass

    return names


def _extract_font_family_style(path: Path) -> Tuple[str, str]:
    """Read family/subfamily name IDs from an OpenType name table."""
    family_names: Dict[int, List[str]] = {1: [], 16: []}
    style_names: Dict[int, List[str]] = {2: [], 17: []}
    try:
        data = Path(path).read_bytes()
        offsets = [0]
        if data[:4] == b"ttcf" and len(data) >= 12:
            count = struct.unpack_from(">I", data, 8)[0]
            offsets = [struct.unpack_from(">I", data, 12 + index * 4)[0]
                       for index in range(min(count, 32))
                       if 12 + index * 4 + 4 <= len(data)]
        for font_offset in offsets:
            if font_offset + 12 > len(data):
                continue
            table_count = struct.unpack_from(">H", data, font_offset + 4)[0]
            table_directory_end = font_offset + 12 + table_count * 16
            if table_directory_end > len(data):
                continue
            name_offset = None
            for index in range(table_count):
                entry = font_offset + 12 + index * 16
                if data[entry:entry + 4] == b"name":
                    name_offset, name_length = struct.unpack_from(">II", data, entry + 8)
                    if name_offset + min(name_length, 1_048_576) > len(data):
                        name_offset = None
                    break
            if name_offset is None or name_offset + 6 > len(data):
                continue
            _, record_count, strings_offset = struct.unpack_from(">HHH", data, name_offset)
            records_start = name_offset + 6
            for index in range(min(record_count, 4096)):
                record = records_start + index * 12
                if record + 12 > len(data):
                    break
                platform, _, _, name_id, length, string_offset = struct.unpack_from(">HHHHHH", data, record)
                if name_id in family_names:
                    target = family_names[name_id]
                else:
                    target = style_names.get(name_id)
                if target is None:
                    continue
                start = name_offset + strings_offset + string_offset
                end = start + length
                if end > len(data):
                    continue
                raw = data[start:end]
                encodings = ("utf-16-be", "utf-8", "latin-1") if platform in (0, 3) else ("mac_roman", "utf-8", "latin-1")
                for encoding in encodings:
                    try:
                        value = raw.decode(encoding).strip("\x00").strip()
                    except (UnicodeDecodeError, LookupError):
                        continue
                    if value:
                        target.append(value)
                        break
    except (OSError, struct.error, ValueError):
        return "", ""

    family = next((name for key in (16, 1) for name in family_names[key]), "")
    style = next((name for key in (17, 2) for name in style_names[key]), "Regular")
    return family, style


def compute_sha256(path: Path) -> str:
    """Compute SHA-256 hash of a file."""
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1048576):
            hasher.update(chunk)
    return hasher.hexdigest().lower()


def get_windows_installed_fonts() -> Dict[str, Dict[str, Any]]:
    """
    Scans Windows Font Registry and Windows Fonts directories.
    Returns mapping of lowercase font name to metadata.
    """
    installed: Dict[str, Dict[str, Any]] = {}
    if not winreg:
        return installed

    reg_paths = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
    ]

    for root_key, sub_key in reg_paths:
        try:
            with winreg.OpenKey(root_key, sub_key) as key:
                count = winreg.QueryInfoKey(key)[1]
                for i in range(count):
                    try:
                        val_name, val_data, _ = winreg.EnumValue(key, i)
                        clean_name = re.sub(
                            r"\s*\((?:TrueType|OpenType|PostScript)\)\s*$", "", val_name, flags=re.IGNORECASE
                        ).strip()

                        # Determine full path
                        font_file_path = val_data
                        if not os.path.isabs(font_file_path):
                            cand_win = Path(os.environ.get("WINDIR", "C:\\Windows")) / "Fonts" / font_file_path
                            cand_user = (
                                Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts" / font_file_path
                            )
                            if cand_win.exists():
                                font_file_path = str(cand_win)
                            elif cand_user.exists():
                                font_file_path = str(cand_user)

                        item = {
                            "name": clean_name,
                            "file": font_file_path,
                            "source": "windows_registry",
                        }
                        installed[clean_name.lower()] = item

                        family_base = re.sub(
                            r"\s+(?:Regular|Bold|Italic|Medium|Light|SemiBold|Black|Thin)\b",
                            "",
                            clean_name,
                            flags=re.IGNORECASE,
                        ).strip()
                        if family_base.lower() not in installed:
                            installed[family_base.lower()] = item
                    except Exception:
                        continue
        except Exception:
            pass

    return installed


def get_studio_attached_fonts() -> Dict[str, Dict[str, Any]]:
    """Scans bundled and persistent Studio-managed font directories."""
    attached: Dict[str, Dict[str, Any]] = {}
    # Scan bundled fonts first and persistent user-managed fonts second so
    # managed files take precedence for an exact family name.
    for font_dir in dict.fromkeys((FONTS_DIR, MANAGED_FONTS_DIR)):
        if not font_dir.exists():
            continue
        for font_path in font_dir.glob("*.*"):
            if font_path.suffix.lower() not in (".ttf", ".otf", ".ttc"):
                continue
            extracted_names = extract_font_names_from_ttf(font_path)
            stem_clean = font_path.stem.replace("-", " ").replace("_", " ")

            item = {
                "name": font_path.stem,
                "path": str(font_path),
                "filename": font_path.name,
                "source": "studio_managed" if font_dir == MANAGED_FONTS_DIR else "studio_fonts",
                "extracted_names": list(extracted_names),
            }

            for n in extracted_names:
                attached[n.lower()] = item
                base = re.sub(
                    r"\s+(?:Regular|Bold|Italic|Medium|Light|SemiBold|Black|Thin)\b",
                    "",
                    n,
                    flags=re.IGNORECASE,
                ).strip()
                if base.lower() not in attached:
                    attached[base.lower()] = item

            if stem_clean.lower() not in attached:
                attached[stem_clean.lower()] = item

    return attached


def _configured_font_library_root() -> Optional[Path]:
    value = str(load_studio_config().get("font_library_folder") or "").strip().strip('"')
    if not value:
        return None
    return Path(value).expanduser().resolve()


def get_font_library_index(
    *, force_refresh: bool = False, root: Optional[Path] = None,
    cache_file: Optional[Path] = None,
) -> Dict[str, Any]:
    """Return the flat external font library index, reparsing only changed files."""
    directory = Path(root).expanduser().resolve() if root is not None else _configured_font_library_root()
    cache_path = Path(cache_file) if cache_file is not None else FONT_LIBRARY_INDEX_FILE
    if directory is None or not directory.is_dir():
        return {"root": str(directory) if directory else "", "files": [], "broken_files": [], "families": 0}

    file_stats = []
    for font_path in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
        if not font_path.is_file() or font_path.suffix.casefold() not in FONT_EXTENSIONS:
            continue
        try:
            stat = font_path.stat()
        except OSError as exc:
            logger.warning("Cannot stat external font %s: %s", font_path.name, type(exc).__name__)
            continue
        file_stats.append({"filename": font_path.name, "mtime_ns": stat.st_mtime_ns, "size_bytes": stat.st_size})

    cached = None
    if not force_refresh and cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Cannot read font index cache: %s", type(exc).__name__)
    if (
        cached and cached.get("schema_version") == FONT_LIBRARY_INDEX_SCHEMA
        and cached.get("root") == str(directory)
        and cached.get("fingerprint") == file_stats
        and isinstance(cached.get("files"), list)
    ):
        return cached

    entries = []
    broken = []
    for record in file_stats:
        font_path = directory / record["filename"]
        names = sorted(extract_font_names_from_ttf(font_path), key=str.casefold)
        if not names:
            broken.append(record["filename"])
            continue
        family, style = _extract_font_family_style(font_path)
        if not family:
            broken.append(record["filename"])
            continue
        entries.append({
            "family": family,
            "style": style,
            "filename": record["filename"],
            "full_path": str(font_path),
            "mtime": font_path.stat().st_mtime,
            "mtime_ns": record["mtime_ns"],
            "size_bytes": record["size_bytes"],
            "names": names,
        })
    families = len({entry["family"].casefold() for entry in entries})
    result = {
        "schema_version": FONT_LIBRARY_INDEX_SCHEMA,
        "root": str(directory), "fingerprint": file_stats,
        "files": entries, "broken_files": broken, "families": families,
    }
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("Cannot persist font index cache: %s", type(exc).__name__)
    return result


def get_font_library_attached_fonts(*, force_refresh: bool = False) -> Dict[str, Dict[str, Any]]:
    """Map external font families/names to their original files without copying them."""
    index = get_font_library_index(force_refresh=force_refresh)
    attached: Dict[str, Dict[str, Any]] = {}
    for entry in index.get("files", []):
        item = {
            "name": entry["family"], "path": entry["full_path"],
            "filename": entry["filename"], "source": "font_library",
            "extracted_names": list(entry.get("names", [])),
        }
        for name in entry.get("names", []):
            attached[name.casefold()] = item
            base = re.sub(r"\s+(?:Regular|Bold|Italic|Medium|Light|SemiBold|Black|Thin|ExtraBold|Condensed)\b", "", name, flags=re.IGNORECASE).strip()
            attached.setdefault(base.casefold(), item)
        attached.setdefault(entry["family"].casefold(), item)
    return attached


def get_font_library_summary(*, force_refresh: bool = False) -> Dict[str, Any]:
    """Return non-secret Settings data for the external library and its cache."""
    index = get_font_library_index(force_refresh=force_refresh)
    return {
        "root": index.get("root", ""),
        "file_count": len(index.get("files", [])),
        "family_count": index.get("families", 0),
        "broken_count": len(index.get("broken_files", [])),
        "broken_files": list(index.get("broken_files", [])),
    }


def apply_font_library_to_audit(audit: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve previously missing ASS families from the external font library."""
    updated = json.loads(json.dumps(audit or {}, ensure_ascii=False))
    lookup = get_font_library_attached_fonts()
    fonts = updated.get("fonts")
    if not isinstance(fonts, list):
        return updated
    for item in fonts:
        if not isinstance(item, dict) or item.get("status") != "missing":
            continue
        name = str(item.get("name") or "").strip()
        loose = re.sub(r"\s+(?:Regular|Bold|Italic|Medium|Light|SemiBold|Black|Thin|ExtraBold|Condensed)\b", "", name, flags=re.IGNORECASE).strip()
        match = lookup.get(name.casefold()) or lookup.get(loose.casefold())
        if not match:
            continue
        item.update({
            "status": "font_library", "source": "font_library",
            "file": match["path"], "status_label": "مجلد الخطوط",
        })
        try:
            item["sha256"] = compute_sha256(Path(match["path"]))
        except OSError as exc:
            logger.warning("Cannot hash matched external font %s: %s", match["filename"], type(exc).__name__)
    updated["available_count"] = sum(1 for item in fonts if item.get("status") != "missing")
    updated["missing_font_names"] = [item.get("name", "") for item in fonts if item.get("status") == "missing"]
    updated["missing_count"] = len(updated["missing_font_names"])
    updated["all_fonts_present"] = updated["missing_count"] == 0
    updated["total_fonts"] = len(fonts)
    return updated


def get_season_folder_fonts(season_fonts_dir: Optional[Path]) -> Dict[str, Dict[str, Any]]:
    """Scans the season fonts/ directory for font files (.ttf, .otf, .ttc)."""
    attached: Dict[str, Dict[str, Any]] = {}
    if not season_fonts_dir:
        return attached
    p = Path(season_fonts_dir).resolve()
    if not p.exists() or not p.is_dir():
        return attached

    for font_path in p.glob("*.*"):
        if font_path.suffix.lower() in (".ttf", ".otf", ".ttc"):
            extracted_names = extract_font_names_from_ttf(font_path)
            stem_clean = font_path.stem.replace("-", " ").replace("_", " ")

            item = {
                "name": font_path.stem,
                "path": str(font_path),
                "filename": font_path.name,
                "source": "season_fonts",
                "extracted_names": list(extracted_names),
            }

            for n in extracted_names:
                attached[n.lower()] = item
                base = re.sub(
                    r"\s+(?:Regular|Bold|Italic|Medium|Light|SemiBold|Black|Thin)\b",
                    "",
                    n,
                    flags=re.IGNORECASE,
                ).strip()
                if base.lower() not in attached:
                    attached[base.lower()] = item

            if stem_clean.lower() not in attached:
                attached[stem_clean.lower()] = item

    return attached


def extract_mkv_font_attachments(mkv_path: Path, output_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """
    Inspects and dumps all font attachments embedded in an MKV file using ffprobe/ffmpeg.
    Deduplicates font binaries by SHA-256 so duplicate attachments are saved only once.
    """
    mkv_path = Path(mkv_path).resolve()
    if not mkv_path.exists() or mkv_path.suffix.lower() != ".mkv":
        return []

    stat = mkv_path.stat()
    identity = f"{mkv_path}:{stat.st_size}:{stat.st_mtime_ns}"
    target_dir = output_dir or (TEMP_DIR / f"mkv_fonts_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]}")
    target_dir.mkdir(parents=True, exist_ok=True)

    from studio.core.ffmpeg_finder import get_ffmpeg_path, get_ffprobe_path
    ffprobe_bin = get_ffprobe_path()
    ffmpeg_bin = get_ffmpeg_path()
    if not ffprobe_bin or not ffmpeg_bin:
        return []

    # Probe attachment streams
    cmd = [
        ffprobe_bin,
        "-v", "error",
        "-select_streams", "t",
        "-show_entries", "stream=index:stream_tags=filename,mimetype",
        "-of", "json",
        str(mkv_path),
    ]
    try:
        proc = run_headless(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", timeout=15, check=False)
        if proc.returncode != 0:
            return []
        data = json.loads(proc.stdout)
    except Exception:
        return []

    extracted: List[Dict[str, Any]] = []
    seen_hashes: Set[str] = set()
    candidates = []
    dump_cmd = [ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y"]
    for stream in data.get("streams", []):
        idx = stream.get("index")
        if not isinstance(idx, int) or idx < 0:
            continue
        tags = stream.get("tags", {})
        fname = tags.get("filename", f"attachment_{idx}.bin")
        ext = Path(fname).suffix.lower()

        # Only process fonts
        if ext not in (".ttf", ".otf", ".ttc") and not tags.get("mimetype", "").startswith("font/"):
            continue

        safe_name = f"mkv_stream_{idx}{ext if ext in ('.ttf', '.otf', '.ttc') else '.ttf'}"
        target_file = target_dir / safe_name
        candidates.append((idx, fname, target_file))
        dump_cmd.extend([f"-dump_attachment:{idx}", str(target_file)])
    if not candidates:
        return []
    dump_cmd.extend(["-i", str(mkv_path), "-t", "0", "-f", "null", "-"])
    try:
        run_headless(dump_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        logger.warning("Font attachment extraction incomplete for %s", mkv_path.name)
    for idx, fname, target_file in candidates:
        if target_file.exists() and target_file.stat().st_size > 0:
            sha = compute_sha256(target_file)
            if sha in seen_hashes:
                # Deduplicate identical font binaries
                target_file.unlink(missing_ok=True)
                continue

            seen_hashes.add(sha)
            names = extract_font_names_from_ttf(target_file)
            extracted.append({
                "stream_index": idx,
                "original_filename": fname,
                "saved_path": str(target_file),
                "sha256": sha,
                "size_bytes": target_file.stat().st_size,
                "names": list(names),
            })

    return extracted


def import_fonts_from_zip(zip_path: Path, output_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """
    Safely imports font files from a user-provided ZIP archive with path traversal protection
    and SHA-256 deduplication.
    """
    zip_path = Path(zip_path).resolve()
    target_dir = output_dir or MANAGED_FONTS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    imported: List[Dict[str, Any]] = []
    seen_hashes: Set[str] = set()

    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            if member.is_dir():
                continue

            name = Path(member.filename).name
            ext = Path(name).suffix.lower()
            if ext not in (".ttf", ".otf", ".ttc"):
                continue

            # Path traversal / Zip Slip protection
            norm = os.path.normpath(member.filename)
            if norm.startswith("..") or os.path.isabs(norm):
                continue

            data = zf.read(member)
            sha = hashlib.sha256(data).hexdigest()
            if sha in seen_hashes:
                continue

            dest_path = _store_font_bytes(target_dir, name, data)
            seen_hashes.add(sha)

            names = extract_font_names_from_ttf(dest_path)
            imported.append({
                "filename": dest_path.name,
                "path": str(dest_path),
                "sha256": sha,
                "names": list(names),
            })

    return imported


def parse_ass_fonts(ass_path: Path) -> Tuple[List[str], Dict[str, Dict[str, bool]]]:
    """
    Parses an .ass file to extract all unique font family names from:
    1. [V4+ Styles] / [V4 Styles] Format & Style lines
    2. [Events] Dialogue override tags \fn
    """
    ass_path = Path(ass_path)
    if not ass_path.exists():
        raise FileNotFoundError(f"ملف الترجمة ASS غير موجود: {ass_path}")

    content = ""
    for enc in ("utf-8-sig", "utf-8", "windows-1256", "cp1252", "latin-1"):
        try:
            with open(ass_path, "r", encoding=enc) as f:
                content = f.read()
            break
        except Exception:
            continue

    if not content:
        raise ValueError("تعذر قراءة ملف الترجمة ASS بأي ترميز مدعوم.")

    current_section = ""
    style_format_cols: List[str] = []
    events_format_cols: List[str] = []

    style_fonts: Set[str] = set()
    override_fonts: Set[str] = set()

    for line in content.splitlines():
        line_clean = line.strip()
        if not line_clean or line_clean.startswith(";"):
            continue

        if line_clean.startswith("[") and line_clean.endswith("]"):
            current_section = line_clean.lower()
            continue

        if current_section in ("[v4+ styles]", "[v4 styles]"):
            if line_clean.lower().startswith("format:"):
                raw_cols = line_clean.split(":", 1)[1]
                style_format_cols = [c.strip().lower() for c in raw_cols.split(",")]
            elif line_clean.lower().startswith("style:"):
                raw_vals = line_clean.split(":", 1)[1]
                cols = [c.strip() for c in raw_vals.split(",")]
                if "fontname" in style_format_cols:
                    idx = style_format_cols.index("fontname")
                    if idx < len(cols):
                        font_name = cols[idx].strip().lstrip("@")
                        if font_name:
                            style_fonts.add(font_name)

        elif current_section == "[events]":
            if line_clean.lower().startswith("format:"):
                raw_cols = line_clean.split(":", 1)[1]
                events_format_cols = [c.strip().lower() for c in raw_cols.split(",")]
            elif line_clean.lower().startswith("dialogue:"):
                raw_vals = line_clean.split(":", 1)[1]
                text_content = ""
                if "text" in events_format_cols:
                    text_idx = events_format_cols.index("text")
                    parts = raw_vals.split(",", len(events_format_cols) - 1)
                    if text_idx < len(parts):
                        text_content = parts[text_idx]
                else:
                    text_content = raw_vals

                matches = re.findall(r"\\fn(?:\"([^\"]+)\"|([^\\\}]+))", text_content)
                for m1, m2 in matches:
                    fn = (m1 or m2).strip().lstrip("@")
                    if fn:
                        override_fonts.add(fn)

    all_fonts_map: Dict[str, str] = {}
    for fn in style_fonts:
        all_fonts_map[fn.lower()] = fn
    for fn in override_fonts:
        if fn.lower() not in all_fonts_map:
            all_fonts_map[fn.lower()] = fn

    metadata: Dict[str, Dict[str, bool]] = {}
    for lower_fn, original_fn in all_fonts_map.items():
        metadata[original_fn] = {
            "in_styles": any(s.lower() == lower_fn for s in style_fonts),
            "in_overrides": any(o.lower() == lower_fn for o in override_fonts),
        }

    return list(all_fonts_map.values()), metadata


def analyze_ass_fonts(
    ass_path: Path,
    mkv_path: Optional[Path] = None,
    season_fonts_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Performs complete audit of fonts in an ASS file against:
    1. Configured external font library
    2. Studio-managed, season, and MKV fonts
    3. Windows system fonts as fallback

    Returns:
      total_fonts, available_count, missing_count, missing_font_names, and itemized breakdown.
    """
    required_fonts, metadata = parse_ass_fonts(ass_path)
    font_library = get_font_library_attached_fonts()
    system_fonts = get_windows_installed_fonts()
    studio_fonts = get_studio_attached_fonts()
    season_fonts = get_season_folder_fonts(season_fonts_dir)

    # MKV font attachments
    mkv_fonts_map: Dict[str, Dict[str, Any]] = {}
    if mkv_path and Path(mkv_path).exists() and Path(mkv_path).suffix.lower() == ".mkv":
        mkv_extracted = extract_mkv_font_attachments(Path(mkv_path))
        for item in mkv_extracted:
            for name in item["names"]:
                mkv_fonts_map[name.lower()] = item
                base = re.sub(r"\s+(?:Regular|Bold|Italic|Medium|Light|SemiBold|Black|Thin)\b", "", name, flags=re.IGNORECASE).strip()
                if base.lower() not in mkv_fonts_map:
                    mkv_fonts_map[base.lower()] = item

    results: List[Dict[str, Any]] = []
    missing_count = 0
    available_count = 0

    for font_name in required_fonts:
        lower = font_name.lower()
        loose_base = re.sub(r"\s+(?:Regular|Bold|Italic|Medium|Light|SemiBold|Black|Thin)\b", "", font_name, flags=re.IGNORECASE).strip().lower()

        status = "missing"
        source = None
        file_path = None
        sha256 = None

        if lower in font_library or loose_base in font_library:
            matched = font_library.get(lower) or font_library.get(loose_base)
            status = "font_library"
            source = "font_library"
            file_path = matched.get("path")
            available_count += 1
        elif lower in studio_fonts or loose_base in studio_fonts:
            matched = studio_fonts.get(lower) or studio_fonts.get(loose_base)
            status = "attached"
            source = "studio"
            file_path = matched.get("path")
            available_count += 1
        elif lower in season_fonts or loose_base in season_fonts:
            matched = season_fonts.get(lower) or season_fonts.get(loose_base)
            status = "season_fonts"
            source = "season"
            file_path = matched.get("path")
            available_count += 1
        elif lower in mkv_fonts_map or loose_base in mkv_fonts_map:
            matched = mkv_fonts_map.get(lower) or mkv_fonts_map.get(loose_base)
            status = "mkv_attachment"
            source = "mkv"
            file_path = matched.get("saved_path")
            sha256 = matched.get("sha256")
            available_count += 1
        elif lower in system_fonts or loose_base in system_fonts:
            matched = system_fonts.get(lower) or system_fonts.get(loose_base)
            status = "installed"
            source = "system"
            file_path = matched.get("file")
            available_count += 1
        else:
            status = "missing"
            missing_count += 1

        if file_path and Path(file_path).exists() and not sha256:
            try:
                sha256 = compute_sha256(Path(file_path))
            except Exception:
                pass

        results.append({
            "name": font_name,
            "status": status,
            "source": source,
            "file": file_path,
            "sha256": sha256,
            "in_styles": metadata[font_name]["in_styles"],
            "in_overrides": metadata[font_name]["in_overrides"],
            "status_label": {
                "font_library": "مجلد الخطوط",
                "installed": "مثبت بالنظام",
                "attached": "مرفق بالاستوديو",
                "season_fonts": "مجلد الموسم fonts/",
                "mkv_attachment": "مستخرج من MKV",
                "missing": "مفقود",
            }.get(status, "مفقود"),
        })

    missing_font_names = [f["name"] for f in results if f["status"] == "missing"]

    return {
        "ass_file": str(ass_path),
        "total_fonts": len(required_fonts),
        "available_count": available_count,
        "missing_count": missing_count,
        "all_fonts_present": missing_count == 0,
        "missing_font_names": missing_font_names,
        "fonts": results,
    }


def package_required_fonts(
    required_fonts: List[str],
    target_fonts_dir: Path,
    mkv_path: Optional[Path] = None,
    season_fonts_dir: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """
    Copies all available required fonts into the package fonts/ directory.
    Strictly deduplicates font binaries by SHA-256 so duplicate fonts are stored once.
    Returns list of manifest font records.
    """
    target_fonts_dir = Path(target_fonts_dir).resolve()
    target_fonts_dir.mkdir(parents=True, exist_ok=True)

    font_library = get_font_library_attached_fonts()
    system_fonts = get_windows_installed_fonts()
    studio_fonts = get_studio_attached_fonts()
    season_fonts = get_season_folder_fonts(season_fonts_dir)

    mkv_fonts_map: Dict[str, Dict[str, Any]] = {}
    if mkv_path and Path(mkv_path).exists() and Path(mkv_path).suffix.lower() == ".mkv":
        mkv_extracted = extract_mkv_font_attachments(Path(mkv_path))
        for item in mkv_extracted:
            for name in item["names"]:
                mkv_fonts_map[name.lower()] = item
                base = re.sub(r"\s+(?:Regular|Bold|Italic|Medium|Light|SemiBold|Black|Thin)\b", "", name, flags=re.IGNORECASE).strip()
                if base.lower() not in mkv_fonts_map:
                    mkv_fonts_map[base.lower()] = item

    packaged: List[Dict[str, Any]] = []
    seen_hashes: Set[str] = set()

    for font_name in required_fonts:
        lower = font_name.lower()
        loose_base = re.sub(r"\s+(?:Regular|Bold|Italic|Medium|Light|SemiBold|Black|Thin)\b", "", font_name, flags=re.IGNORECASE).strip().lower()

        src_file = None
        source_label = None

        if lower in font_library or loose_base in font_library:
            matched = font_library.get(lower) or font_library.get(loose_base)
            src_file = Path(matched["path"])
            source_label = "font_library"
        elif lower in studio_fonts or loose_base in studio_fonts:
            matched = studio_fonts.get(lower) or studio_fonts.get(loose_base)
            src_file = Path(matched["path"])
            source_label = "studio"
        elif lower in season_fonts or loose_base in season_fonts:
            matched = season_fonts.get(lower) or season_fonts.get(loose_base)
            src_file = Path(matched["path"])
            source_label = "season"
        elif lower in mkv_fonts_map or loose_base in mkv_fonts_map:
            matched = mkv_fonts_map.get(lower) or mkv_fonts_map.get(loose_base)
            src_file = Path(matched["saved_path"])
            source_label = "mkv"
        elif lower in system_fonts or loose_base in system_fonts:
            matched = system_fonts.get(lower) or system_fonts.get(loose_base)
            if matched.get("file"):
                src_file = Path(matched["file"])
                source_label = "system"

        if src_file and src_file.exists():
            sha = compute_sha256(src_file)
            if sha in seen_hashes:
                continue

            seen_hashes.add(sha)
            dest_file = _store_font_bytes(target_fonts_dir, src_file.name, src_file.read_bytes())
            clean_filename = dest_file.name

            packaged.append({
                "family_name": font_name,
                "filename": clean_filename,
                "path": f"fonts/{clean_filename}",
                "size_bytes": dest_file.stat().st_size,
                "sha256": sha,
                "source": source_label,
            })

    return packaged



def attach_font_file(filename: str, content_bytes: bytes) -> Dict[str, Any]:
    """Saves an uploaded font into the persistent Studio-managed font directory."""
    target_path = _store_font_bytes(MANAGED_FONTS_DIR, filename, content_bytes)
    clean_name = target_path.name

    extracted_names = extract_font_names_from_ttf(target_path)
    return {
        "success": True,
        "filename": clean_name,
        "saved_path": str(target_path),
        "sha256": compute_sha256(target_path),
        "extracted_font_names": list(extracted_names),
    }


def list_attached_fonts() -> List[Dict[str, Any]]:
    """Lists bundled and persistent Studio-managed fonts without deleting duplicates."""
    fonts = []
    seen: Dict[str, str] = {}
    for font_dir in dict.fromkeys((FONTS_DIR, MANAGED_FONTS_DIR)):
        if not font_dir.exists():
            continue
        for f in font_dir.glob("*.*"):
            if f.suffix.lower() in (".ttf", ".otf", ".ttc"):
                sha = compute_sha256(f)
                names = extract_font_names_from_ttf(f)
                fonts.append({
                    "filename": f.name,
                    "path": str(f),
                    "size_bytes": f.stat().st_size,
                    "sha256": sha,
                    "font_names": list(names),
                    "source": "studio_managed" if font_dir == MANAGED_FONTS_DIR else "studio_fonts",
                    "duplicate_of": seen.get(sha),
                })
                seen.setdefault(sha, str(f))
    return fonts
