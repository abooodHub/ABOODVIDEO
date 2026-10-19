# -*- mode: python ; coding: utf-8 -*-
import os
import sys
import shutil
from pathlib import Path

block_cipher = None

PROJECT_ROOT = Path(SPECPATH).resolve()

# Bundle FFmpeg/FFprobe found on PATH, or in the explicitly configured folder.
ffmpeg_bin_dir = os.environ.get("ABOODVIDEO_FFMPEG_BIN_DIR")
if ffmpeg_bin_dir:
    ffmpeg_root = Path(ffmpeg_bin_dir).expanduser().resolve()
else:
    ffmpeg_on_path = shutil.which("ffmpeg")
    ffmpeg_root = Path(ffmpeg_on_path).resolve().parent if ffmpeg_on_path else Path()

binaries = []
for binary_name in ("ffmpeg.exe", "ffprobe.exe"):
    binary_path = ffmpeg_root / binary_name
    if not binary_path.is_file():
        raise RuntimeError(
            f"Missing {binary_name}; put FFmpeg tools on PATH or set ABOODVIDEO_FFMPEG_BIN_DIR."
        )
    binaries.append((str(binary_path), "bin"))

# Bundle only assets used by the active v3 Studio page. The prior page remains
# in source for legacy regression tests, but is not part of the shipped client.
datas = [
    (str(PROJECT_ROOT / "shared" / "i18n" / "en.json"), "shared/i18n"),
    (str(PROJECT_ROOT / "shared" / "i18n" / "web"), "shared/i18n/web"),
    (str(PROJECT_ROOT / "studio" / "web" / "static" / "css" / "studio_desktop.css"), "studio/web/static/css"),
    (str(PROJECT_ROOT / "studio" / "web" / "static" / "css" / "studio_import.css"), "studio/web/static/css"),
    (str(PROJECT_ROOT / "studio" / "web" / "static" / "css" / "studio_platform.css"), "studio/web/static/css"),
    (str(PROJECT_ROOT / "studio" / "web" / "static" / "css" / "studio_v3.css"), "studio/web/static/css"),
    (str(PROJECT_ROOT / "studio" / "web" / "static" / "js" / "studio_v3.js"), "studio/web/static/js"),
    (str(PROJECT_ROOT / "studio" / "web" / "templates" / "index_v3.html"), "studio/web/templates"),
    (str(PROJECT_ROOT / "app.ico"), "."),
]

# Preserve the notices distributed with this exact FFmpeg build. Do not bundle
# executables copied out of a distribution without their license/source details.
for notice, target in [("LICENSE", "LICENSE"), ("README.txt", "README.txt")]:
    notice_path = ffmpeg_root.parent / notice
    if not notice_path.is_file():
        raise RuntimeError(f"Missing FFmpeg distribution notice: {notice_path}")
    datas.append((str(notice_path), "licenses/ffmpeg"))
datas += [(str(PROJECT_ROOT / "LICENSE"), "licenses/project"),
          (str(PROJECT_ROOT / "THIRD_PARTY_NOTICES.md"), "licenses/project")]

hidden_imports = [
    "uvicorn",
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "fastapi",
    "fastapi.staticfiles",
    "fastapi.templating",
    "starlette",
    "starlette.staticfiles",
    "starlette.templating",
    "jinja2",
    "websockets",
    "clr_loader",
    "pythonnet",
    "webview",
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
    "sqlite3",
    "psutil",
    "pydantic",
    "pydantic_settings",
    "multipart",
    "python_multipart",
    "email.mime.multipart",
    "email.mime.text",
    "email.mime.base",
    "studio.core.dpapi",
    "studio.core.process",
    "studio.core.picker",
]

a = Analysis(
    ["studio/main.py"],
    pathex=[str(PROJECT_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "scipy", "notebook", "IPython"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ABOODVIDEO Studio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # No command prompt console window!
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=str(PROJECT_ROOT / "studio_version_info.txt"),
    icon=str(PROJECT_ROOT / "studio_platform.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ABOODVIDEO-Studio",
)
