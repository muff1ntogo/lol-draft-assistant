"""Double-click launcher: start the app and open it in its own window.

Installed as `draftassist.exe` (with a console) and `draftassist-app.exe` (no console; see pyproject.toml). The window is
a native one (pywebview, on Windows' WebView2): closing it stops the app. Without WebView2 it falls back to the browser.
If the app is already running, it just opens another window.

    draftassist                # start + open the app window
    draftassist --browser      # start + open the default browser instead (closing the console stops it)
    draftassist --demo         # simulated drafts, no League client needed
    draftassist --no-browser   # start only
    draftassist --replay fixtures/lcu/flex-ranked-sample   # replay a recorded champ select
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

import httpx

from draftassist.config import DATA_ROOT, RESOURCE_ROOT

HOST = "127.0.0.1"
TITLE = "Draft Assistant"
STARTUP_TIMEOUT_S = 60.0
log = logging.getLogger(__name__)


def app_url(port: int) -> str:
    return f"http://localhost:{port}"


def is_running(port: int) -> bool:
    """True if a Draft Assistant (not just anything) is already answering on the port."""
    try:
        resp = httpx.get(f"http://{HOST}:{port}/api/live", timeout=1.0)
        return resp.status_code == 200 and "status" in resp.json()
    except (httpx.HTTPError, ValueError):
        return False


def wait_started(server, timeout: float | None = None) -> bool:
    deadline = time.monotonic() + (STARTUP_TIMEOUT_S if timeout is None else timeout)
    while not server.started and not server.should_exit and time.monotonic() < deadline:
        time.sleep(0.1)
    return bool(server.started)


def announce_when_ready(server, url: str, open_browser: bool = True, timeout: float = 60.0) -> None:
    """Browser mode: wait for uvicorn to finish startup, then say so and open the browser (on a helper thread)."""
    if wait_started(server, timeout):
        print(f"\n  Draft Assistant is running at {url}\n  Close this window to stop it.\n", flush=True)
        if open_browser:
            webbrowser.open(url)


def open_window(url: str) -> bool:
    """Show the app in a native window and block until it's closed. False if no window can be shown: pywebview missing,
    or no WebView2 runtime (it ships with Windows 10 and 11, but can be removed)."""
    try:
        import webview
    except ImportError:
        return False
    try:
        webview.create_window(TITLE, url, width=1600, height=960, min_size=(1000, 700))
        webview.start()
    except Exception:  # noqa: BLE001 - any failure to show a window means: use the browser
        log.exception("could not open the app window")
        return False
    return True


def log_to_file_if_windowless(folder: Path) -> Path | None:
    """Started without a console (draftassist-app.exe): stdout and stderr are None, so write them to a file instead."""
    if sys.stdout is not None and sys.stderr is not None:
        return None
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "launcher.log"
    stream = open(path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 - lives as long as the process
    sys.stdout = sys.stderr = stream
    return path


def make_server(port: int):
    import uvicorn  # after the env vars, so the app reads demo mode at startup

    return uvicorn.Server(uvicorn.Config("draftassist.api.main:app", host=HOST, port=port, log_level="warning"))


def main(argv: list[str] | None = None) -> int:
    from draftassist import commands

    argv = sys.argv[1:] if argv is None else argv
    if commands.is_command(argv):  # a batch job, e.g. `draftassist crawl --hours 8`
        return commands.run(argv)
    parser = argparse.ArgumentParser(description="Start the Draft Assistant and open it in its own window",
                                     epilog=commands.usage(), formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--demo", action="store_true", help="simulated drafts instead of the League client")
    parser.add_argument("--browser", action="store_true", help="open the default browser instead of the app window")
    parser.add_argument("--no-browser", action="store_true", help="start the server only, no window or browser")
    parser.add_argument("--replay", metavar="FOLDER", help="replay a recorded champ select instead of the client")
    args = parser.parse_args(argv)
    url = app_url(args.port)
    log_to_file_if_windowless(DATA_ROOT / "cache")

    if is_running(args.port):
        print(f"Draft Assistant is already running at {url}")
        if args.no_browser:
            return 0
        if args.browser or not open_window(url):
            webbrowser.open(url)
        return 0

    if not (RESOURCE_ROOT / "web" / "dist" / "index.html").is_file():
        print("Note: the web UI isn't built yet. In the web folder run: npm install && npm run build")
    if args.demo:
        os.environ["DRAFTASSIST_LCU"] = "demo"
    if args.replay:
        os.environ["DRAFTASSIST_LCU"] = "replay"
        os.environ["DRAFTASSIST_REPLAY"] = os.path.abspath(args.replay)

    server = make_server(args.port)
    print(f"Starting Draft Assistant on {url} ...", flush=True)
    if args.browser or args.no_browser:
        threading.Thread(target=announce_when_ready, args=(server, url, not args.no_browser), daemon=True).start()
        server.run()
        return 0 if server.started else 1

    # The window needs the main thread (on Windows and macOS), so the server runs beside it
    thread = threading.Thread(target=server.run, name="server", daemon=True)
    thread.start()
    if not wait_started(server):
        server.should_exit = True
        print("Draft Assistant didn't start (is something else using the port?)", flush=True)
        return 1
    if open_window(url):  # blocks until the window is closed
        server.should_exit = True
        thread.join(timeout=10)
        return 0
    print("No app window available (it needs Microsoft Edge WebView2): opening the browser. Close this window to stop.",
          flush=True)
    webbrowser.open(url)
    try:
        while thread.is_alive():  # a bare join() can't be interrupted with Ctrl+C on Windows
            thread.join(0.5)
    except KeyboardInterrupt:
        server.should_exit = True
        thread.join(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
