"""Windows DPAPI: encrypt a value so only this Windows user on this PC can read it back.

Provider keys kept by OmniBots (when Omni isn't installed, PLAN.md A1.s.01) are stored in
SQLite as DPAPI blobs, never as text. Copying the database to another PC or another account
gives ciphertext that can't be opened there. No extra package: CryptProtectData via ctypes.
"""

from __future__ import annotations

import ctypes
import sys

# Ties the blobs to OmniBots: another program calling DPAPI as the same user needs this too.
ENTROPY = b"omnibots/provider-key/v1"
CRYPTPROTECT_UI_FORBIDDEN = 0x1


class DpapiError(RuntimeError):
    pass


def available() -> bool:
    return sys.platform == "win32"


if sys.platform == "win32":
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p

    def _in(data: bytes) -> tuple[_Blob, ctypes.Array]:
        buf = ctypes.create_string_buffer(data, len(data))
        return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    def _out(blob: _Blob) -> bytes:
        try:
            return ctypes.string_at(blob.pbData, blob.cbData)
        finally:
            _kernel32.LocalFree(ctypes.cast(blob.pbData, ctypes.c_void_p))


def protect(plain: str) -> bytes:
    if not available():
        raise DpapiError("encrypted key storage needs Windows (DPAPI)")
    data, _keep = _in(plain.encode("utf-8"))
    ent, _keep2 = _in(ENTROPY)
    out = _Blob()
    if not _crypt32.CryptProtectData(ctypes.byref(data), None, ctypes.byref(ent), None, None,
                                     CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise DpapiError(f"CryptProtectData failed (error {ctypes.GetLastError()})")
    return _out(out)


def unprotect(blob: bytes) -> str:
    if not available():
        raise DpapiError("encrypted key storage needs Windows (DPAPI)")
    data, _keep = _in(bytes(blob))
    ent, _keep2 = _in(ENTROPY)
    out = _Blob()
    if not _crypt32.CryptUnprotectData(ctypes.byref(data), None, ctypes.byref(ent), None, None,
                                       CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise DpapiError("this key was saved by another Windows user or PC and can't be read here")
    return _out(out).decode("utf-8")
