from types import SimpleNamespace

from draftassist import launcher


def test_is_running_false_on_a_free_port():
    assert launcher.is_running(1) is False


def test_already_running_just_opens_the_browser(monkeypatch):
    opened = []
    monkeypatch.setattr(launcher, "is_running", lambda port: True)
    monkeypatch.setattr(launcher.webbrowser, "open", opened.append)
    assert launcher.main(["--port", "8123"]) == 0
    assert opened == ["http://localhost:8123"]
    assert launcher.main(["--port", "8123", "--no-browser"]) == 0
    assert opened == ["http://localhost:8123"]


def test_browser_opens_only_after_startup(monkeypatch):
    opened = []
    monkeypatch.setattr(launcher.webbrowser, "open", opened.append)
    launcher.announce_when_ready(SimpleNamespace(started=True, should_exit=False), "http://localhost:1")
    assert opened == ["http://localhost:1"]
    launcher.announce_when_ready(SimpleNamespace(started=False, should_exit=True), "http://localhost:2")
    launcher.announce_when_ready(SimpleNamespace(started=True, should_exit=False), "http://localhost:3", open_browser=False)
    assert opened == ["http://localhost:1"]
