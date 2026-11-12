"""The app window's look on Windows 11, and its remembered size and position.

- Title bar: dark mode, coloured like the app's nav bar so the two read as one strip, a matching border, rounded
  corners (DwmSetWindowAttribute; Windows 10 ignores the colours and corners and keeps dark mode).
- No white flash: the window paints the page's background colour before the page loads.
- Size and position: saved to the data folder when the window closes, used next time if still on a screen.
Everything here is best effort: a failure leaves the default window, never stops the app.
"""
from __future__ import annotations

import ctypes
import json
import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)

BACKGROUND = "#0b0c0e"  # web/src/app.css --bg
CAPTION = "#08090b"  # the left rail (web/src/app.css --rail)
BORDER = "#2a2d34"  # --hairline
DEFAULT_SIZE = (1600, 960)
MIN_SIZE = (1000, 700)
GEOMETRY_FILE = "window.json"

# DWMWINDOWATTRIBUTE values (dwmapi.h)
DWMWA_USE_IMMERSIVE_DARK_MODE = 20  # Windows 10 20H1 (build 19041) and later
DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19  # Windows 10 1809-1909 used this number
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_BORDER_COLOR = 34
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36
DWMWCP_ROUND = 2


def colorref(hex_color: str) -> int:
    """'#rrggbb' -> a Win32 COLORREF (0x00bbggrr)."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (b << 16) | (g << 8) | r


SWP_FLAGS = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020  # NOSIZE | NOMOVE | NOZORDER | NOACTIVATE | FRAMECHANGED
WM_NCACTIVATE = 0x0086


def style_window(hwnd: int, dwm=None, user32=None) -> bool:
    """Dark title bar in the app's colours, rounded corners. True if Windows took the dark mode (the rest is optional).

    Windows 10 has the dark title bar but not the colours or corners (those are Windows 11's), and it doesn't repaint
    a title bar that's already on screen: so this asks for a repaint of the frame and the caption."""
    if dwm is None or user32 is None:
        if sys.platform != "win32":
            return False
        dwm = dwm or ctypes.windll.dwmapi  # type: ignore[attr-defined]
        user32 = user32 or ctypes.windll.user32  # type: ignore[attr-defined]

    def setattr_(attr: int, value: int) -> bool:
        v = ctypes.c_int(value)
        return dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v)) == 0

    ok = setattr_(DWMWA_USE_IMMERSIVE_DARK_MODE, 1) or setattr_(DWMWA_USE_IMMERSIVE_DARK_MODE_OLD, 1)
    setattr_(DWMWA_CAPTION_COLOR, colorref(CAPTION))
    setattr_(DWMWA_BORDER_COLOR, colorref(BORDER))
    setattr_(DWMWA_TEXT_COLOR, colorref("#e6edf3"))
    setattr_(DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND)
    repaint_frame(hwnd, user32)
    return ok


def repaint_frame(hwnd: int, user32) -> None:
    """Make Windows redraw the title bar now (Windows 10 otherwise waits for the next focus change)."""
    user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_FLAGS)
    active = user32.GetForegroundWindow() == hwnd
    user32.SendMessageW(hwnd, WM_NCACTIVATE, 0, 0)
    user32.SendMessageW(hwnd, WM_NCACTIVATE, 1 if active else 0, 0)


# What happened the last time the window was styled, for Settings and the log: Windows build, handle, dark mode taken
LAST_RESULT: dict = {}


def windows_build() -> int | None:
    if sys.platform != "win32":
        return None
    try:
        return sys.getwindowsversion().build  # type: ignore[attr-defined]
    except AttributeError:
        return None


def apply(hwnd: int | None, when: str) -> bool:
    """Style the window and remember how it went (window_status())."""
    if hwnd is None:
        LAST_RESULT.update(when=when, handle=False, dark=False)
        return False
    dark = style_window(hwnd)
    LAST_RESULT.update(when=when, handle=True, dark=dark, build=windows_build())
    log.info("window styled (%s): dark title bar %s on Windows build %s", when, "on" if dark else "refused",
             LAST_RESULT["build"])
    return dark


def window_status() -> dict:
    build = windows_build()
    return {**LAST_RESULT, "build": build, "colours": bool(build and build >= 22000)}  # colours, corners: Windows 11


def take_over_pywebview_theme() -> bool:
    """pywebview's WinForms window sets the title bar light or dark from Windows' app theme, when it's created and on
    every system preference change (which Windows 10 sends often), undoing ours: Windows 10's default is light apps.
    Point that method at ours instead. False if pywebview's WinForms backend isn't there (not Windows, another version)."""
    try:
        from webview.platforms import winforms
    except Exception:  # noqa: BLE001 - not on Windows, or its .NET runtime can't load: nothing to take over
        return False
    form = getattr(getattr(winforms, "BrowserView", None), "BrowserForm", None)
    if form is None or not hasattr(form, "update_title_bar_theme"):
        return False

    def update_title_bar_theme(self) -> None:
        try:
            apply(int(self.Handle.ToInt64()), "pywebview theme update")
        except Exception:  # noqa: BLE001 - looks only
            log.exception("couldn't style the window")

    try:
        form.update_title_bar_theme = update_title_bar_theme
    except Exception:  # noqa: BLE001 - a .NET-derived class may refuse it: reapply_after_preference_changes covers us
        log.warning("couldn't take over pywebview's title bar theme")
        return False
    return True


_HANDLERS: list = []  # kept alive: .NET holds only a weak hold on a Python delegate


def reapply_after_preference_changes(get_handle) -> bool:
    """Restyle after Windows' preference changes. Subscribed after pywebview's own handler, so ours runs last and wins
    even if take_over_pywebview_theme couldn't replace it."""
    try:
        from Microsoft.Win32 import SystemEvents  # pythonnet, on Windows
    except Exception:  # noqa: BLE001
        return False

    def handler(sender, args) -> None:
        try:
            apply(get_handle(), "preference change")
        except Exception:  # noqa: BLE001
            log.exception("couldn't style the window")

    SystemEvents.UserPreferenceChanged += handler
    _HANDLERS.append(handler)
    return True


def native_handle(window) -> int | None:
    """The window's handle from pywebview's WinForms form, without looking it up by title."""
    form = getattr(window, "native", None)
    handle = getattr(form, "Handle", None)
    if handle is None:
        return None
    try:
        return int(handle.ToInt64())
    except (AttributeError, TypeError, ValueError):
        try:
            return int(handle)
        except (TypeError, ValueError):
            return None


def find_window(title: str, user32=None) -> int | None:
    if user32 is None:
        if sys.platform != "win32":
            return None
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    hwnd = user32.FindWindowW(None, title)
    return int(hwnd) or None


def _on_a_screen(x: int, y: int) -> bool:
    """Whether a point is on any monitor now (a saved position from an unplugged screen isn't)."""
    if sys.platform != "win32":
        return True
    MONITOR_DEFAULTTONULL = 0

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    return bool(ctypes.windll.user32.MonitorFromPoint(POINT(x + 40, y + 20), MONITOR_DEFAULTTONULL))  # type: ignore[attr-defined]


def load_geometry(folder: Path, on_screen=_on_a_screen) -> dict:
    """create_window's width / height / x / y / maximized from last time, or the defaults."""
    out = {"width": DEFAULT_SIZE[0], "height": DEFAULT_SIZE[1]}
    try:
        saved = json.loads((folder / GEOMETRY_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    try:
        w, h = int(saved["width"]), int(saved["height"])
        if w >= MIN_SIZE[0] and h >= MIN_SIZE[1]:
            out.update(width=w, height=h)
        x, y = saved.get("x"), saved.get("y")
        if x is not None and y is not None and on_screen(int(x), int(y)):
            out.update(x=int(x), y=int(y))
        if saved.get("maximized"):
            out["maximized"] = True
    except (KeyError, TypeError, ValueError):
        pass
    return out


def save_geometry(folder: Path, width: int, height: int, x: int | None, y: int | None, maximized: bool) -> None:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / GEOMETRY_FILE).write_text(json.dumps(
            {"width": width, "height": height, "x": x, "y": y, "maximized": maximized}), encoding="utf-8")
    except OSError:
        log.warning("couldn't save the window's size and position")
