"""Native Windows File and Folder Pickers for ABOODVIDEO Studio.

Uses Windows native COM IFileOpenDialog with WinForms / comdlg32 fallbacks.
Runs in a dedicated STA thread. No PowerShell, no cmd.exe, no console windows.
"""
import os
import sys
import threading
from pathlib import Path
from typing import Optional, List, Tuple
from shared.i18n import gettext

VIDEO_EXTENSIONS = [".mkv", ".mp4", ".mov", ".ts", ".webm", ".flv", ".avi"]
SUBTITLE_EXTENSIONS = [".ass", ".srt", ".vtt"]


def _run_in_sta_thread(worker_fn):
    """Executes a GUI dialog callable on a dedicated thread."""
    res_box = []
    err_box = []

    def _sta_wrapper():
        try:
            val = worker_fn()
            res_box.append(val)
        except Exception as e:
            err_box.append(e)

    t = threading.Thread(target=_sta_wrapper, daemon=True)
    t.start()
    t.join()

    if err_box:
        raise err_box[0]
    return res_box[0] if res_box else None


def _pick_com_dialog(
    title: str,
    is_folder: bool = False,
    file_types: Optional[List[Tuple[str, str]]] = None,
    initial_dir: Optional[str] = None,
) -> Optional[str]:
    """Native Windows COM IFileOpenDialog implementation."""
    import ctypes
    from ctypes import wintypes, POINTER, c_void_p, Structure, byref, c_uint, cast, WINFUNCTYPE, c_long

    ole32 = ctypes.windll.ole32
    shell32 = ctypes.windll.shell32
    user32 = ctypes.windll.user32

    # Initialize COM as Single-Threaded Apartment (STA)
    co_init = ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED = 0x2

    try:
        class GUID(Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", wintypes.BYTE * 8),
            ]

        CLSID_FileOpenDialog = GUID(0xDC1C5A9C, 0xE88A, 0x4DDE, (wintypes.BYTE * 8)(0xA5, 0xA1, 0x60, 0xF8, 0x2A, 0x20, 0xAE, 0xF7))
        IID_IFileOpenDialog = GUID(0xD57C7288, 0xD4AD, 0x4768, (wintypes.BYTE * 8)(0xBE, 0x02, 0x9D, 0x96, 0x95, 0x32, 0xD9, 0x60))
        IID_IShellItem = GUID(0x43826D1E, 0xE718, 0x42EE, (wintypes.BYTE * 8)(0xBC, 0x55, 0xA1, 0xE2, 0x61, 0xC3, 0x7B, 0xFE))

        class COMDLG_FILTERSPEC(Structure):
            _fields_ = [
                ("pszName", wintypes.LPCWSTR),
                ("pszSpec", wintypes.LPCWSTR),
            ]

        p_dialog = c_void_p()
        hr = ole32.CoCreateInstance(
            byref(CLSID_FileOpenDialog),
            None,
            1,  # CLSCTX_INPROC_SERVER
            byref(IID_IFileOpenDialog),
            byref(p_dialog),
        )
        if hr != 0:
            return None

        vtable = cast(cast(p_dialog, POINTER(c_void_p)).contents, POINTER(c_void_p))

        # VTable Function Prototypes
        Release_proto = WINFUNCTYPE(c_uint, c_void_p)
        Show_proto = WINFUNCTYPE(c_long, c_void_p, wintypes.HWND)
        SetTitle_proto = WINFUNCTYPE(c_long, c_void_p, wintypes.LPCWSTR)
        GetOptions_proto = WINFUNCTYPE(c_long, c_void_p, POINTER(wintypes.DWORD))
        SetOptions_proto = WINFUNCTYPE(c_long, c_void_p, wintypes.DWORD)
        SetFolder_proto = WINFUNCTYPE(c_long, c_void_p, c_void_p)
        SetFileTypes_proto = WINFUNCTYPE(c_long, c_void_p, c_uint, POINTER(COMDLG_FILTERSPEC))
        SetFileTypeIndex_proto = WINFUNCTYPE(c_long, c_void_p, c_uint)
        GetResult_proto = WINFUNCTYPE(c_long, c_void_p, POINTER(c_void_p))

        Release = Release_proto(vtable[2])
        Show = Show_proto(vtable[3])
        SetFileTypes = SetFileTypes_proto(vtable[4])
        SetFileTypeIndex = SetFileTypeIndex_proto(vtable[5])
        SetOptions = SetOptions_proto(vtable[9])
        GetOptions = GetOptions_proto(vtable[10])
        SetFolder = SetFolder_proto(vtable[12])
        SetTitle = SetTitle_proto(vtable[17])
        GetResult = GetResult_proto(vtable[20])

        try:
            # Set Title
            if title:
                SetTitle(p_dialog, str(title))

            # Options configuration
            FOS_PICKFOLDERS = 0x00000020
            FOS_FORCEFILESYSTEM = 0x00000040
            FOS_NOCHANGEDIR = 0x00000008
            FOS_PATHMUSTEXIST = 0x00000800
            FOS_FILEMUSTEXIST = 0x00001000

            cur_opt = wintypes.DWORD()
            GetOptions(p_dialog, byref(cur_opt))
            new_opt = cur_opt.value | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST | FOS_NOCHANGEDIR

            if is_folder:
                new_opt |= FOS_PICKFOLDERS
            else:
                new_opt |= FOS_FILEMUSTEXIST

            SetOptions(p_dialog, new_opt)

            # Set Initial Folder if available
            if initial_dir and os.path.exists(initial_dir):
                init_item = c_void_p()
                if shell32.SHCreateItemFromParsingName(str(Path(initial_dir).resolve()), None, byref(IID_IShellItem), byref(init_item)) == 0:
                    SetFolder(p_dialog, init_item)
                    init_vtable = cast(cast(init_item, POINTER(c_void_p)).contents, POINTER(c_void_p))
                    Release_proto(init_vtable[2])(init_item)

            # Set File Types filter for files
            if not is_folder and file_types:
                c_types = len(file_types)
                filters_array = (COMDLG_FILTERSPEC * c_types)()
                for idx, (name, spec) in enumerate(file_types):
                    filters_array[idx].pszName = name
                    filters_array[idx].pszSpec = spec
                SetFileTypes(p_dialog, c_types, filters_array)
                SetFileTypeIndex(p_dialog, 1)

            # Active foreground parent window
            hwnd_parent = user32.GetForegroundWindow()

            # Show Dialog
            ret = Show(p_dialog, hwnd_parent)

            # 0 is S_OK. Negative values (e.g. 0x800704C7 -> -2147023673) indicate cancellation
            if ret != 0:
                return None

            # Get Selected Item
            p_result_item = c_void_p()
            if GetResult(p_dialog, byref(p_result_item)) != 0:
                return None

            item_vtable = cast(cast(p_result_item, POINTER(c_void_p)).contents, POINTER(c_void_p))
            GetDisplayName = WINFUNCTYPE(c_long, c_void_p, wintypes.DWORD, POINTER(wintypes.LPWSTR))(item_vtable[5])
            ReleaseItem = Release_proto(item_vtable[2])

            SIGDN_FILESYSPATH = 0x80058000
            psz_path = wintypes.LPWSTR()
            hr_disp = GetDisplayName(p_result_item, SIGDN_FILESYSPATH, byref(psz_path))

            selected_path = None
            if hr_disp == 0 and psz_path.value:
                selected_path = str(Path(psz_path.value).resolve())
                ole32.CoTaskMemFree(psz_path)

            ReleaseItem(p_result_item)
            return selected_path

        finally:
            Release(p_dialog)

    finally:
        if co_init in (0, 1):  # S_OK or S_FALSE
            ole32.CoUninitialize()


def _pick_winforms_dialog(
    title: str,
    is_folder: bool = False,
    file_types: Optional[List[Tuple[str, str]]] = None,
    initial_dir: Optional[str] = None,
) -> Optional[str]:
    """Fallback WinForms OpenFileDialog / FolderBrowserDialog via pythonnet."""
    import clr
    clr.AddReference("System.Windows.Forms")
    from System.Windows.Forms import OpenFileDialog, FolderBrowserDialog, DialogResult

    if is_folder:
        dlg = FolderBrowserDialog()
        dlg.Description = title
        dlg.AutoUpgradeEnabled = True
        dlg.UseDescriptionForTitle = True
        if initial_dir and os.path.exists(initial_dir):
            dlg.SelectedPath = str(Path(initial_dir).resolve())
        res = dlg.ShowDialog()
        if res == DialogResult.OK and dlg.SelectedPath:
            return str(Path(dlg.SelectedPath).resolve())
        return None
    else:
        dlg = OpenFileDialog()
        dlg.Title = title
        dlg.Multiselect = False
        dlg.CheckFileExists = True
        dlg.CheckPathExists = True
        dlg.AutoUpgradeEnabled = True
        if initial_dir and os.path.exists(initial_dir):
            dlg.InitialDirectory = str(Path(initial_dir).resolve())
        if file_types:
            parts = []
            for name, spec in file_types:
                parts.append(f"{name}|{spec}")
            dlg.Filter = "|".join(parts)
        res = dlg.ShowDialog()
        if res == DialogResult.OK and dlg.FileName:
            return str(Path(dlg.FileName).resolve())
        return None


def select_video_file(initial_dir: Optional[str] = None, title: str = "اختر ملف فيديو للمعالجة") -> Optional[str]:
    """Prompts native Windows file picker for video selection.
    Returns absolute path or None if cancelled.
    """
    filters = [
        ("ملفات الفيديو (*.mkv; *.mp4; *.mov; *.ts; *.webm; *.flv; *.avi)", "*.mkv;*.mp4;*.mov;*.ts;*.webm;*.flv;*.avi"),
        ("Matroska Video (*.mkv)", "*.mkv"),
        ("MP4 Video (*.mp4)", "*.mp4"),
        ("QuickTime Video (*.mov)", "*.mov"),
        ("MPEG Transport Stream (*.ts)", "*.ts"),
        ("WebM Video (*.webm)", "*.webm"),
        ("كل الملفات (*.*)", "*.*"),
    ]

    title = gettext(title)
    filters = [(gettext(label), pattern) for label, pattern in filters]

    def _worker():
        try:
            return _pick_com_dialog(title=title, is_folder=False, file_types=filters, initial_dir=initial_dir)
        except Exception:
            try:
                return _pick_winforms_dialog(title=title, is_folder=False, file_types=filters, initial_dir=initial_dir)
            except Exception:
                return None

    return _run_in_sta_thread(_worker)


def select_subtitle_file(initial_dir: Optional[str] = None, title: str = "اختر ملف ترجمة") -> Optional[str]:
    """Prompts native Windows file picker for subtitle selection.
    Returns absolute path or None if cancelled.
    """
    filters = [
        ("ملفات الترجمة (*.ass; *.srt; *.vtt)", "*.ass;*.srt;*.vtt"),
        ("Advanced SubStation Alpha (*.ass)", "*.ass"),
        ("SubRip Subtitle (*.srt)", "*.srt"),
        ("WebVTT (*.vtt)", "*.vtt"),
        ("كل الملفات (*.*)", "*.*"),
    ]

    title = gettext(title)
    filters = [(gettext(label), pattern) for label, pattern in filters]

    def _worker():
        try:
            return _pick_com_dialog(title=title, is_folder=False, file_types=filters, initial_dir=initial_dir)
        except Exception:
            try:
                return _pick_winforms_dialog(title=title, is_folder=False, file_types=filters, initial_dir=initial_dir)
            except Exception:
                return None

    return _run_in_sta_thread(_worker)


def select_folder(initial_dir: Optional[str] = None, title: str = "اختر مجلد الموسم للمعالجة") -> Optional[str]:
    """Prompts native Windows folder picker for selecting a folder.
    Returns absolute path or None if cancelled.
    """
    title = gettext(title)

    def _worker():
        try:
            return _pick_com_dialog(title=title, is_folder=True, initial_dir=initial_dir)
        except Exception:
            try:
                return _pick_winforms_dialog(title=title, is_folder=True, initial_dir=initial_dir)
            except Exception:
                return None

    return _run_in_sta_thread(_worker)
