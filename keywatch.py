# -*- coding: utf-8 -*-
"""Fire a callback when Enter is pressed *inside the Discord window*.

Why this exists: Discord's API only shows a message once its upload has
finished, so waiting for the API means waiting out a 400 MB transfer before
moving to the next file. The keystroke is the only signal available at the
moment the user actually sends.

Scope, deliberately narrow:
  * only the Enter key is ever acted on
  * only while the foreground window belongs to Discord.exe
  * only while explicitly armed by the panel
  * the key is always passed through untouched; nothing is blocked,
    recorded, or stored
"""
import ctypes
import ctypes.wintypes as w
import threading
import time

WH_KEYBOARD_LL = 13
WM_KEYUP = 0x0101
WM_SYSKEYUP = 0x0105
VK_RETURN = 0x0D

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", w.DWORD), ("scanCode", w.DWORD),
                ("flags", w.DWORD), ("time", w.DWORD),
                ("dwExtraInfo", ctypes.POINTER(w.ULONG))]


HOOKPROC = ctypes.WINFUNCTYPE(w.LPARAM, ctypes.c_int, w.WPARAM, w.LPARAM)

# ctypes defaults every return value to C int. On 64-bit that truncates every
# HANDLE/HHOOK to 32 bits, so SetWindowsHookExW fails with 126. The signatures
# below are not optional.
user32.SetWindowsHookExW.restype = w.HHOOK
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, w.HINSTANCE, w.DWORD]
user32.CallNextHookEx.restype = w.LPARAM
user32.CallNextHookEx.argtypes = [w.HHOOK, ctypes.c_int, w.WPARAM, w.LPARAM]
user32.UnhookWindowsHookEx.argtypes = [w.HHOOK]
user32.GetForegroundWindow.restype = w.HWND
user32.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
user32.GetMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND,
                               ctypes.c_uint, ctypes.c_uint]
kernel32.GetModuleHandleW.restype = w.HMODULE
kernel32.GetModuleHandleW.argtypes = [w.LPCWSTR]
kernel32.OpenProcess.restype = w.HANDLE
kernel32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
kernel32.CloseHandle.argtypes = [w.HANDLE]
kernel32.QueryFullProcessImageNameW.argtypes = [
    w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]


def _foreground_exe():
    """Executable name of the focused window, lowercased."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ""
    pid = w.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = w.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1].lower()
    finally:
        kernel32.CloseHandle(h)
    return ""


class EnterWatcher:
    def __init__(self, on_enter, target="discord.exe", debounce=1.2):
        self.on_enter = on_enter
        self.target = target
        self.debounce = debounce
        self.armed = False
        self._last = 0.0
        self._thread = None
        self._hook = None
        self._proc = None
        self.available = True
        self.error = ""

    def _callback(self, nCode, wParam, lParam):
        try:
            if (nCode == 0 and self.armed
                    and wParam in (WM_KEYUP, WM_SYSKEYUP)):
                kb = ctypes.cast(lParam,
                                 ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if kb.vkCode == VK_RETURN:
                    now = time.time()
                    if now - self._last >= self.debounce:
                        if _foreground_exe() == self.target:
                            self._last = now
                            self.armed = False
                            try:
                                self.on_enter()
                            except Exception:
                                pass
        except Exception:
            pass
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    def _run(self):
        self._proc = HOOKPROC(self._callback)
        self._hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._proc, None, 0)
        if not self._hook:
            self.available = False
            self.error = "SetWindowsHookExW failed (%d)" % ctypes.get_last_error()
            return
        msg = w.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def start(self):
        if self._thread and self._thread.is_alive():
            return self.available
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        time.sleep(0.35)          # let the hook install before reporting
        return self.available

    def arm(self):
        self.start()
        self._last = 0.0
        self.armed = True

    def disarm(self):
        self.armed = False
