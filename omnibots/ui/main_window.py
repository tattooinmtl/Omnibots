"""Windows focus helper. (This file held the A0 placeholder main window; A11's windows replaced
it and it was removed in A15.a.02. `_force_foreground` stays: bot_window.py uses it.)"""

from __future__ import annotations


def _force_foreground(hwnd: int) -> bool:
    """Bring hwnd to the front despite Windows' focus-stealing rules.

    Windows refuses SetForegroundWindow from a background process while the
    user is typing elsewhere. Briefly attaching our input thread to the
    foreground window's thread is the standard, documented workaround. If
    Windows still refuses, flash the taskbar button so the user notices.
    """
    import ctypes
    from ctypes import wintypes

    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    if user32.SetForegroundWindow(hwnd):
        return True
    fg = user32.GetForegroundWindow()
    fg_thread = user32.GetWindowThreadProcessId(fg, None)
    our_thread = kernel32.GetCurrentThreadId()
    attached = fg_thread and fg_thread != our_thread and user32.AttachThreadInput(our_thread, fg_thread, True)
    try:
        user32.BringWindowToTop(hwnd)
        ok = bool(user32.SetForegroundWindow(hwnd))
    finally:
        if attached:
            user32.AttachThreadInput(our_thread, fg_thread, False)
    if not ok:
        class FLASHWINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND), ("dwFlags", wintypes.DWORD),
                        ("uCount", wintypes.UINT), ("dwTimeout", wintypes.DWORD)]
        FLASHW_ALL, FLASHW_TIMERNOFG = 0x3, 0xC
        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, FLASHW_ALL | FLASHW_TIMERNOFG, 0, 0)
        user32.FlashWindowEx(ctypes.byref(info))
    return ok
