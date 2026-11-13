"""Real Windows screen and input, without application-specific automation.

Win32 contracts used here:
https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-sendinput
https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-getuserobjectinformationw
https://learn.microsoft.com/windows/win32/api/wingdi/nf-wingdi-getdibits
https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-vkkeyscanexw
https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-mapvirtualkeyexw
https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-tounicodeex
https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-setforegroundwindow
https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-enumwindows
https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-windowfrompoint

Importing the module is safe on other platforms. An explicit activation request
uses the ordinary Windows foreground policy on the current input desktop.
"""
from __future__ import annotations

import ctypes as C
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from contextlib import contextmanager

from ..errors import U2CError

# Fixed width Win32 types also make structure-layout tests useful on Linux.
DWORD = C.c_uint32
LONG = C.c_int32
WORD = C.c_uint16
UINT = C.c_uint32
BOOL = C.c_int32
HANDLE = C.c_void_p
ULONG_PTR = C.c_size_t
PW_CLIENTONLY = 0x01
PW_RENDERFULLCONTENT = 0x02

# Fixed statuses from the isolated helper; stdout contains only RGB bytes.
WINDOW_CAPTURE_ERRORS = {
    "CAPTURE_FAILED": 10,
    "PERMISSION_DENIED": 11,
    "NO_INTERACTIVE_DESKTOP": 12,
    "STALE_FRAME": 13,
    "WINDOW_UNAVAILABLE": 14,
    "OUT_OF_BOUNDS": 15,
}


class POINT(C.Structure):
    _fields_ = [("x", LONG), ("y", LONG)]


class RECT(C.Structure):
    _fields_ = [("left", LONG), ("top", LONG), ("right", LONG), ("bottom", LONG)]


class MOUSEINPUT(C.Structure):
    _fields_ = [("dx", LONG), ("dy", LONG), ("mouseData", DWORD),
                ("dwFlags", DWORD), ("time", DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(C.Structure):
    _fields_ = [("wVk", WORD), ("wScan", WORD), ("dwFlags", DWORD),
                ("time", DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(C.Structure):
    _fields_ = [("uMsg", DWORD), ("wParamL", WORD), ("wParamH", WORD)]


class INPUTUNION(C.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(C.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("type", DWORD), ("data", INPUTUNION)]


class MONITORINFOEXW(C.Structure):
    _fields_ = [("cbSize", DWORD), ("rcMonitor", RECT), ("rcWork", RECT),
                ("dwFlags", DWORD), ("szDevice", C.c_wchar * 32)]


class BITMAPINFOHEADER(C.Structure):
    _fields_ = [("biSize", DWORD), ("biWidth", LONG), ("biHeight", LONG),
                ("biPlanes", WORD), ("biBitCount", WORD), ("biCompression", DWORD),
                ("biSizeImage", DWORD), ("biXPelsPerMeter", LONG),
                ("biYPelsPerMeter", LONG), ("biClrUsed", DWORD), ("biClrImportant", DWORD)]


class BITMAPINFO(C.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", DWORD * 3)]


class SID_AND_ATTRIBUTES(C.Structure):
    _fields_ = [("Sid", HANDLE), ("Attributes", DWORD)]


class TOKEN_MANDATORY_LABEL(C.Structure):
    _fields_ = [("Label", SID_AND_ATTRIBUTES)]


MOUSE_MOVE = 0x0001
MOUSE_ABSOLUTE = 0x8000
MOUSE_VIRTUALDESK = 0x4000
MOUSE_WHEEL = 0x0800
KEY_UP = 0x0002
KEY_UNICODE = 0x0004
KEY_SCANCODE = 0x0008
KEY_EXTENDED = 0x0001
TOKEN_QUERY = 0x0008
TOKEN_INTEGRITY_LEVEL = 25
ERROR_ACCESS_DENIED = 5
INTEGRITY_LEVELS = {0x0000: "untrusted", 0x1000: "low", 0x2000: "medium",
                    0x3000: "high", 0x4000: "system", 0x5000: "protected"}
MODIFIER_VKS = (0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0x5C)
BUTTON_FLAGS = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010),
                "middle": (0x0020, 0x0040)}
KEYS = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "shift": 0x10,
    "ctrl": 0x11, "alt": 0x12, "pause": 0x13, "capslock": 0x14,
    "escape": 0x1B, "space": 0x20, "pageup": 0x21, "pagedown": 0x22,
    "end": 0x23, "home": 0x24, "left": 0x25, "up": 0x26,
    "right": 0x27, "down": 0x28, "printscreen": 0x2C,
    "insert": 0x2D, "delete": 0x2E, "win": 0x5B,
    "numlock": 0x90, "scrolllock": 0x91,
}
KEYS.update({chr(n).lower(): n for n in range(ord("A"), ord("Z") + 1)})
KEYS.update({str(n): 0x30 + n for n in range(10)})
KEYS.update({f"f{n}": 0x6F + n for n in range(1, 25)})
EXTENDED_KEYS = {"pageup", "pagedown", "end", "home", "left", "up", "right",
                 "down", "printscreen", "insert", "delete", "win", "numlock"}


def virtual_bounds(monitors):
    """Bounding rectangle in physical pixels, preserving negative origins."""
    if not monitors:
        raise U2CError("NO_INTERACTIVE_DESKTOP", "No display monitors are available")
    left = min(m["left"] for m in monitors)
    top = min(m["top"] for m in monitors)
    right = max(m["left"] + m["width"] for m in monitors)
    bottom = max(m["top"] + m["height"] for m in monitors)
    return {"left": left, "top": top, "width": right - left, "height": bottom - top}


def absolute_mouse_point(x, y, bounds):
    """Convert physical pixel centers to SendInput's virtual desktop space."""
    for value in (x, y):
        if isinstance(value, bool) or not isinstance(value, int):
            raise U2CError("OUT_OF_BOUNDS", "Physical coordinates must be integers")
    left, top, width, height = (bounds[k] for k in ("left", "top", "width", "height"))
    if width < 1 or height < 1 or not (left <= x < left + width and top <= y < top + height):
        raise U2CError("OUT_OF_BOUNDS", "Point is outside the physical virtual desktop")
    return (min(65535, ((x - left) * 65536 + 32768) // width),
            min(65535, ((y - top) * 65536 + 32768) // height))


def unicode_units(text):
    """UTF-16 code units, including both halves of supplementary characters."""
    encoded = text.encode("utf-16-le")
    return [int.from_bytes(encoded[n:n + 2], "little") for n in range(0, len(encoded), 2)]


class WindowsDriver:
    # Chromium drops back-to-back VK_PACKET pairs; pace multi-character text.
    DEFAULT_TYPE_DELAY_MS = 12

    def __init__(self):
        self._loaded = False
        self._held_keys = set()
        self._held_buttons = set()
        self._dpi = None
        self.type_delay = self._configured_type_delay()

    @classmethod
    def _configured_type_delay(cls):
        """Delay between typed characters, from U2C_TYPE_DELAY_MS (0-200 ms)."""
        try:
            milliseconds = float(os.environ.get("U2C_TYPE_DELAY_MS", cls.DEFAULT_TYPE_DELAY_MS))
        except ValueError:
            milliseconds = float(cls.DEFAULT_TYPE_DELAY_MS)
        return max(0.0, min(200.0, milliseconds)) / 1000.0

    def _load(self):
        if self._loaded:
            if hasattr(self, "_dpi_thread_id"):
                thread_id = self.kernel32.GetCurrentThreadId()
                if thread_id != self._dpi_thread_id:
                    self._dpi = self._enable_physical_coordinates()
                    self._dpi_thread_id = thread_id
            return
        if sys.platform != "win32":
            raise U2CError("NO_INTERACTIVE_DESKTOP", "The native desktop driver requires Windows 10/11",
                           platform=sys.platform)
        self.user32 = C.WinDLL("user32", use_last_error=True)
        self.gdi32 = C.WinDLL("gdi32", use_last_error=True)
        self.kernel32 = C.WinDLL("kernel32", use_last_error=True)
        self.dwmapi = C.WinDLL("dwmapi", use_last_error=True)
        try:
            self.shcore = C.WinDLL("shcore", use_last_error=True)
        except OSError:
            self.shcore = None
        u, g, k = self.user32, self.gdi32, self.kernel32
        self._monitor_callback = C.WINFUNCTYPE(BOOL, HANDLE, HANDLE, C.POINTER(RECT), C.c_ssize_t)
        self._window_callback = C.WINFUNCTYPE(BOOL, HANDLE, C.c_ssize_t)
        signatures = [
            (u.GetThreadDesktop, [DWORD], HANDLE),
            (u.GetProcessWindowStation, [], HANDLE),
            (u.OpenInputDesktop, [DWORD, BOOL, DWORD], HANDLE),
            (u.CloseDesktop, [HANDLE], BOOL),
            (u.GetUserObjectInformationW, [HANDLE, C.c_int, C.c_void_p, DWORD, C.POINTER(DWORD)], BOOL),
            (u.GetForegroundWindow, [], HANDLE),
            (u.SetForegroundWindow, [HANDLE], BOOL),
            (u.GetWindowThreadProcessId, [HANDLE, C.POINTER(DWORD)], DWORD),
            (u.EnumWindows, [self._window_callback, C.c_ssize_t], BOOL),
            (u.GetAncestor, [HANDLE, UINT], HANDLE),
            (u.GetClientRect, [HANDLE, C.POINTER(RECT)], BOOL),
            (u.ClientToScreen, [HANDLE, C.POINTER(POINT)], BOOL),
            (u.IsWindowVisible, [HANDLE], BOOL),
            (u.IsIconic, [HANDLE], BOOL),
            (u.GetWindow, [HANDLE, UINT], HANDLE),
            (u.GetWindowRect, [HANDLE, C.POINTER(RECT)], BOOL),
            (u.WindowFromPoint, [POINT], HANDLE),
            (u.PrintWindow, [HANDLE, HANDLE, UINT], BOOL),
            (self.dwmapi.DwmGetWindowAttribute, [HANDLE, DWORD, C.c_void_p, DWORD], LONG),
            (u.GetKeyboardLayout, [DWORD], HANDLE),
            (u.VkKeyScanExW, [C.c_wchar, HANDLE], C.c_int16),
            (u.MapVirtualKeyExW, [UINT, UINT, HANDLE], UINT),
            (u.ToUnicodeEx, [UINT, UINT, C.POINTER(C.c_ubyte), C.POINTER(C.c_wchar),
                             C.c_int, UINT, HANDLE], C.c_int),
            (u.GetAsyncKeyState, [C.c_int], C.c_int16),
            (u.GetKeyState, [C.c_int], C.c_int16),
            (u.GetCursorPos, [C.POINTER(POINT)], BOOL),
            (u.EnumDisplayMonitors, [HANDLE, C.POINTER(RECT), self._monitor_callback, C.c_ssize_t], BOOL),
            (u.GetMonitorInfoW, [HANDLE, C.POINTER(MONITORINFOEXW)], BOOL),
            (u.SendInput, [UINT, C.POINTER(INPUT), C.c_int], UINT),
            (u.GetDC, [HANDLE], HANDLE),
            (u.ReleaseDC, [HANDLE, HANDLE], C.c_int),
            (k.GetCurrentThreadId, [], DWORD),
            (g.CreateCompatibleDC, [HANDLE], HANDLE),
            (g.CreateCompatibleBitmap, [HANDLE, C.c_int, C.c_int], HANDLE),
            (g.SelectObject, [HANDLE, HANDLE], HANDLE),
            (g.DeleteObject, [HANDLE], BOOL),
            (g.DeleteDC, [HANDLE], BOOL),
            (g.BitBlt, [HANDLE, C.c_int, C.c_int, C.c_int, C.c_int, HANDLE,
                        C.c_int, C.c_int, DWORD], BOOL),
            (g.PatBlt, [HANDLE, C.c_int, C.c_int, C.c_int, C.c_int, DWORD], BOOL),
            (g.GetDIBits, [HANDLE, HANDLE, UINT, UINT, C.c_void_p, C.POINTER(BITMAPINFO), UINT], C.c_int),
        ]
        for function, args, result in signatures:
            function.argtypes, function.restype = args, result
        if self.shcore:
            self.shcore.GetDpiForMonitor.argtypes = [HANDLE, C.c_int, C.POINTER(UINT), C.POINTER(UINT)]
            self.shcore.GetDpiForMonitor.restype = C.c_int32
        self._dpi = self._enable_physical_coordinates()
        self._dpi_thread_id = k.GetCurrentThreadId()
        self._loaded = True

    def _enable_physical_coordinates(self):
        """Set DPI awareness before reading coordinates; refuse virtualized input."""
        u = self.user32
        if not hasattr(u, "SetProcessDpiAwarenessContext"):
            return {"physical": False, "issue": "Per-monitor DPI API is unavailable"}
        u.SetProcessDpiAwarenessContext.argtypes = [HANDLE]
        u.SetProcessDpiAwarenessContext.restype = BOOL
        u.GetThreadDpiAwarenessContext.argtypes = []
        u.GetThreadDpiAwarenessContext.restype = HANDLE
        u.GetAwarenessFromDpiAwarenessContext.argtypes = [HANDLE]
        u.GetAwarenessFromDpiAwarenessContext.restype = C.c_int
        # Access denied is normal if the host already set its process awareness.
        process_set = bool(u.SetProcessDpiAwarenessContext(HANDLE(-4)))
        current = u.GetAwarenessFromDpiAwarenessContext(u.GetThreadDpiAwarenessContext())
        if current != 2 and hasattr(u, "SetThreadDpiAwarenessContext"):
            u.SetThreadDpiAwarenessContext.argtypes = [HANDLE]
            u.SetThreadDpiAwarenessContext.restype = HANDLE
            u.SetThreadDpiAwarenessContext(HANDLE(-4))
            current = u.GetAwarenessFromDpiAwarenessContext(u.GetThreadDpiAwarenessContext())
        return {"physical": current == 2, "awareness": current, "process_context_set": process_set}

    @staticmethod
    def _last_error():
        return C.get_last_error() if hasattr(C, "get_last_error") else 0

    def integrity_level(self):
        """Report this process's Windows mandatory integrity label.

        UIPI drops synthetic input aimed at a higher-integrity window while
        SendInput still reports one accepted event. Sandboxes such as the DSH
        tool shell run commands at Low integrity, which silently disables
        desktop-wide mouse and keyboard injection. Doctor surfaces the level so
        a host can run the runtime unsandboxed instead of trusting the receipt.
        """
        if sys.platform != "win32":
            return {"level": "unsupported", "rid": None}
        cached = getattr(self, "_integrity", None)
        if cached is not None:
            return cached
        result = {"level": "unknown", "rid": None}
        try:
            advapi32 = C.WinDLL("advapi32", use_last_error=True)
            kernel32 = C.WinDLL("kernel32", use_last_error=True)
            advapi32.OpenProcessToken.argtypes = [HANDLE, DWORD, C.POINTER(HANDLE)]
            advapi32.OpenProcessToken.restype = BOOL
            advapi32.GetTokenInformation.argtypes = [HANDLE, C.c_int, C.c_void_p, DWORD, C.POINTER(DWORD)]
            advapi32.GetTokenInformation.restype = BOOL
            advapi32.GetSidSubAuthorityCount.argtypes = [HANDLE]
            advapi32.GetSidSubAuthorityCount.restype = C.POINTER(C.c_ubyte)
            advapi32.GetSidSubAuthority.argtypes = [HANDLE, DWORD]
            advapi32.GetSidSubAuthority.restype = C.POINTER(DWORD)
            token = HANDLE()
            if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, C.byref(token)):
                return {**result, "winerror": self._last_error()}
            try:
                size = DWORD()
                advapi32.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, None, 0, C.byref(size))
                if not size.value:
                    return {**result, "winerror": self._last_error()}
                buffer = C.create_string_buffer(size.value)
                if not advapi32.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, buffer, size.value, C.byref(size)):
                    return {**result, "winerror": self._last_error()}
                label = C.cast(buffer, C.POINTER(TOKEN_MANDATORY_LABEL)).contents.Label
                count = advapi32.GetSidSubAuthorityCount(label.Sid)
                if not count or not count[0]:
                    return {**result, "winerror": self._last_error() or None}
                last = advapi32.GetSidSubAuthority(label.Sid, count[0] - 1)
                rid = int(last[0])
                result = {"level": INTEGRITY_LEVELS.get(rid, f"0x{rid:04x}"), "rid": rid,
                          "input_integrity_ok": rid >= 0x2000}
            finally:
                kernel32.CloseHandle.argtypes = [HANDLE]
                kernel32.CloseHandle(token)
        except (OSError, AttributeError) as exc:
            return {**result, "error": type(exc).__name__}
        self._integrity = result
        return result

    def _object_name(self, handle):
        needed = DWORD()
        self.user32.GetUserObjectInformationW(handle, 2, None, 0, C.byref(needed))
        if not needed.value:
            return None
        name = C.create_unicode_buffer(max(1, needed.value // C.sizeof(C.c_wchar)))
        if not self.user32.GetUserObjectInformationW(handle, 2, name, C.sizeof(name), C.byref(needed)):
            return None
        return name.value

    def _receives_input(self, handle):
        value, needed = BOOL(), DWORD()
        if not self.user32.GetUserObjectInformationW(handle, 6, C.byref(value), C.sizeof(value), C.byref(needed)):
            return None
        return bool(value.value)

    def _monitors(self):
        monitors, errors = [], []

        def callback(handle, dc, rect, param):
            try:
                info = MONITORINFOEXW()
                info.cbSize = C.sizeof(info)
                if not self.user32.GetMonitorInfoW(handle, C.byref(info)):
                    errors.append("GetMonitorInfoW failed")
                    return False
                r = info.rcMonitor
                dx, dy = UINT(), UINT()
                dpi_ok = bool(self.shcore) and self.shcore.GetDpiForMonitor(handle, 0, C.byref(dx), C.byref(dy)) == 0
                monitors.append({"id": len(monitors) + 1, "device": info.szDevice,
                                 "left": r.left, "top": r.top,
                                 "width": r.right - r.left, "height": r.bottom - r.top,
                                 "dpi_x": dx.value if dpi_ok else None,
                                 "dpi_y": dy.value if dpi_ok else None,
                                 "primary": bool(info.dwFlags & 1)})
                return True
            except Exception as exc:
                errors.append(type(exc).__name__)
                return False

        cb = self._monitor_callback(callback)
        if not self.user32.EnumDisplayMonitors(None, None, cb, 0) or errors:
            raise U2CError("PERMISSION_DENIED", "Could not enumerate physical display monitors",
                           winerror=self._last_error(), issues=errors)
        return monitors

    def doctor(self):
        result = {"platform": sys.platform, "interactive": False, "thread_desktop": None,
                  "input_desktop": None, "window_station": None, "foreground_hwnd": 0,
                  "monitors": [], "permission_issues": []}
        try:
            self._load()
        except U2CError as exc:
            result["permission_issues"].append(exc.as_dict())
            return result
        u = self.user32
        result["dpi"] = self._dpi
        thread = u.GetThreadDesktop(self.kernel32.GetCurrentThreadId())
        result["thread_desktop"] = self._object_name(thread) if thread else None
        station = u.GetProcessWindowStation()
        result["window_station"] = self._object_name(station) if station else None
        result["thread_receives_input"] = self._receives_input(thread) if thread else None
        desk = u.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS only.
        if desk:
            try:
                result["input_desktop"] = self._object_name(desk)
                result["input_receives_input"] = self._receives_input(desk)
            finally:
                u.CloseDesktop(desk)
        else:
            result["permission_issues"].append({"code": "NO_INTERACTIVE_DESKTOP",
                "message": "The input desktop is inaccessible (locked, secure, or isolated desktop)",
                "winerror": self._last_error()})
        if (result["window_station"] or "").lower() != "winsta0":
            result["permission_issues"].append({"code": "NO_INTERACTIVE_DESKTOP",
                "message": "Process is not in the interactive WinSta0 window station"})
        if (result["thread_desktop"] or "").lower() != "default" or (result["input_desktop"] or "").lower() != "default":
            result["permission_issues"].append({"code": "NO_INTERACTIVE_DESKTOP",
                "message": "Process desktop or input desktop is not the user's Default desktop"})
        if result["thread_receives_input"] is not True or result.get("input_receives_input") is not True:
            result["permission_issues"].append({"code": "NO_INTERACTIVE_DESKTOP",
                "message": "The process desktop is not proven to receive physical user input"})
        if not self._dpi["physical"]:
            result["permission_issues"].append({"code": "PERMISSION_DENIED",
                "message": "Per-monitor DPI awareness is required for physical coordinates"})
        result["foreground_hwnd"] = int(u.GetForegroundWindow() or 0)
        result["integrity"] = self.integrity_level()
        try:
            result["monitors"] = self._monitors()
        except U2CError as exc:
            result["permission_issues"].append(exc.as_dict())
        if not result["monitors"]:
            result["permission_issues"].append({"code": "NO_INTERACTIVE_DESKTOP", "message": "No monitors detected"})
        result["interactive"] = not result["permission_issues"]
        return result

    def assert_interactive(self):
        info = self.doctor()
        if not info["interactive"]:
            raise U2CError("NO_INTERACTIVE_DESKTOP", "An unlocked authorized Default input desktop is required",
                           diagnostics=info)
        return info

    def foreground(self):
        self._load()
        return int(self.user32.GetForegroundWindow() or 0)

    def capture_pixels(self, bounds):
        """BitBlt the real desktop, then read a top-down RGB DIB."""
        self.assert_interactive()
        return self._capture_bitmap(bounds)

    def resolve_window_client(self, pid):
        """Select a unique client for the supplied process without reading window titles."""
        self._load()
        u = self.user32
        matches = []

        @self._window_callback
        def collect(hwnd, _):
            owner = DWORD()
            if u.GetWindowThreadProcessId(hwnd, C.byref(owner)) and owner.value == pid:
                matches.append(hwnd)
            return True

        if not u.EnumWindows(collect, 0):
            raise U2CError("WINDOW_UNAVAILABLE", "Could not enumerate top-level windows", winerror=self._last_error())
        eligible = []
        for hwnd in matches:
            try:
                bounds, owner = self.window_client(hwnd)
            except U2CError as exc:
                if exc.code != "WINDOW_UNAVAILABLE":
                    raise
                continue
            if owner == pid and bounds["width"] > 0 and bounds["height"] > 0 and not self._window_is_cloaked(hwnd):
                eligible.append(hwnd)
        if not eligible:
            raise U2CError("WINDOW_UNAVAILABLE", "The supplied PID has no eligible top-level window", pid=pid)
        if len(eligible) != 1:
            raise U2CError("WINDOW_AMBIGUOUS", "More than one window matches the supplied PID; specify an authorized HWND",
                           pid=pid, candidate_count=len(eligible))
        return eligible[0]

    def window_owner(self, hwnd):
        """Read the owner of a top-level handle, including a minimized window."""
        self._load()
        u = self.user32
        pid = DWORD()
        if not (u.GetWindowThreadProcessId(hwnd, C.byref(pid)) and pid.value and u.GetAncestor(hwnd, 2) == hwnd):
            raise U2CError("WINDOW_UNAVAILABLE", "The selected top-level window is unavailable")
        return pid.value

    def window_client(self, hwnd):
        """Read the physical rectangle and owning PID of a visible top-level client."""
        pid = self.window_owner(hwnd)
        u = self.user32
        rect, origin = RECT(), POINT()
        if not (u.IsWindowVisible(hwnd) and not u.IsIconic(hwnd) and
                u.GetClientRect(hwnd, C.byref(rect)) and u.ClientToScreen(hwnd, C.byref(origin))):
            raise U2CError("WINDOW_UNAVAILABLE", "The selected top-level client is unavailable")
        return {"left": origin.x, "top": origin.y, "width": rect.right, "height": rect.bottom}, pid

    def activate_window_input(self, hwnd, pid, bounds, checkpoint):
        """Request normal activation for the verified input target at most once."""
        checkpoint()
        if self.window_client(hwnd) != (bounds, pid):
            raise U2CError("STALE_FRAME", "The selected window moved, resized, or changed ownership")
        if self.foreground() == hwnd:
            return
        if self.user32.SetForegroundWindow(hwnd):
            deadline = time.monotonic() + 0.25
            while self.foreground() != hwnd and time.monotonic() < deadline:
                checkpoint()
                time.sleep(0.02)
        if self.foreground() != hwnd:
            raise U2CError("FOCUS_LOST", "Windows did not grant foreground focus to the selected window")

    def assert_window_input(self, hwnd, pid, bounds, *, point=None):
        """Verify the foreground target and the area that must accept input."""
        if self.foreground() != hwnd:
            raise U2CError("FOCUS_LOST", "The selected window is no longer in the foreground")
        if self.window_client(hwnd) != (bounds, pid):
            raise U2CError("STALE_FRAME", "The selected window moved, resized, or changed ownership")
        area = bounds
        if point is not None:
            x, y = point
            if not (bounds["left"] <= x < bounds["left"] + bounds["width"] and
                    bounds["top"] <= y < bounds["top"] + bounds["height"]):
                raise U2CError("OUT_OF_BOUNDS", "The click lies outside the bound window client")
            area = {"left": x, "top": y, "width": 1, "height": 1}
        self._assert_window_unoccluded(hwnd, area)
        if point is not None:
            hit = self.user32.WindowFromPoint(POINT(*point))
            if not hit or self.user32.GetAncestor(hit, 2) != hwnd:
                raise U2CError("TARGET_OCCLUDED", "The click does not hit the bound window")
        if self.foreground() != hwnd:
            raise U2CError("FOCUS_LOST", "Foreground changed during window stacking validation")

    def _window_is_cloaked(self, hwnd):
        cloaked = DWORD()
        result = self.dwmapi.DwmGetWindowAttribute(hwnd, 14, C.byref(cloaked), C.sizeof(cloaked))
        return result == 0 and bool(cloaked.value)

    def _assert_window_unoccluded(self, hwnd, bounds):
        u = self.user32
        right, bottom = bounds["left"] + bounds["width"], bounds["top"] + bounds["height"]
        other = u.GetWindow(hwnd, 3)  # GW_HWNDPREV
        for _ in range(512):
            if not other:
                return
            if u.IsWindowVisible(other) and not u.IsIconic(other) and not self._window_is_cloaked(other):
                rect = RECT()
                if not u.GetWindowRect(other, C.byref(rect)):
                    raise U2CError("TARGET_OCCLUDED", "Could not verify overlapping window bounds")
                if (max(rect.left, bounds["left"]) < min(rect.right, right) and
                        max(rect.top, bounds["top"]) < min(rect.bottom, bottom)):
                    raise U2CError("TARGET_OCCLUDED", "Another visible window overlaps the input area")
            other = u.GetWindow(other, 3)
        raise U2CError("TARGET_OCCLUDED", "Could not verify the complete window stacking order")

    def capture_window_pixels(self, hwnd, bounds, pid):
        """Run synchronous PrintWindow outside the caller with a hard timeout."""
        from PIL import Image

        self.assert_interactive()
        if self.window_client(hwnd) != (bounds, pid):
            raise U2CError("STALE_FRAME", "The selected window changed before capture")
        package_root = str(Path(__file__).resolve().parents[2])
        worker = ("import sys; sys.path.insert(0, sys.argv.pop(1)); "
                  "from u2c.drivers.window_capture import main; main()")
        arguments = [str(n) for n in (hwnd, pid, bounds["left"], bounds["top"], bounds["width"], bounds["height"])]
        try:
            result = subprocess.run([sys.executable, "-I", "-B", "-c", worker, package_root, *arguments],
                                    stdin=subprocess.DEVNULL, capture_output=True, timeout=3,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
        except subprocess.TimeoutExpired as exc:
            raise U2CError("CAPTURE_FAILED", "The target did not render its client within three seconds") from exc
        except OSError as exc:
            raise U2CError("CAPTURE_FAILED", "Could not start the window capture helper") from exc
        for code, exit_code in WINDOW_CAPTURE_ERRORS.items():
            if result.returncode == exit_code:
                if code == "CAPTURE_FAILED":
                    raise U2CError(code, "The target did not return a usable client image", retryable=True)
                raise U2CError(code, "The window capture helper could not safely render the bound client")
        size = bounds["width"], bounds["height"]
        if result.returncode or len(result.stdout) != size[0] * size[1] * 3:
            raise U2CError("CAPTURE_FAILED", "The target did not return a usable client image")
        if self.window_client(hwnd) != (bounds, pid):
            raise U2CError("STALE_FRAME", "The selected window changed during capture")
        return Image.frombytes("RGB", size, result.stdout)

    def _render_window_pixels(self, hwnd, bounds, pid):
        self.assert_interactive()
        # Some composited windows render only when asked for full content.
        # Both attempts still render this HWND into a private client bitmap.
        for flags in (PW_CLIENTONLY, PW_CLIENTONLY | PW_RENDERFULLCONTENT):
            if self.window_client(hwnd) != (bounds, pid):
                raise U2CError("STALE_FRAME", "The selected window changed before rendering")
            try:
                image = self._capture_bitmap(bounds, hwnd=hwnd, print_flags=flags)
            except U2CError as exc:
                if exc.code != "CAPTURE_FAILED" or flags != PW_CLIENTONLY:
                    raise
                continue
            if self.window_client(hwnd) != (bounds, pid):
                raise U2CError("STALE_FRAME", "The selected window changed during rendering")
            if image.getbbox() is not None:
                return image
        raise U2CError("CAPTURE_FAILED", "The target returned an empty window image")

    def _capture_bitmap(self, bounds, *, hwnd=None, print_flags=PW_CLIENTONLY):
        from PIL import Image

        u, g = self.user32, self.gdi32
        width, height = bounds["width"], bounds["height"]
        if width <= 0 or height <= 0 or width * height > 64_000_000:
            raise U2CError("OUT_OF_BOUNDS", "Capture dimensions exceed the supported pixel budget")
        screen, memory, bitmap, old = None, None, None, None
        selected = False
        try:
            screen = u.GetDC(hwnd)
            if not screen:
                raise U2CError("PERMISSION_DENIED", "Could not acquire the capture device context")
            memory = g.CreateCompatibleDC(screen)
            bitmap = g.CreateCompatibleBitmap(screen, width, height)
            if not memory or not bitmap:
                raise U2CError("PERMISSION_DENIED", "Could not allocate desktop capture resources")
            old = g.SelectObject(memory, bitmap)
            if not old or old == C.c_void_p(-1).value:
                raise U2CError("PERMISSION_DENIED", "Could not select desktop capture bitmap")
            selected = True
            if hwnd is None:
                if not g.BitBlt(memory, 0, 0, width, height, screen, bounds["left"], bounds["top"],
                                0x00CC0020 | 0x40000000):  # SRCCOPY | CAPTUREBLT
                    raise U2CError("PERMISSION_DENIED", "Real desktop BitBlt failed", winerror=self._last_error())
            else:
                # PrintWindow asks this specific HWND to render into a cleared
                # bitmap. PW_CLIENTONLY limits the result to the client.
                if not g.PatBlt(memory, 0, 0, width, height, 0x00000042):
                    raise U2CError("PERMISSION_DENIED", "Could not clear the private capture bitmap",
                                   winerror=self._last_error())
                if not u.PrintWindow(hwnd, memory, print_flags):
                    raise U2CError("CAPTURE_FAILED", "The target could not render its client",
                                   winerror=self._last_error())
            # GetDIBits explicitly forbids reading a bitmap selected into a DC.
            if not g.SelectObject(memory, old):
                raise U2CError("PERMISSION_DENIED", "Could not deselect capture bitmap")
            selected = False
            info = BITMAPINFO()
            info.bmiHeader.biSize = C.sizeof(BITMAPINFOHEADER)
            info.bmiHeader.biWidth = width
            info.bmiHeader.biHeight = -height
            info.bmiHeader.biPlanes = 1
            info.bmiHeader.biBitCount = 32
            info.bmiHeader.biSizeImage = width * height * 4
            pixels = C.create_string_buffer(width * height * 4)
            if g.GetDIBits(memory, bitmap, 0, height, pixels, C.byref(info), 0) != height:
                raise U2CError("PERMISSION_DENIED", "GetDIBits could not read every desktop scan line",
                               winerror=self._last_error())
            image = Image.frombytes("RGB", (width, height), pixels.raw, "raw", "BGRX")
            self.assert_interactive()
            return image
        finally:
            if selected and memory and old:
                g.SelectObject(memory, old)
            if bitmap:
                g.DeleteObject(bitmap)
            if memory:
                g.DeleteDC(memory)
            if screen:
                u.ReleaseDC(hwnd, screen)

    @contextmanager
    def _protected(self):
        try:
            yield
        except BaseException as exc:
            try:
                self.release_all()
            except Exception as cleanup:
                raise U2CError("PERMISSION_DENIED", "Input stopped but held input could not be released",
                               original_error=str(exc), cleanup_error=str(cleanup)) from exc
            raise

    def _checkpoint(self, checkpoint):
        if checkpoint:
            checkpoint()
        self.assert_interactive()

    def _raw_send(self, event):
        # A below-medium process cannot inject into normal desktop windows;
        # UIPI drops the event and SendInput can still return 1 without setting
        # GetLastError. Refuse honestly instead of reporting a false success.
        integrity = self.integrity_level()
        if integrity.get("input_integrity_ok") is False:
            raise U2CError("PERMISSION_DENIED",
                           "Windows UIPI drops synthetic input from a below-medium integrity process. "
                           "Run U2C through a medium-integrity launcher or an unsandboxed terminal.",
                           integrity=integrity.get("level"), input_integrity_ok=False)
        # UIPI can also accept the call (return 1) while the kernel silently
        # drops the event; sometimes the only signal is
        # GetLastError()==ACCESS_DENIED. Clear the per-thread error first so a
        # stale value cannot fake a rejection.
        C.set_last_error(0)
        count = int(self.user32.SendInput(1, C.byref(event), C.sizeof(INPUT)))
        winerror = self._last_error()
        if count != 1 or winerror == ERROR_ACCESS_DENIED:
            raise U2CError("PERMISSION_DENIED",
                           "Windows rejected synthetic input; UIPI may block a lower-integrity process or an elevated window.",
                           requested=1, sent=count, winerror=winerror,
                           integrity=integrity.get("level"), input_integrity_ok=integrity.get("input_integrity_ok"))

    def _mouse(self, flags, x=0, y=0, data=0):
        event = INPUT()
        event.type = 0
        event.mi = MOUSEINPUT(x, y, data & 0xFFFFFFFF, flags, 0, 0)
        self._raw_send(event)

    def _keyboard(self, vk, scan=0, flags=0, *, up=False):
        key = (vk, scan, flags)
        if not up:
            self._held_keys.add(key)  # Conservative if SendInput fails midway.
        event = INPUT()
        event.type = 1
        event.ki = KEYBDINPUT(vk, scan, flags | (KEY_UP if up else 0), 0, 0)
        self._raw_send(event)
        if up:
            self._held_keys.discard(key)

    def _button(self, button, *, up=False):
        if button not in BUTTON_FLAGS:
            raise U2CError("INVALID_REQUEST", "Unsupported mouse button", button=button)
        if not up:
            self._held_buttons.add(button)
        self._mouse(BUTTON_FLAGS[button][1 if up else 0])
        if up:
            self._held_buttons.discard(button)

    @staticmethod
    def _validate_target(x, y, monitors):
        normalized = absolute_mouse_point(x, y, virtual_bounds(monitors))
        if not any(m["left"] <= x < m["left"] + m["width"] and
                   m["top"] <= y < m["top"] + m["height"] for m in monitors):
            raise U2CError("OUT_OF_BOUNDS", "Physical point is outside all monitors (or in a desktop gap)")
        return normalized

    @staticmethod
    def _validate_duration(duration):
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or not 0 <= duration <= 30:
            raise U2CError("INVALID_REQUEST", "Mouse movement duration must be between zero and 30 seconds")

    def _position(self, x, y, monitors):
        nx, ny = self._validate_target(x, y, monitors)
        self._mouse(MOUSE_MOVE | MOUSE_ABSOLUTE | MOUSE_VIRTUALDESK, nx, ny)

    def _move(self, x, y, duration, checkpoint):
        self._checkpoint(checkpoint)
        monitors = self._monitors()
        self._validate_target(x, y, monitors)
        self._validate_duration(duration)
        if duration == 0:
            self._position(x, y, monitors)
            return
        origin = POINT()
        if not self.user32.GetCursorPos(C.byref(origin)):
            raise U2CError("PERMISSION_DENIED", "Could not read the physical pointer position")
        start = time.monotonic()
        while True:
            self._checkpoint(checkpoint)
            fraction = min(1.0, (time.monotonic() - start) / duration)
            # Interpolation may cross empty monitor gaps. Clamp the target only
            # when fraction=1; SendInput can traverse an ordinary desktop seam.
            px = round(origin.x + (x - origin.x) * fraction)
            py = round(origin.y + (y - origin.y) * fraction)
            if fraction == 1:
                self._position(x, y, monitors)
                return
            nx, ny = absolute_mouse_point(px, py, virtual_bounds(monitors))
            self._mouse(MOUSE_MOVE | MOUSE_ABSOLUTE | MOUSE_VIRTUALDESK, nx, ny)
            time.sleep(min(.01, max(0, duration - (time.monotonic() - start))))

    def move(self, x, y, duration=0, checkpoint=None):
        with self._protected():
            self._move(x, y, duration, checkpoint)

    def _pause(self, seconds, checkpoint):
        """Bounded event-pump opportunity with stop/focus checks while waiting."""
        deadline = time.monotonic() + seconds
        while True:
            self._checkpoint(checkpoint)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(.01, remaining))

    def click(self, x, y, button="left", count=1, checkpoint=None, *, verify_position=False):
        with self._protected():
            if isinstance(count, bool) or count not in (1, 2):
                raise U2CError("INVALID_REQUEST", "Click count must be one or two")
            if button not in BUTTON_FLAGS:
                raise U2CError("INVALID_REQUEST", "Unsupported mouse button", button=button)
            self._move(x, y, 0, checkpoint)
            self._pause(.03, checkpoint)
            for index in range(count):
                self._checkpoint(checkpoint)
                if verify_position:
                    cursor = POINT()
                    if not self.user32.GetCursorPos(C.byref(cursor)) or (cursor.x, cursor.y) != (x, y):
                        raise U2CError("STALE_FRAME", "The pointer left the verified click location")
                self._button(button)
                self._pause(.03, checkpoint)
                self._button(button, up=True)
                if index + 1 < count:
                    self._pause(.06, checkpoint)

    def drag(self, x, y, to_x, to_y, button="left", duration=.3, checkpoint=None):
        with self._protected():
            self._checkpoint(checkpoint)
            if button not in BUTTON_FLAGS:
                raise U2CError("INVALID_REQUEST", "Unsupported mouse button", button=button)
            monitors = self._monitors()
            self._validate_target(x, y, monitors)
            self._validate_target(to_x, to_y, monitors)
            self._validate_duration(duration)
            self._move(x, y, 0, checkpoint)
            self._pause(.03, checkpoint)
            self._button(button)
            self._move(to_x, to_y, duration, checkpoint)
            self._button(button, up=True)

    def scroll(self, amount, x=None, y=None, checkpoint=None):
        with self._protected():
            self._checkpoint(checkpoint)
            if (x is None) != (y is None):
                raise U2CError("INVALID_REQUEST", "Both scroll coordinates are required together")
            if x is not None:
                self._move(x, y, 0, checkpoint)
            if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount) or abs(amount) > 100:
                raise U2CError("INVALID_REQUEST", "Scroll amount must be finite and at most 100 wheel ticks")
            self._mouse(MOUSE_WHEEL, data=round(amount * 120))

    def type_text(self, text, checkpoint=None, method="unicode"):
        """Type via Unicode packets or explicit layout-aware ASCII keystrokes.

        `keyboard` is a generic compatibility option for applications that do not
        consume VK_PACKET. It supports printable ASCII only, requires a neutral
        modifier state and CapsLock off for letters, and never uses Ctrl/Alt/Win
        mappings, a clipboard, an application API, or a keyboard-layout change.
        SendInput acceptance still requires visual verification by the host.
        """
        with self._protected():
            self._checkpoint(checkpoint)
            if not isinstance(text, str):
                raise U2CError("INVALID_REQUEST", "Typing requires a Unicode string")
            if method not in ("unicode", "keyboard"):
                raise U2CError("INVALID_REQUEST", "Text input method must be unicode or keyboard")
            if method == "keyboard":
                self._type_keyboard(text, checkpoint)
                return
            try:
                units = unicode_units(text)
            except UnicodeEncodeError as exc:
                raise U2CError("INVALID_REQUEST", "Text contains an unpaired Unicode surrogate") from exc
            for scan in units:
                self._checkpoint(checkpoint)
                self._keyboard(0, scan, KEY_UNICODE)
                self._keyboard(0, scan, KEY_UNICODE, up=True)
                # Chromium-based browsers drop back-to-back VK_PACKET pairs.
                # A short inter-character pause keeps fast typing lossless.
                if len(units) > 1:
                    time.sleep(self.type_delay)

    def _foreground_layout(self):
        hwnd = self.foreground()
        if not hwnd:
            raise U2CError("FOCUS_LOST", "No foreground window is available for keyboard typing")
        thread_id = self.user32.GetWindowThreadProcessId(hwnd, None)
        if not thread_id:
            raise U2CError("FOCUS_LOST", "Could not identify the foreground keyboard thread")
        layout = self.user32.GetKeyboardLayout(thread_id)
        if not layout:
            raise U2CError("PERMISSION_DENIED", "Could not inspect the foreground keyboard layout")
        return hwnd, int(layout)

    def _assert_keyboard_neutral(self, *, letters=False, own_left_shift=False):
        for vk in MODIFIER_VKS:
            if own_left_shift and vk == 0xA0:
                continue
            if int(self.user32.GetAsyncKeyState(vk)) & 0x8000:
                raise U2CError("PERMISSION_DENIED", "A modifier is already held; release it before keyboard typing",
                               recovery="Release physical Ctrl, Alt, Win and Shift keys, then reobserve")
        if letters and int(self.user32.GetKeyState(0x14)) & 1:
            raise U2CError("PERMISSION_DENIED", "CapsLock must be off for case-correct ASCII keyboard typing",
                           recovery="Turn CapsLock off explicitly, then reobserve")

    def _scan_for_vk(self, vk, layout):
        scan = int(self.user32.MapVirtualKeyExW(vk, 4, layout))
        if not scan or scan > 0xFFFF or scan >> 8 not in (0, 0xE0):
            raise U2CError("INVALID_REQUEST", "Keyboard layout has no supported physical scan code")
        return scan & 0xFF, KEY_SCANCODE | (KEY_EXTENDED if scan >> 8 == 0xE0 else 0)

    def _validate_layout_character(self, char, vk, scan, shifted, layout):
        # MAPVK_VK_TO_CHAR is unshifted and cannot identify a shifted dead key.
        # Windows 10 1607+ ToUnicodeEx bit2 preserves the keyboard/dead-key state;
        # P0 already requires newer per-monitor DPI APIs. Never translate with
        # flags=0, which could consume a user's composition/dead-key buffer.
        state = (C.c_ubyte * 256)()
        if shifted:
            state[0x10] = state[0xA0] = 0x80
        output = C.create_unicode_buffer(8)
        count = int(self.user32.ToUnicodeEx(vk, scan, state, output, len(output), 4, layout))
        if count != 1 or output[0] != char:
            raise U2CError("INVALID_REQUEST", "Keyboard mapping is a dead key or does not directly produce the requested ASCII character")

    def _type_keyboard(self, text, checkpoint):
        if any(not 32 <= ord(char) <= 126 for char in text):
            raise U2CError("INVALID_REQUEST", "Keyboard typing supports printable ASCII only; use explicit key actions for controls")
        if not text:
            return
        identity = self._foreground_layout()
        layout = identity[1]
        letters = any(char.isalpha() for char in text)
        self._assert_keyboard_neutral(letters=letters)
        plan = []
        # Validate the entire string before input. AltGr appears as Ctrl+Alt in
        # VkKeyScanEx and is deliberately refused, not silently synthesized.
        for char in text:
            self._checkpoint(checkpoint)
            mapping = int(self.user32.VkKeyScanExW(char, layout))
            if mapping == -1 or mapping & 0xFFFF == 0xFFFF:
                raise U2CError("INVALID_REQUEST", "Character is not representable on the active keyboard layout")
            vk, modifiers = mapping & 0xFF, (mapping >> 8) & 0xFF
            if not 1 <= vk <= 0xFE or modifiers not in (0, 1):
                raise U2CError("INVALID_REQUEST", "Text mapping requires forbidden modifiers; only Shift is allowed")
            if int(self.user32.MapVirtualKeyExW(vk, 2, layout)) & 0x80000000:
                raise U2CError("INVALID_REQUEST", "Dead-key text mappings are unsupported in keyboard typing")
            scan, flags = self._scan_for_vk(vk, layout)
            self._validate_layout_character(char, vk, scan, bool(modifiers), layout)
            plan.append((scan, flags, bool(modifiers)))
        shift_scan, shift_flags = self._scan_for_vk(0xA0, layout)
        for scan, flags, shifted in plan:
            self._checkpoint(checkpoint)
            if self._foreground_layout() != identity:
                raise U2CError("FOCUS_LOST", "Foreground window or keyboard layout changed during text input")
            self._assert_keyboard_neutral(letters=letters)
            if shifted:
                self._keyboard(0, shift_scan, shift_flags)
                self._checkpoint(checkpoint)
                if self._foreground_layout() != identity:
                    raise U2CError("FOCUS_LOST", "Foreground window or keyboard layout changed during text input")
                self._assert_keyboard_neutral(letters=letters, own_left_shift=True)
            self._keyboard(0, scan, flags)
            self._keyboard(0, scan, flags, up=True)
            if shifted:
                self._keyboard(0, shift_scan, shift_flags, up=True)
            if len(plan) > 1:
                time.sleep(self.type_delay)

    def key(self, key, checkpoint=None):
        with self._protected():
            self._checkpoint(checkpoint)
            vk, flags = self._key_value(key)
            self._keyboard(vk, flags=flags)
            self._keyboard(vk, flags=flags, up=True)

    @staticmethod
    def _key_value(key):
        if not isinstance(key, str) or key not in KEYS:
            raise U2CError("INVALID_REQUEST", "Unsupported key name", key=key)
        return KEYS[key], KEY_EXTENDED if key in EXTENDED_KEYS else 0

    def hotkey(self, keys, checkpoint=None):
        with self._protected():
            if not isinstance(keys, (list, tuple)) or not keys or len(keys) > 8:
                raise U2CError("INVALID_REQUEST", "Hotkey requires one to eight named keys")
            values = [self._key_value(key) for key in keys]
            self._checkpoint(checkpoint)
            for vk, flags in values:
                self._checkpoint(checkpoint)
                self._keyboard(vk, flags=flags)
            for vk, flags in reversed(values):
                self._keyboard(vk, flags=flags, up=True)

    def release_all(self):
        """Release only inputs this driver might hold; report cleanup failures."""
        if not self._held_keys and not self._held_buttons:
            return
        if self.integrity_level().get("input_integrity_ok") is False:
            # Nothing this process sent can have reached the desktop.
            return
        self._load()
        errors = []
        for vk, scan, flags in list(self._held_keys):
            try:
                self._keyboard(vk, scan, flags, up=True)
            except Exception as exc:
                errors.append(str(exc))
        for button in list(self._held_buttons):
            try:
                self._button(button, up=True)
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise U2CError("PERMISSION_DENIED", "Could not release all held input; release keys/buttons manually",
                           failures=errors)

    def emergency_release(self):
        """Explicit emergency-stop best effort, including another U2C process.

        Only release events are generated, on the current authorized desktop.
        This cannot guarantee cleanup of a hung process or override Windows UIPI.
        """
        self.assert_interactive()
        errors = []
        # Generic and sided modifier VKs. No printable key is pressed or typed.
        for vk in (0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0x5C):
            try:
                self._keyboard(vk, flags=KEY_EXTENDED if vk in (0xA3, 0xA5, 0x5B, 0x5C) else 0, up=True)
            except U2CError as exc:
                errors.append(exc.as_dict())
        for button in BUTTON_FLAGS:
            try:
                self._button(button, up=True)
            except U2CError as exc:
                errors.append(exc.as_dict())
        if errors:
            raise U2CError("PERMISSION_DENIED", "Emergency release was not fully accepted by Windows", failures=errors)
        return {"status": "release_events_sent", "guaranteed": False}
