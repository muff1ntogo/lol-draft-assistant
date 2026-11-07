"""The app window on Windows 11 (window_chrome.py): title bar colours, remembered size and position, the icon."""
import ctypes
import json
import sys
from types import SimpleNamespace

from draftassist import launcher
from draftassist import window_chrome as chrome


def test_colours_become_colorrefs():
    assert chrome.colorref("#090c10") == 0x00100C09
    assert chrome.colorref("ff0000") == 0x000000FF


class FakeDwm:
    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)

    def DwmSetWindowAttribute(self, hwnd, attr, value, size):
        self.calls.append((hwnd, attr, ctypes.cast(value, ctypes.POINTER(ctypes.c_int)).contents.value, size))
        return 1 if attr in self.fail else 0


class FakeUser32:
    def __init__(self, foreground=42):
        self.calls, self.foreground = [], foreground

    def SetWindowPos(self, *args):
        self.calls.append(("pos", *args))
        return 1

    def GetForegroundWindow(self):
        return self.foreground

    def SendMessageW(self, *args):
        self.calls.append(("msg", *args))
        return 0


def test_the_title_bar_is_dark_in_the_apps_colours_with_round_corners():
    dwm = FakeDwm()
    assert chrome.style_window(42, dwm, FakeUser32()) is True
    attrs = {attr: value for _, attr, value, _ in dwm.calls}
    assert attrs[chrome.DWMWA_USE_IMMERSIVE_DARK_MODE] == 1
    assert attrs[chrome.DWMWA_CAPTION_COLOR] == chrome.colorref(chrome.CAPTION)
    assert attrs[chrome.DWMWA_BORDER_COLOR] == chrome.colorref(chrome.BORDER)
    assert attrs[chrome.DWMWA_WINDOW_CORNER_PREFERENCE] == chrome.DWMWCP_ROUND
    assert all(hwnd == 42 for hwnd, *_ in dwm.calls)


def test_windows_10_ignoring_the_colours_still_counts_dark_mode():
    dwm = FakeDwm(fail={chrome.DWMWA_CAPTION_COLOR, chrome.DWMWA_BORDER_COLOR, chrome.DWMWA_WINDOW_CORNER_PREFERENCE})
    assert chrome.style_window(42, dwm, FakeUser32()) is True


def test_older_windows_10_gets_dark_mode_by_its_old_number():
    dwm = FakeDwm(fail={chrome.DWMWA_USE_IMMERSIVE_DARK_MODE})  # 1809-1909 don't know 20
    assert chrome.style_window(42, dwm, FakeUser32()) is True
    assert (42, chrome.DWMWA_USE_IMMERSIVE_DARK_MODE_OLD, 1, 4) in dwm.calls


def test_the_title_bar_is_repainted_at_once_keeping_its_focus():
    user32 = FakeUser32(foreground=42)
    chrome.style_window(42, FakeDwm(), user32)
    assert user32.calls[0] == ("pos", 42, 0, 0, 0, 0, 0, chrome.SWP_FLAGS)
    assert user32.calls[1:] == [("msg", 42, chrome.WM_NCACTIVATE, 0, 0), ("msg", 42, chrome.WM_NCACTIVATE, 1, 0)]
    background = FakeUser32(foreground=99)
    chrome.style_window(42, FakeDwm(), background)
    assert background.calls[-1] == ("msg", 42, chrome.WM_NCACTIVATE, 0, 0)  # not in front: stays inactive


def test_the_handle_comes_from_pywebviews_form():
    class IntPtr:
        def ToInt64(self):
            return 1234

    assert chrome.native_handle(SimpleNamespace(native=SimpleNamespace(Handle=IntPtr()))) == 1234
    assert chrome.native_handle(SimpleNamespace(native=SimpleNamespace(Handle=55))) == 55
    assert chrome.native_handle(SimpleNamespace(native=None)) is None
    assert chrome.native_handle(SimpleNamespace()) is None


def test_the_caption_matches_the_pages_nav_bar():
    from draftassist.config import PROJECT_ROOT

    css = (PROJECT_ROOT / "web" / "src" / "app.css").read_text(encoding="utf-8")
    assert f"--nav: {chrome.CAPTION}" in css and f"--bg: {chrome.BACKGROUND}" in css


def test_off_windows_nothing_is_touched():
    if sys.platform != "win32":
        assert chrome.style_window(42) is False and chrome.find_window("Draft Assistant") is None


def test_find_window():
    user32 = SimpleNamespace(FindWindowW=lambda cls, title: 77 if title == "Draft Assistant" else 0)
    assert chrome.find_window("Draft Assistant", user32) == 77
    assert chrome.find_window("Other", user32) is None


# ---------- size and position ----------
def test_defaults_without_a_saved_window(tmp_path):
    assert chrome.load_geometry(tmp_path) == {"width": 1600, "height": 960}


def test_size_and_position_come_back(tmp_path):
    chrome.save_geometry(tmp_path, 1400, 900, 120, 80, False)
    assert chrome.load_geometry(tmp_path, on_screen=lambda x, y: True) == {"width": 1400, "height": 900, "x": 120, "y": 80}


def test_a_position_on_an_unplugged_screen_is_dropped(tmp_path):
    chrome.save_geometry(tmp_path, 1400, 900, 4000, 80, True)
    got = chrome.load_geometry(tmp_path, on_screen=lambda x, y: False)
    assert got == {"width": 1400, "height": 900, "maximized": True}


def test_too_small_or_broken_files_fall_back(tmp_path):
    chrome.save_geometry(tmp_path, 300, 200, None, None, False)
    assert chrome.load_geometry(tmp_path) == {"width": 1600, "height": 960}
    (tmp_path / chrome.GEOMETRY_FILE).write_text("not json")
    assert chrome.load_geometry(tmp_path) == {"width": 1600, "height": 960}
    (tmp_path / chrome.GEOMETRY_FILE).write_text(json.dumps({"width": "x"}))
    assert chrome.load_geometry(tmp_path) == {"width": 1600, "height": 960}


# ---------- the window ----------
class Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, fn):
        self.handlers.append(fn)
        return self

    def fire(self):
        for fn in self.handlers:
            fn()


def test_the_window_opens_dark_with_the_icon_and_saves_its_size(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "DATA_ROOT", tmp_path)
    chrome.save_geometry(tmp_path, 1300, 800, None, None, True)
    made, started, styled = {}, {}, []
    window = SimpleNamespace(width=1500, height=850, x=10, y=20,
                             events=SimpleNamespace(shown=Event(), closing=Event(), maximized=Event(), restored=Event()))

    def create_window(title, url, **kw):
        made.update(kw, title=title)
        return window

    def start(**kw):
        started.update(kw)
        window.events.shown.fire()
        window.events.restored.fire()
        window.events.closing.fire()

    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(create_window=create_window, start=start))
    monkeypatch.setattr(chrome, "find_window", lambda title: 5)
    monkeypatch.setattr(chrome, "style_window", lambda hwnd: styled.append(hwnd))
    assert launcher.open_window("http://localhost:1") is True
    assert made["background_color"] == chrome.BACKGROUND and made["maximized"] is True
    assert (made["width"], made["height"]) == (1300, 800) and made["min_size"] == chrome.MIN_SIZE
    assert started["icon"].endswith("app.ico") and styled == [5]
    saved = json.loads((tmp_path / chrome.GEOMETRY_FILE).read_text())
    assert saved == {"width": 1500, "height": 850, "x": 10, "y": 20, "maximized": False}  # restored before closing


def test_the_icon_is_in_the_checkout_and_bundled():
    from draftassist.config import PROJECT_ROOT

    assert launcher.app_icon() == PROJECT_ROOT / "packaging" / "app.ico"
    spec = (PROJECT_ROOT / "packaging" / "DraftAssistant.spec").read_text(encoding="utf-8")
    assert '"packaging" / "app.ico", "."' in spec
