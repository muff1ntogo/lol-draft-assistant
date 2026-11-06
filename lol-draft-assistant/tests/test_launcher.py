import io
import sys
import threading
import time
from types import SimpleNamespace

from draftassist import launcher


def test_is_running_false_on_a_free_port():
    assert launcher.is_running(1) is False


class Opened:
    """Records what the launcher opened: the app window or the browser."""

    def __init__(self, monkeypatch, window_ok=True):
        self.windows, self.browser = [], []
        self.window_ok = window_ok

        def window(url, **_):
            self.windows.append(url)
            return self.window_ok

        monkeypatch.setattr(launcher, "open_window", window)
        monkeypatch.setattr(launcher.webbrowser, "open", self.browser.append)


# ---------- already running ----------
def test_already_running_opens_the_app_window(monkeypatch):
    opened = Opened(monkeypatch)
    monkeypatch.setattr(launcher, "is_running", lambda port: True)
    assert launcher.main(["--port", "8123"]) == 0
    assert opened.windows == ["http://localhost:8123"] and opened.browser == []


def test_already_running_with_browser_flag_opens_the_browser(monkeypatch):
    opened = Opened(monkeypatch)
    monkeypatch.setattr(launcher, "is_running", lambda port: True)
    assert launcher.main(["--port", "8123", "--browser"]) == 0
    assert opened.browser == ["http://localhost:8123"] and opened.windows == []
    assert launcher.main(["--port", "8123", "--no-browser"]) == 0
    assert opened.browser == ["http://localhost:8123"] and opened.windows == []


def test_without_a_window_it_falls_back_to_the_browser(monkeypatch):
    opened = Opened(monkeypatch, window_ok=False)
    monkeypatch.setattr(launcher, "is_running", lambda port: True)
    assert launcher.main(["--port", "8123"]) == 0
    assert opened.windows == ["http://localhost:8123"] and opened.browser == ["http://localhost:8123"]


# ---------- starting the server ----------
class FakeServer:
    """Stands in for uvicorn.Server: starts at once, runs until asked to exit."""

    def __init__(self):
        self.started = False
        self.should_exit = False
        self.thread = None

    def run(self):
        self.thread = threading.current_thread()
        self.started = True
        while not self.should_exit:
            time.sleep(0.01)


def test_closing_the_window_stops_the_server(monkeypatch):
    server = FakeServer()
    monkeypatch.setattr(launcher, "is_running", lambda port: False)
    monkeypatch.setattr(launcher, "make_server", lambda port: server)
    seen = {}

    def window(url, **_):
        seen["url"], seen["running"] = url, server.started and not server.should_exit
        return True  # the user closed the window

    monkeypatch.setattr(launcher, "open_window", window)
    assert launcher.main(["--port", "8124"]) == 0
    assert seen == {"url": "http://localhost:8124", "running": True}  # the window opens once the server is up
    assert server.should_exit is True
    assert server.thread is not threading.main_thread()  # the window needs the main thread
    assert not server.thread.is_alive()


def test_browser_mode_keeps_the_server_in_the_foreground(monkeypatch):
    server = FakeServer()
    opened = Opened(monkeypatch)
    monkeypatch.setattr(launcher, "is_running", lambda port: False)
    monkeypatch.setattr(launcher, "make_server", lambda port: server)
    threading.Timer(0.3, lambda: setattr(server, "should_exit", True)).start()  # stands in for closing the console
    assert launcher.main(["--port", "8125", "--browser"]) == 0
    assert opened.browser == ["http://localhost:8125"] and opened.windows == []
    assert server.thread is threading.main_thread()


def test_a_server_that_never_starts_fails(monkeypatch):
    server = SimpleNamespace(started=False, should_exit=True, run=lambda: None)
    Opened(monkeypatch)
    monkeypatch.setattr(launcher, "is_running", lambda port: False)
    monkeypatch.setattr(launcher, "make_server", lambda port: server)
    monkeypatch.setattr(launcher, "STARTUP_TIMEOUT_S", 0.2)
    assert launcher.main(["--port", "8126"]) == 1


def test_the_real_server_answers_while_the_window_is_open_and_stops_after(monkeypatch, tmp_path):
    import httpx

    monkeypatch.setenv("DRAFTASSIST_DB", str(tmp_path / "scratch.sqlite"))
    monkeypatch.setenv("DRAFTASSIST_LCU", "off")
    monkeypatch.setenv("DRAFTASSIST_STATS_REFRESH_S", "0")
    port = 8131
    seen = {}

    def window(url, **_):
        seen["status"] = httpx.get(f"{url}/api/live", timeout=5).status_code
        return True

    monkeypatch.setattr(launcher, "open_window", window)
    assert launcher.main(["--port", str(port)]) == 0
    assert seen["status"] == 200
    assert launcher.is_running(port) is False  # closing the window stopped it


# ---------- the window itself ----------
def test_open_window_is_false_without_pywebview(monkeypatch):
    monkeypatch.setitem(sys.modules, "webview", None)  # import fails
    assert launcher.open_window("http://localhost:1") is False


def test_open_window_is_false_when_the_webview_cannot_start(monkeypatch):
    def fail():
        raise RuntimeError("WebView2 runtime not found")

    fake = SimpleNamespace(create_window=lambda *a, **k: None, start=fail)
    monkeypatch.setitem(sys.modules, "webview", fake)
    assert launcher.open_window("http://localhost:1") is False


def test_open_window_names_the_window_and_blocks_until_closed(monkeypatch):
    calls = []
    fake = SimpleNamespace(create_window=lambda title, url, **k: calls.append((title, url, k)), start=lambda **k: calls.append("start"))
    monkeypatch.setitem(sys.modules, "webview", fake)
    assert launcher.open_window("http://localhost:1") is True
    assert calls[0][:2] == ("Draft Assistant", "http://localhost:1") and calls[-1] == "start"
    assert calls[0][2]["min_size"] == (1000, 700)


# ---------- started without a console ----------
def test_windowless_start_writes_output_to_a_log_file(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    path = launcher.log_to_file_if_windowless(tmp_path)
    assert path == tmp_path / "launcher.log"
    print("hello")
    sys.stdout.flush()
    assert "hello" in path.read_text(encoding="utf-8")


def test_console_start_keeps_the_console(monkeypatch, tmp_path):
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", out)
    assert launcher.log_to_file_if_windowless(tmp_path) is None
    assert sys.stdout is out


def test_browser_opens_only_after_startup(monkeypatch):
    opened = []
    monkeypatch.setattr(launcher.webbrowser, "open", opened.append)
    launcher.announce_when_ready(SimpleNamespace(started=True, should_exit=False), "http://localhost:1")
    assert opened == ["http://localhost:1"]
    launcher.announce_when_ready(SimpleNamespace(started=False, should_exit=True), "http://localhost:2")
    launcher.announce_when_ready(SimpleNamespace(started=True, should_exit=False), "http://localhost:3", open_browser=False)
    assert opened == ["http://localhost:1"]
