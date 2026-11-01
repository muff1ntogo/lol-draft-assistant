"""Double-click launcher: start the app and open it in the default browser.

Installed as `draftassist.exe` (console script, see pyproject.toml). Closing the window stops it.
If the app is already running, it just opens the browser.

    draftassist                # start + open browser
    draftassist --demo         # simulated drafts, no League client needed
    draftassist --no-browser   # start only
    draftassist --replay fixtures/lcu/flex-ranked-sample   # replay a recorded champ select
"""
from __future__ import annotations

import argparse
import os
import threading
import time
import webbrowser

import httpx

from draftassist.config import PROJECT_ROOT

HOST = "127.0.0.1"


def app_url(port: int) -> str:
    return f"http://localhost:{port}"


def is_running(port: int) -> bool:
    """True if a Draft Assistant (not just anything) is already answering on the port."""
    try:
        resp = httpx.get(f"http://{HOST}:{port}/api/live", timeout=1.0)
        return resp.status_code == 200 and "status" in resp.json()
    except (httpx.HTTPError, ValueError):
        return False


def announce_when_ready(server, url: str, open_browser: bool = True, timeout: float = 60.0) -> None:
    """Wait for uvicorn to finish startup, then say so and open the browser (on a helper thread)."""
    deadline = time.monotonic() + timeout
    while not server.started and not server.should_exit and time.monotonic() < deadline:
        time.sleep(0.1)
    if server.started:
        print(f"\n  Draft Assistant is running at {url}\n  Close this window to stop it.\n", flush=True)
        if open_browser:
            webbrowser.open(url)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start the Draft Assistant and open it in the browser")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--demo", action="store_true", help="simulated drafts instead of the League client")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--replay", metavar="FOLDER", help="replay a recorded champ select instead of the client")
    args = parser.parse_args(argv)
    url = app_url(args.port)

    if is_running(args.port):
        print(f"Draft Assistant is already running at {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return 0

    if not (PROJECT_ROOT / "web" / "dist" / "index.html").is_file():
        print("Note: the web UI isn't built yet. In the web folder run: npm install && npm run build")
    if args.demo:
        os.environ["DRAFTASSIST_LCU"] = "demo"
    if args.replay:
        os.environ["DRAFTASSIST_LCU"] = "replay"
        os.environ["DRAFTASSIST_REPLAY"] = os.path.abspath(args.replay)

    import uvicorn  # after the env var, so the app reads demo mode at startup

    server = uvicorn.Server(uvicorn.Config("draftassist.api.main:app", host=HOST, port=args.port, log_level="warning"))
    threading.Thread(target=announce_when_ready, args=(server, url, not args.no_browser), daemon=True).start()
    print(f"Starting Draft Assistant on {url} ...", flush=True)
    server.run()
    return 0 if server.started else 1


if __name__ == "__main__":
    raise SystemExit(main())
