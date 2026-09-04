"""CDP harness against Alex's real, logged-in Chrome (Tier 2 of the
chrome-control skill).

Runs its own asyncio event loop on a background thread so the websocket
connection stays open across calls (one "Allow" click per run), while the
methods exposed to callers are plain synchronous calls - the scrape loop in
run.py stays plain sequential code.

Every message sent carries a fresh id; replies are correlated by that id
regardless of arrival order (`_pending`). Events (no id) are dispatched by
method name - `Target.attachedToTarget` for the handshake, `Page.loadEventFired`
for navigation, `Network.*` for passive response capture.
"""

import asyncio
import json
import re
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import structlog
import websockets
from websockets.asyncio.client import connect as ws_connect

log = structlog.get_logger(__name__)

DEVTOOLS_ACTIVE_PORT = Path(
    "~/Library/Application Support/Google/Chrome/DevToolsActivePort"
).expanduser()

HANDSHAKE_TIMEOUT = 30.0
ALLOW_HINT = "click Allow in the Chrome remote-debugging dialog"


class CdpError(RuntimeError):
    """A CDP handshake timeout, protocol error, or Runtime.evaluate exception."""


def _read_devtools_port(path: str | Path) -> tuple[str, str]:
    lines = Path(path).expanduser().read_text().splitlines()
    return lines[0].strip(), lines[1].strip()


def _url_host(url: str) -> str:
    return urlsplit(url).netloc


class Browser:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self._ws = None
        self._target_id: str | None = None
        self._session_id: str | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._attach_waiter: asyncio.Future | None = None
        self._load_waiter: asyncio.Future | None = None
        self._capture_patterns: list[str] = []
        self._captured: list[dict] = []
        self._captured_returned_upto = 0
        self._pending_responses: dict[str, dict] = {}
        self._capture_tasks: list[asyncio.Task] = []

    # -- connect / handshake --------------------------------------------

    @classmethod
    def connect(
        cls,
        devtools_port_path: str | Path = DEVTOOLS_ACTIVE_PORT,
        handshake_timeout: float = HANDSHAKE_TIMEOUT,
    ) -> "Browser":
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        browser = cls(loop)
        fut = asyncio.run_coroutine_threadsafe(browser._connect_async(devtools_port_path), loop)
        try:
            fut.result(timeout=handshake_timeout)
        except FutureTimeoutError as e:
            fut.cancel()
            if browser._ws is not None:
                closer = asyncio.run_coroutine_threadsafe(browser._ws.close(), loop)
                try:
                    closer.result(timeout=5)
                except Exception:
                    pass
            loop.call_soon_threadsafe(loop.stop)
            raise CdpError(
                f"CDP handshake did not complete within {handshake_timeout:.0f}s - {ALLOW_HINT}"
            ) from e
        browser._thread = thread
        log.info("cdp connected", target_id=browser._target_id)
        return browser

    async def _connect_async(self, devtools_port_path: str | Path) -> None:
        port, ws_path = _read_devtools_port(devtools_port_path)
        self._ws = await ws_connect(f"ws://127.0.0.1:{port}{ws_path}", max_size=None)
        self._recv_task = asyncio.ensure_future(self._recv_loop())
        await self._handshake()

    async def _handshake(self) -> None:
        self._attach_waiter = asyncio.get_running_loop().create_future()
        created = await self._send("Target.createTarget", {"url": "about:blank"})
        self._target_id = created["targetId"]
        await self._send("Target.attachToTarget", {"targetId": self._target_id, "flatten": True})
        self._session_id = await self._attach_waiter
        await self._send("Page.enable", session_id=self._session_id)

    # -- transport --------------------------------------------------------

    async def _send(self, method: str, params: dict | None = None, session_id: str | None = None):
        self._next_id += 1
        msg_id = self._next_id
        msg: dict[str, Any] = {"id": msg_id, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        await self._ws.send(json.dumps(msg))
        return await fut

    async def _recv_loop(self) -> None:
        try:
            async for raw in self._ws:
                await self._dispatch(json.loads(raw))
        except websockets.exceptions.ConnectionClosed:
            pass

    async def _dispatch(self, msg: dict) -> None:
        msg_id = msg.get("id")
        if msg_id is not None:
            fut = self._pending.pop(msg_id, None)
            if fut is not None and not fut.done():
                if "error" in msg:
                    fut.set_exception(CdpError(json.dumps(msg["error"])))
                else:
                    fut.set_result(msg.get("result"))
            return

        method = msg.get("method")
        params = msg.get("params") or {}
        session_id = msg.get("sessionId")

        if method == "Target.attachedToTarget":
            if self._attach_waiter is not None and not self._attach_waiter.done():
                self._attach_waiter.set_result(params["sessionId"])
        elif method == "Page.loadEventFired" and session_id == self._session_id:
            if self._load_waiter is not None and not self._load_waiter.done():
                self._load_waiter.set_result(None)
        elif method == "Network.responseReceived" and session_id == self._session_id:
            self._on_response_received(params)
        elif method == "Network.loadingFinished" and session_id == self._session_id:
            self._on_loading_finished(params)

    # -- navigation ---------------------------------------------------------

    def navigate(self, url: str, wait_ms: int = 10000, capture: list[str] | None = None) -> dict:
        fut = asyncio.run_coroutine_threadsafe(
            self._navigate_async(url, wait_ms, capture), self._loop
        )
        return fut.result(timeout=wait_ms / 1000 * 2 + 30)

    async def _navigate_async(self, url: str, wait_ms: int, capture: list[str] | None) -> dict:
        self._captured = []
        self._captured_returned_upto = 0
        self._capture_tasks = []
        self._pending_responses = {}
        self._capture_patterns = list(capture) if capture else []
        if self._capture_patterns:
            await self._send("Network.enable", session_id=self._session_id)

        self._load_waiter = asyncio.get_running_loop().create_future()
        start = time.monotonic()
        await self._send("Page.navigate", {"url": url}, session_id=self._session_id)
        try:
            await asyncio.wait_for(self._load_waiter, timeout=wait_ms / 1000)
        except asyncio.TimeoutError:
            pass
        load_ms = (time.monotonic() - start) * 1000

        if self._capture_patterns:
            # SPAs (LinkedIn, Instagram) fetch their real data after the load
            # event, so keep listening for the same window again.
            await asyncio.sleep(wait_ms / 1000)
            await self._settle_capture_tasks()

        return self._drain_captured(load_ms=load_ms)

    def capture_more(self, seconds: float) -> list[dict]:
        fut = asyncio.run_coroutine_threadsafe(self._capture_more_async(seconds), self._loop)
        return fut.result(timeout=seconds + 30)

    async def _capture_more_async(self, seconds: float) -> list[dict]:
        await asyncio.sleep(seconds)
        await self._settle_capture_tasks()
        return self._drain_captured()["captured"]

    async def _settle_capture_tasks(self) -> None:
        tasks, self._capture_tasks = self._capture_tasks, []
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _drain_captured(self, load_ms: float | None = None) -> dict:
        new = self._captured[self._captured_returned_upto :]
        self._captured_returned_upto = len(self._captured)
        result: dict = {"captured": new}
        if load_ms is not None:
            result["load_ms"] = load_ms
        return result

    def scroll(self, px: int) -> None:
        fut = asyncio.run_coroutine_threadsafe(self._scroll_async(px), self._loop)
        fut.result(timeout=10)

    async def _scroll_async(self, px: int) -> None:
        await self._send(
            "Input.dispatchMouseEvent",
            {"type": "mouseWheel", "x": 400, "y": 400, "deltaX": 0, "deltaY": px},
            session_id=self._session_id,
        )

    # -- passive response capture -------------------------------------------

    def _matches(self, url: str) -> bool:
        return any(re.search(pattern, url) for pattern in self._capture_patterns)

    def _on_response_received(self, params: dict) -> None:
        response = params.get("response") or {}
        url = response.get("url", "")
        if self._capture_patterns and self._matches(url):
            self._pending_responses[params["requestId"]] = {
                "url": url,
                "status": response.get("status"),
                "mimeType": response.get("mimeType", ""),
            }

    def _on_loading_finished(self, params: dict) -> None:
        request_id = params.get("requestId")
        info = self._pending_responses.pop(request_id, None)
        if info is not None:
            task = asyncio.ensure_future(self._fetch_body(request_id, info))
            self._capture_tasks.append(task)

    async def _fetch_body(self, request_id: str, info: dict) -> None:
        try:
            result = await self._send(
                "Network.getResponseBody",
                {"requestId": request_id},
                session_id=self._session_id,
            )
            body = result.get("body", "") if result else ""
        except CdpError as e:
            log.warning("response body fetch failed", host=_url_host(info["url"]), reason=str(e))
            return
        self._captured.append({**info, "body": body})

    # -- eval / close ---------------------------------------------------

    def eval(self, js: str) -> Any:
        fut = asyncio.run_coroutine_threadsafe(self._eval_async(js), self._loop)
        return fut.result(timeout=30)

    async def _eval_async(self, js: str) -> Any:
        result = await self._send(
            "Runtime.evaluate",
            {"expression": js, "returnByValue": True, "awaitPromise": True},
            session_id=self._session_id,
        )
        exception = result.get("exceptionDetails") if result else None
        if exception:
            text = exception.get("text", "")
            description = (exception.get("exception") or {}).get("description", "")
            raise CdpError(f"Runtime.evaluate failed: {text} {description}".strip())
        return (result.get("result") or {}).get("value")

    def close(self) -> None:
        try:
            fut = asyncio.run_coroutine_threadsafe(self._close_async(), self._loop)
            fut.result(timeout=10)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)

    async def _close_async(self) -> None:
        try:
            if self._target_id:
                await self._send("Target.closeTarget", {"targetId": self._target_id})
        finally:
            await self._ws.close()
