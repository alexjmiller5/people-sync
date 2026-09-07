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
import base64
import json
import os
import random
import re
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import structlog
import websockets
from websockets.asyncio.client import connect as ws_connect

log = structlog.get_logger(__name__)

# Indirected so tests can drive the clock (typing jitter, wait_for polling)
# without patching the `time` module out from under the asyncio loop.
_sleep = time.sleep
_now = time.monotonic

DEVTOOLS_ACTIVE_PORT = Path(
    "~/Library/Application Support/Google/Chrome/DevToolsActivePort"
).expanduser()

# Endpoint resolution (R1): explicit endpoint (arg or env) > explicit data
# dir (arg or env) > Chrome's default data dir above.
CDP_ENDPOINT_ENV = "CONTACT_SYNC_CDP_ENDPOINT"
CHROME_DATA_DIR_ENV = "CONTACT_SYNC_CHROME_DATA_DIR"
JSON_VERSION_TIMEOUT = 5.0

HANDSHAKE_TIMEOUT = 30.0
ALLOW_HINT = "click Allow in the Chrome remote-debugging dialog"

# Trusted input (Input.dispatchKeyEvent / Input.dispatchMouseEvent - the
# chrome-control skill's "trusted" tier: the page sees isTrusted events,
# unlike anything dispatched from Runtime.evaluate).
TYPE_JITTER_MS = (80, 200)
CLICK_JITTER_PX = 3
WAIT_POLL_S = 0.5

RECT_JS = (
    "(function(){{var e=document.querySelector({selector});if(!e)return null;"
    'e.scrollIntoView({{block:"center",inline:"center"}});'
    "var r=e.getBoundingClientRect();"
    "return {{x:r.x,y:r.y,width:r.width,height:r.height}};}})()"
)

# A single Network.getResponseBody must never hang navigate() forever - a
# request whose body Chrome never returns (evicted, aborted, redirected) is
# logged and skipped rather than blocking the whole capture window.
RESPONSE_BODY_TIMEOUT = 10.0


class CdpError(RuntimeError):
    """A CDP handshake timeout, protocol error, or Runtime.evaluate exception."""


def _read_devtools_port(path: str | Path) -> tuple[str, str]:
    lines = Path(path).expanduser().read_text().splitlines()
    return lines[0].strip(), lines[1].strip()


def _url_host(url: str) -> str:
    return urlsplit(url).netloc


def _ws_url_from_data_dir(data_dir: str | Path) -> str:
    port, ws_path = _read_devtools_port(Path(data_dir).expanduser() / "DevToolsActivePort")
    return f"ws://127.0.0.1:{port}{ws_path}"


def _no_data_dir_message(endpoint: str, reason: str) -> str:
    return (
        f"{reason} and no data dir was given to fall back to DevToolsActivePort - "
        "pass data_dir or set CONTACT_SYNC_CHROME_DATA_DIR, or point endpoint at a "
        "Chrome started with a dedicated --user-data-dir (no approval dialog, "
        "/json/version works there)"
    )


def _ws_url_from_endpoint(endpoint: str, data_dir: str | Path | None) -> str:
    try:
        resp = httpx.get(f"http://{endpoint}/json/version", timeout=JSON_VERSION_TIMEOUT)
    except httpx.HTTPError as e:
        raise CdpError(
            _no_data_dir_message(endpoint, f"http://{endpoint}/json/version was unreachable ({e})")
        ) from e

    if resp.status_code == 404:
        # Approval-mode Chrome: no /json/version. Fall back to the
        # DevToolsActivePort file only when a data dir is also known.
        if data_dir:
            log.info("cdp endpoint resolved", via="endpoint-404-data-dir-fallback")
            return _ws_url_from_data_dir(data_dir)
        raise CdpError(
            _no_data_dir_message(
                endpoint,
                f"http://{endpoint}/json/version returned 404 (Chrome is running in approval mode)",
            )
        )
    resp.raise_for_status()
    ws_url = resp.json().get("webSocketDebuggerUrl")
    if not ws_url:
        raise CdpError(f"no webSocketDebuggerUrl in http://{endpoint}/json/version response")
    return ws_url


def _resolve_ws_url(
    endpoint: str | None,
    data_dir: str | Path | None,
    devtools_port_path: str | Path | None,
) -> str:
    endpoint = endpoint or os.environ.get(CDP_ENDPOINT_ENV)
    if endpoint:
        log.info("cdp endpoint resolved", via="endpoint", host_port=endpoint)
        return _ws_url_from_endpoint(endpoint, data_dir or os.environ.get(CHROME_DATA_DIR_ENV))

    data_dir = data_dir or os.environ.get(CHROME_DATA_DIR_ENV)
    if data_dir:
        log.info("cdp endpoint resolved", via="data_dir")
        return _ws_url_from_data_dir(data_dir)

    log.info("cdp endpoint resolved", via="default")
    port, ws_path = _read_devtools_port(devtools_port_path or DEVTOOLS_ACTIVE_PORT)
    return f"ws://127.0.0.1:{port}{ws_path}"


def _key_events(char: str) -> list[dict]:
    """The keyDown/char/keyUp triple Chrome expects for one printable
    character - `text` is what actually lands in the field, `key` is what a
    site's keydown handlers read."""
    code = ord(char.upper()) if char.isalnum() else 0
    return [
        {
            "type": "keyDown",
            "text": char,
            "unmodifiedText": char,
            "key": char,
            "windowsVirtualKeyCode": code,
            "nativeVirtualKeyCode": code,
        },
        {"type": "char", "text": char, "unmodifiedText": char, "key": char},
        {
            "type": "keyUp",
            "key": char,
            "windowsVirtualKeyCode": code,
            "nativeVirtualKeyCode": code,
        },
    ]


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
        endpoint: str | None = None,
        data_dir: str | Path | None = None,
        handshake_timeout: float = HANDSHAKE_TIMEOUT,
        devtools_port_path: str | Path | None = None,
    ) -> "Browser":
        ws_url = _resolve_ws_url(endpoint, data_dir, devtools_port_path)
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        browser = cls(loop)
        fut = asyncio.run_coroutine_threadsafe(browser._connect_async(ws_url), loop)
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

    async def _connect_async(self, ws_url: str) -> None:
        self._ws = await ws_connect(ws_url, max_size=None)
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
        """Navigate and wait for Page.loadEventFired (or wait_ms, non-fatal -
        SPAs like Facebook's /friends_all never fire it; the DOM is usually
        already usable, so navigate() proceeds and reports `load_event: False`
        instead of raising). Only a failed Page.navigate RPC itself (bad URL,
        target gone) raises `CdpError`.

        `capture` is a list of regexes (matched with `re.search` against the
        response URL) - a plain substring works unchanged, but a literal URL
        containing regex metacharacters (`?`, `.`, `+`, ...) should be passed
        through `re.escape` first.
        """
        fut = asyncio.run_coroutine_threadsafe(
            self._navigate_async(url, wait_ms, capture), self._loop
        )
        return fut.result(timeout=wait_ms / 1000 * 2 + 30)

    async def _navigate_async(self, url: str, wait_ms: int, capture: list[str] | None) -> dict:
        # Clean up any stragglers left pending by a previous navigate/capture
        # window before starting a new one.
        await self._cancel_capture_tasks()
        self._captured = []
        self._captured_returned_upto = 0
        self._pending_responses = {}
        self._capture_patterns = list(capture) if capture else []
        try:
            if self._capture_patterns:
                await self._send("Network.enable", session_id=self._session_id)

            self._load_waiter = asyncio.get_running_loop().create_future()
            start = time.monotonic()
            await self._send("Page.navigate", {"url": url}, session_id=self._session_id)
            load_event = True
            try:
                await asyncio.wait_for(self._load_waiter, timeout=wait_ms / 1000)
            except asyncio.TimeoutError:
                load_event = False
            load_ms = (time.monotonic() - start) * 1000

            if self._capture_patterns:
                # SPAs (LinkedIn, Instagram) fetch their real data after the
                # load event, so keep listening for the same window again.
                # Each fetch is individually bounded (RESPONSE_BODY_TIMEOUT),
                # so this can't hang past that regardless of load_event.
                await asyncio.sleep(wait_ms / 1000)
                await self._settle_capture_tasks()

            return self._drain_captured(load_ms=load_ms, load_event=load_event)
        except BaseException:
            # A Page.navigate RPC error (or anything else) still leaves
            # nothing pending behind for the loop to destroy later.
            await self._cancel_capture_tasks()
            raise

    def capture_more(self, seconds: float) -> list[dict]:
        fut = asyncio.run_coroutine_threadsafe(self._capture_more_async(seconds), self._loop)
        return fut.result(timeout=seconds + 30)

    async def _capture_more_async(self, seconds: float) -> list[dict]:
        await asyncio.sleep(seconds)
        await self._settle_capture_tasks()
        return self._drain_captured()["captured"]

    async def _settle_capture_tasks(self) -> None:
        """Wait for in-flight fetches to finish naturally. Each one is
        individually bounded by RESPONSE_BODY_TIMEOUT (see `_fetch_body`),
        so this can't hang - it's not a cancellation path."""
        tasks, self._capture_tasks = self._capture_tasks, []
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _cancel_capture_tasks(self) -> None:
        """Force-cancel and await any still-pending fetch tasks, so none are
        left for the event loop to destroy while pending (on an exception
        path, a fresh navigate(), or close())."""
        tasks, self._capture_tasks = self._capture_tasks, []
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _drain_captured(self, load_ms: float | None = None, load_event: bool | None = None) -> dict:
        new = self._captured[self._captured_returned_upto :]
        self._captured_returned_upto = len(self._captured)
        result: dict = {"captured": new}
        if load_ms is not None:
            result["load_ms"] = load_ms
        if load_event is not None:
            result["load_event"] = load_event
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

    # -- trusted input ------------------------------------------------------

    def type_text(self, text: str, jitter_ms: tuple[int, int] = TYPE_JITTER_MS) -> None:
        """Type into whatever has focus, one trusted keyDown/char/keyUp triple
        per character, with a human pause between characters. Click the field
        first - this does not focus anything itself."""
        for char in text:
            for params in _key_events(char):
                fut = asyncio.run_coroutine_threadsafe(
                    self._send("Input.dispatchKeyEvent", params, session_id=self._session_id),
                    self._loop,
                )
                fut.result(timeout=10)
            _sleep(random.uniform(jitter_ms[0], jitter_ms[1]) / 1000)

    def insert_text(self, text: str) -> None:
        """Paste `text` into the focused field in one shot. NOT human-like -
        no key events reach the page, which a site's input handlers can spot.
        Kept for bulk, non-login fields; the login flow never uses it."""
        fut = asyncio.run_coroutine_threadsafe(
            self._send("Input.insertText", {"text": text}, session_id=self._session_id), self._loop
        )
        fut.result(timeout=10)

    def click(self, selector: str) -> None:
        """Scroll the element into view and click its center (jittered by a
        few pixels) with trusted mouse events."""
        rect = self.eval(RECT_JS.format(selector=json.dumps(selector)))
        if not rect:
            raise CdpError(f"click target not on the page: {selector}")
        x = rect["x"] + rect["width"] / 2 + random.uniform(-CLICK_JITTER_PX, CLICK_JITTER_PX)
        y = rect["y"] + rect["height"] / 2 + random.uniform(-CLICK_JITTER_PX, CLICK_JITTER_PX)
        for event_type in ("mousePressed", "mouseReleased"):
            fut = asyncio.run_coroutine_threadsafe(
                self._send(
                    "Input.dispatchMouseEvent",
                    {
                        "type": event_type,
                        "x": x,
                        "y": y,
                        "button": "left",
                        "buttons": 1,
                        "clickCount": 1,
                    },
                    session_id=self._session_id,
                ),
                self._loop,
            )
            fut.result(timeout=10)

    def wait_for(self, js_predicate: str, timeout_s: float = 10.0) -> bool:
        """Poll `js_predicate` until it evaluates truthy. Returns False on
        timeout - callers decide whether that is a halt."""
        deadline = _now() + timeout_s
        while True:
            if self.eval(js_predicate):
                return True
            if _now() >= deadline:
                return False
            _sleep(WAIT_POLL_S)

    def screenshot(self, path) -> None:
        result = asyncio.run_coroutine_threadsafe(
            self._send("Page.captureScreenshot", session_id=self._session_id), self._loop
        ).result(timeout=30)
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode((result or {}).get("data", "")))

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
            result = await asyncio.wait_for(
                self._send(
                    "Network.getResponseBody",
                    {"requestId": request_id},
                    session_id=self._session_id,
                ),
                timeout=RESPONSE_BODY_TIMEOUT,
            )
            body = result.get("body", "") if result else ""
        except Exception as e:
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

    def text(self) -> str:
        """Title + up to 3000 chars of visible body text - what run.py's
        challenge check (`pace.is_challenge`) reads, in one eval instead of
        each caller composing its own."""
        return self.eval('document.title + "\\n" + document.body.innerText.slice(0, 3000)')

    def close(self) -> None:
        try:
            fut = asyncio.run_coroutine_threadsafe(self._close_async(), self._loop)
            fut.result(timeout=10)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)

    async def _close_async(self) -> None:
        try:
            await self._cancel_capture_tasks()
            if self._target_id:
                await self._send("Target.closeTarget", {"targetId": self._target_id})
        finally:
            await self._ws.close()
