"""Read-only League client connector (lcu-driver), running in its own thread and event loop.

HARD RULE: this app never writes to the League client. All requests go through ReadOnlyLcu,
which only exposes GET; events come from websocket subscriptions. No hover, pick, lock or accept.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

SESSION = "/lol-champ-select/v1/session"
GAMEFLOW_PHASE = "/lol-gameflow/v1/gameflow-phase"
GAMEFLOW_SESSION = "/lol-gameflow/v1/session"
CURRENT_SUMMONER = "/lol-summoner/v1/current-summoner"


class ReadOnlyLcu:
    """The only handle on the client the app ever holds. GET, nothing else."""

    def __init__(self, connection: Any):
        self._connection = connection

    async def get(self, endpoint: str) -> Any:
        resp = await self._connection.request("get", endpoint)
        if resp.status != 200:
            return None
        return await resp.json()


class LcuConnector:
    """Calls on_session(session | None) for every champ select change (None = champ select ended),
    on_phase(phase, gameflow_session) for gameflow changes, and on_status(text) for connection state.
    Callbacks run on the connector thread."""

    def __init__(self, on_session: Callable[[dict | None], None], on_phase: Callable[[str, dict | None], None],
                 on_status: Callable[[str], None]):
        self.on_session, self.on_phase, self.on_status = on_session, on_phase, on_status
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="lcu-connector", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        from lcu_driver import Connector  # imported here so tests and demo mode don't need a client

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        connector = Connector(loop=loop)
        self.on_status("waiting for League client")

        @connector.ready
        async def ready(connection):
            lcu = ReadOnlyLcu(connection)
            self.on_status("connected")
            phase = await lcu.get(GAMEFLOW_PHASE)
            self.on_phase(phase or "None", await lcu.get(GAMEFLOW_SESSION))
            if phase == "ChampSelect":
                self.on_session(await lcu.get(SESSION))

        @connector.ws.register(SESSION, event_types=("CREATE", "UPDATE", "DELETE"))
        async def champ_select(connection, event):
            self.on_session(None if event.type.upper() == "DELETE" else event.data)

        @connector.ws.register(GAMEFLOW_PHASE, event_types=("UPDATE",))
        async def gameflow(connection, event):
            self.on_phase(event.data, await ReadOnlyLcu(connection).get(GAMEFLOW_SESSION))

        @connector.close
        async def closed(connection):
            self.on_status("League client closed")
            self.on_session(None)

        try:
            connector.start()
        except Exception:  # never take the app down with the client
            log.exception("LCU connector stopped")
            self.on_status("connector error (see log)")
