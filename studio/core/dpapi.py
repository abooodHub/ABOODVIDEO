"""Windows Data Protection API (DPAPI) helper for ABOODVIDEO Studio.

Secures sensitive credentials (such as server API tokens) using Windows-native
per-user encryption (CurrentUser scope via CryptProtectData / CryptUnprotectData).
The encrypted payload is bound to the current Windows user credentials and cannot
be decrypted by any other user account or on a different machine.
"""
import os
import sys
import ctypes
from ctypes import wintypes
from typing import Optional

# Windows DPAPI Constants
CRYPTPROTECT_UI_FORBIDDEN = 0x01


class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def dpapi_protect(data: bytes, description: str = "ABOODVIDEO Studio Secret") -> bytes:
    """Encrypts bytes using Windows DPAPI bound to CurrentUser."""
    if not data:
        return b""

    if os.name != "nt":
        # Safe fallback for non-Windows test environments
        import base64
        return b"NON_WIN_FALLBACK:" + base64.b64encode(data)

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    in_blob = DATA_BLOB(
        len(data),
        ctypes.cast(ctypes.create_string_buffer(data), ctypes.POINTER(ctypes.c_byte))
    )
    out_blob = DATA_BLOB()

    # dwFlags = CRYPTPROTECT_UI_FORBIDDEN (0x01) -> CurrentUser scope, no UI prompt
    success = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        description,
        None,  # Optional entropy
        None,  # Reserved
        None,  # Prompt struct
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not success:
        error_code = kernel32.GetLastError()
        raise ctypes.WinError(error_code)

    try:
        encrypted = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return encrypted
    finally:
        kernel32.LocalFree(out_blob.pbData)


def dpapi_unprotect(data: bytes) -> bytes:
    """Decrypts bytes previously encrypted with Windows DPAPI CurrentUser scope."""
    if not data:
        return b""

    if os.name != "nt":
        if data.startswith(b"NON_WIN_FALLBACK:"):
            import base64
            return base64.b64decode(data[len(b"NON_WIN_FALLBACK:"):])
        return data

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    in_blob = DATA_BLOB(
        len(data),
        ctypes.cast(ctypes.create_string_buffer(data), ctypes.POINTER(ctypes.c_byte))
    )
    out_blob = DATA_BLOB()

    success = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,  # Description out
        None,  # Optional entropy
        None,  # Reserved
        None,  # Prompt struct
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not success:
        error_code = kernel32.GetLastError()
        raise ctypes.WinError(error_code)

    try:
        decrypted = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return decrypted
    finally:
        kernel32.LocalFree(out_blob.pbData)


def mask_secret(secret: Optional[str]) -> str:
    """Safely masks a secret string for diagnostics or logging."""
    if not secret:
        return ""
    s = secret.strip()
    if len(s) <= 8:
        return "********"
    return f"{s[:6]}...{s[-4:]}"
