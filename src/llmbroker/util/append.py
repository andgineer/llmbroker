"""Appending to a file that several writers share without one overwriting another."""

import os
import sys
from pathlib import Path

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _FILE_APPEND_DATA = 0x0004
    _SYNCHRONIZE = 0x00100000
    _FILE_SHARE_ALL = 0x1 | 0x2 | 0x4
    _OPEN_ALWAYS = 4
    _FILE_ATTRIBUTE_NORMAL = 0x80
    _INVALID_HANDLE = wintypes.HANDLE(-1).value

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _kernel32.WriteFile.restype = wintypes.BOOL
    _kernel32.WriteFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    def append_bytes(path: Path, data: bytes) -> None:
        """Append ``data`` at the end of ``path``, creating it, as one indivisible write."""
        # The C runtime's O_APPEND seeks to the end and then writes, so two writers can
        # land on the same offset; a handle holding append rights alone makes the OS do it.
        handle = _kernel32.CreateFileW(
            str(path),
            _FILE_APPEND_DATA | _SYNCHRONIZE,
            _FILE_SHARE_ALL,
            None,
            _OPEN_ALWAYS,
            _FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if handle is None or handle == _INVALID_HANDLE:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            written = wintypes.DWORD()
            if not _kernel32.WriteFile(handle, data, len(data), ctypes.byref(written), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if written.value != len(data):
                raise OSError(f"short append to {path}: {written.value} of {len(data)} bytes")
        finally:
            _kernel32.CloseHandle(handle)

else:

    def append_bytes(path: Path, data: bytes) -> None:
        """Append ``data`` at the end of ``path``, creating it, as one indivisible write."""
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o666)
        try:
            written = os.write(fd, data)
        finally:
            os.close(fd)
        if written != len(data):
            raise OSError(f"short append to {path}: {written} of {len(data)} bytes")
