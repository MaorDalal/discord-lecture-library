# -*- coding: utf-8 -*-
"""Type Ctrl+V then Enter into the Discord window, so a batch of files can be
queued without a human sitting there for each one.

Everything here is deliberately paranoid, because the failure mode is posting a
video into the wrong lecture:

  * the post is confirmed OPEN by reading the Discord window title, which
    carries the post name in quotes ('"Tutorial 13" | Library - Discord')
  * focus is re-checked immediately before the paste AND before Enter
  * anything unexpected aborts the batch instead of guessing

Nothing here talks to Discord's API or automates an account: it drives the
user's own client with the same two keystrokes they would type by hand.
"""
import ctypes
import ctypes.wintypes as w
import re
import time

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

VK_CONTROL = 0x11
VK_V = 0x56
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B
VK_MENU = 0x12                 # Alt

ASFW_ANY = 0xFFFFFFFF

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
SW_RESTORE = 9
WH_KEYBOARD_LL = 13
WM_KEYUP = 0x0101

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 \
    else ctypes.c_ulong


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", w.LONG), ("dy", w.LONG), ("mouseData", w.DWORD),
                ("dwFlags", w.DWORD), ("time", w.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", w.DWORD), ("wParamL", w.WORD), ("wParamH", w.WORD)]


class _IUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", w.DWORD), ("u", _IUNION)]


user32.SendInput.argtypes = [w.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = w.UINT
user32.GetForegroundWindow.restype = w.HWND
user32.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
user32.GetWindowThreadProcessId.restype = w.DWORD
user32.SetForegroundWindow.argtypes = [w.HWND]
user32.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
user32.IsIconic.argtypes = [w.HWND]
user32.AttachThreadInput.argtypes = [w.DWORD, w.DWORD, w.BOOL]
user32.SwitchToThisWindow.argtypes = [w.HWND, w.BOOL]
user32.AllowSetForegroundWindow.argtypes = [w.DWORD]
user32.BringWindowToTop.argtypes = [w.HWND]
kernel32.GetCurrentThreadId.restype = w.DWORD
kernel32.OpenProcess.restype = w.HANDLE
kernel32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
kernel32.CloseHandle.argtypes = [w.HANDLE]
kernel32.QueryFullProcessImageNameW.argtypes = [
    w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]

TARGET = "discord.exe"


# ------------------------------------------------------------- inspection
def _exe_of(hwnd):
    pid = w.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    h = kernel32.OpenProcess(0x1000, False, pid.value)   # QUERY_LIMITED_INFO
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


def title_of(hwnd):
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def foreground():
    """(hwnd, exe, title) of the focused window."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None, "", ""
    return hwnd, _exe_of(hwnd), title_of(hwnd)


def discord_window():
    """The main Discord window: its title ends with '- Discord'."""
    found = []

    def cb(hwnd, _lp):
        if user32.IsWindowVisible(hwnd):
            t = title_of(hwnd)
            if t.endswith("- Discord") and _exe_of(hwnd) == TARGET:
                found.append(hwnd)
        return True

    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, w.HWND, w.LPARAM)(cb)
    user32.EnumWindows(proc, 0)
    return found[0] if found else None


def _alt_tap():
    """Windows only lets the process that owns the most recent input set the
    foreground window. Injecting a bare Alt makes this process that owner. It
    is the standard workaround, and Alt on its own does nothing in Discord."""
    _key(VK_MENU)
    time.sleep(0.02)
    _key(VK_MENU, up=True)
    time.sleep(0.02)


def focus(hwnd):
    """Bring a window to the front, the hard way.

    A background service calling SetForegroundWindow is silently ignored (the
    taskbar button just flashes), which is exactly what a panel running behind
    Discord is. Three escalating tricks, each verified rather than assumed:
    share the foreground thread's input queue, claim the most-recent-input
    right with an Alt tap, and finally SwitchToThisWindow - what Alt+Tab uses.
    """
    if not hwnd:
        return False
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    if user32.GetForegroundWindow() == hwnd:
        return True

    try:
        user32.AllowSetForegroundWindow(ASFW_ANY)
    except Exception:
        pass

    for attempt in range(3):
        fg = user32.GetForegroundWindow()
        mine = kernel32.GetCurrentThreadId()
        theirs = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        attached = bool(theirs) and bool(
            user32.AttachThreadInput(mine, theirs, True))
        try:
            if attempt >= 1:
                _alt_tap()
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            if attempt >= 2:
                user32.SwitchToThisWindow(hwnd, True)
        finally:
            if attached:
                user32.AttachThreadInput(mine, theirs, False)
        time.sleep(0.15)
        if user32.GetForegroundWindow() == hwnd:
            return True
    return False


# ------------------------------------------------------------ title match
_QUOTED = re.compile(r'[""“”"]([^""“”"]+)[""“”"]')


def _norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip().strip("…...").strip()


def post_name(title):
    """The post name Discord shows in quotes, or '' for a normal channel."""
    m = _QUOTED.search(title or "")
    return _norm(m.group(1)) if m else ""


def title_matches(title, expected):
    """Tolerant match: Discord truncates long post names in the title bar."""
    got, want = post_name(title), _norm(expected)
    if not got or not want:
        return False
    if got == want:
        return True
    short, long_ = sorted((got, want), key=len)
    return len(short) >= 4 and long_.startswith(short)


def on_post(expected):
    """Is Discord focused right now, showing `expected`?"""
    _hwnd, exe, title = foreground()
    return exe == TARGET and title_matches(title, expected),         "%s | %s" % (exe, title)


def wait_for_post(expected, timeout=12.0, hwnd=None, settle=1.2):
    """Block until Discord is focused on `expected` AND stays there.

    The `settle` window is not padding. Launching discord:// runs a tiny
    protocol stub; when that stub exits - typically 3-4 s later - Windows
    hands the foreground back to whatever the user last typed in, yanking it
    out from under a paste that has already happened. So we require the match
    to hold steady before trusting it, and keep pulling Discord forward.

    Returns (ok, what_we_saw).
    """
    deadline = time.time() + timeout
    seen = ""
    last_focus = 0.0
    stable_since = None
    while time.time() < deadline:
        ok, seen = on_post(expected)
        if ok:
            if stable_since is None:
                stable_since = time.time()
            elif time.time() - stable_since >= settle:
                return True, seen
        else:
            stable_since = None
            if hwnd and time.time() - last_focus > 1.0:
                focus(hwnd)
                last_focus = time.time()
        time.sleep(0.15)
    return False, seen


def regain(expected, hwnd, timeout=6.0):
    """Get back to `expected` after something stole the foreground.

    Used between the paste and Enter: the attachment is already staged in
    Discord's composer, so recovering focus is enough - nothing is lost, and
    the alternative (giving up) would strand the file there.
    """
    deadline = time.time() + timeout
    seen = ""
    while time.time() < deadline:
        ok, seen = on_post(expected)
        if ok:
            return True, seen
        focus(hwnd)
        time.sleep(0.25)
    return False, seen


# ------------------------------------------------------------- keystrokes
def _key(vk, up=False):
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki = KEYBDINPUT(wVk=vk, wScan=0,
                        dwFlags=KEYEVENTF_KEYUP if up else 0,
                        time=0, dwExtraInfo=0)
    n = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    if n != 1:
        raise OSError("SendInput failed (%d)" % ctypes.get_last_error())


def paste():
    """Ctrl+V."""
    _key(VK_CONTROL)
    time.sleep(0.03)
    _key(VK_V)
    time.sleep(0.03)
    _key(VK_V, up=True)
    time.sleep(0.03)
    _key(VK_CONTROL, up=True)


def enter():
    _key(VK_RETURN)
    time.sleep(0.03)
    _key(VK_RETURN, up=True)


# ------------------------------------------------------------- panic key
class EscapeWatcher:
    """Esc anywhere aborts the batch - the panel window is behind Discord
    while this runs, so a button alone is not a usable stop."""

    def __init__(self, on_escape):
        self.on_escape = on_escape
        self.available = False
        self.error = ""
        self._hook = None
        self._proc = None
        self._thread = None
        self._armed = False

    def _callback(self, nCode, wParam, lParam):
        try:
            if nCode == 0 and self._armed and wParam == WM_KEYUP:
                kb = ctypes.cast(lParam, ctypes.POINTER(_KB)).contents
                if kb.vkCode == VK_ESCAPE:
                    self._armed = False
                    try:
                        self.on_escape()
                    except Exception:
                        pass
        except Exception:
            pass
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    def _run(self):
        self._proc = _HOOKPROC(self._callback)
        self._hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc,
                                              None, 0)
        if not self._hook:
            self.error = "SetWindowsHookExW failed (%d)" % \
                ctypes.get_last_error()
            return
        self.available = True
        msg = w.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def arm(self):
        if not (self._thread and self._thread.is_alive()):
            import threading
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            time.sleep(0.3)
        self._armed = True
        return self.available

    def disarm(self):
        self._armed = False


class _KB(ctypes.Structure):
    _fields_ = [("vkCode", w.DWORD), ("scanCode", w.DWORD),
                ("flags", w.DWORD), ("time", w.DWORD),
                ("dwExtraInfo", ctypes.POINTER(w.ULONG))]


_HOOKPROC = ctypes.WINFUNCTYPE(w.LPARAM, ctypes.c_int, w.WPARAM, w.LPARAM)
user32.SetWindowsHookExW.restype = w.HHOOK
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, _HOOKPROC, w.HINSTANCE,
                                     w.DWORD]
user32.CallNextHookEx.restype = w.LPARAM
user32.CallNextHookEx.argtypes = [w.HHOOK, ctypes.c_int, w.WPARAM, w.LPARAM]
user32.GetMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND,
                               ctypes.c_uint, ctypes.c_uint]


# ------------------------------------------------------------ mouse focus
# Regaining the WINDOW is not the same as regaining the COMPOSER. After the
# foreground bounces (see wait_for_post), Discord can hold focus on the post
# body, where Enter does nothing at all - the file just sits there staged.
# A click in the message box is the one way to be sure the keystroke lands.
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77


class POINT(ctypes.Structure):
    _fields_ = [("x", w.LONG), ("y", w.LONG)]


user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]


def _mouse(flags, x=0, y=0):
    inp = INPUT(type=0)                      # INPUT_MOUSE
    inp.mi = MOUSEINPUT(dx=x, dy=y, mouseData=0, dwFlags=flags, time=0,
                        dwExtraInfo=0)
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def click_at(x, y, restore=True):
    """Left-click one screen point, then put the pointer back."""
    old = POINT()
    user32.GetCursorPos(ctypes.byref(old))
    vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    vw = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN) or 1
    vh = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN) or 1
    nx = int((x - vx) * 65535 / vw)
    ny = int((y - vy) * 65535 / vh)
    _mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, nx, ny)
    time.sleep(0.05)
    _mouse(MOUSEEVENTF_LEFTDOWN | MOUSEEVENTF_ABSOLUTE, nx, ny)
    time.sleep(0.04)
    _mouse(MOUSEEVENTF_LEFTUP | MOUSEEVENTF_ABSOLUTE, nx, ny)
    time.sleep(0.08)
    if restore:
        user32.SetCursorPos(old.x, old.y)


def click_composer(hwnd):
    """Put the caret in Discord's message box: bottom strip, middle column."""
    r = w.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return False
    width = r.right - r.left
    if width < 300:
        return False
    click_at(r.left + int(width * 0.50), r.bottom - 38)
    return True
