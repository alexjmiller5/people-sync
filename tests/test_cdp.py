import asyncio
import json
import threading
import time

import pytest
from websockets.asyncio.server import serve

from contact_sync.scrape import cdp


class FakeChrome:
    """A minimal in-process CDP server. The connect handshake (createTarget /
    attachToTarget / Page.enable) is answered automatically; tests register
    extra behavior for specific methods via `on(method, handler)`.
    """

    def __init__(self):
        self.handlers: dict = {}
        self.messages: list[dict] = []
        self.connections: list = []
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        fut = asyncio.run_coroutine_threadsafe(self._start(), self._loop)
        self.port = fut.result(timeout=5)

    def on(self, method: str, handler) -> None:
        self.handlers[method] = handler

    async def _start(self) -> int:
        self._server = await serve(self._handle, "127.0.0.1", 0)
        return self._server.sockets[0].getsockname()[1]

    async def _handle(self, ws) -> None:
        self.connections.append(ws)
        async for raw in ws:
            msg = json.loads(raw)
            self.messages.append(msg)
            method = msg.get("method")
            mid = msg.get("id")
            sid = msg.get("sessionId")
            if method in self.handlers:
                await self.handlers[method](ws, msg)
            elif method == "Target.createTarget":
                await ws.send(json.dumps({"id": mid, "result": {"targetId": "T1"}}))
            elif method == "Target.attachToTarget":
                # The RPC response deliberately carries a DIFFERENT sessionId
                # than the attachedToTarget event, to prove the client takes
                # the session id from the event (per the chrome-control
                # skill), never from the call's own response.
                await ws.send(json.dumps({"id": mid, "result": {"sessionId": "WRONG-FROM-RPC"}}))
                await ws.send(
                    json.dumps({"method": "Target.attachedToTarget", "params": {"sessionId": "S1"}})
                )
            elif method == "Page.enable":
                await ws.send(json.dumps({"id": mid, "sessionId": sid, "result": {}}))
            else:
                await ws.send(json.dumps({"id": mid, "sessionId": sid, "result": {}}))

    def devtools_port_file(self, tmp_path) -> str:
        p = tmp_path / "DevToolsActivePort"
        p.write_text(f"{self.port}\n/devtools/browser/fake\n")
        return str(p)

    def stop(self) -> None:
        async def _stop():
            self._server.close()
            await self._server.wait_closed()

        asyncio.run_coroutine_threadsafe(_stop(), self._loop).result(timeout=5)
        self._loop.call_soon_threadsafe(self._loop.stop)


@pytest.fixture
def fake_chrome():
    server = FakeChrome()
    yield server
    server.stop()


def test_connect_performs_full_attach_flow(tmp_path, fake_chrome):
    browser = cdp.Browser.connect(devtools_port_path=fake_chrome.devtools_port_file(tmp_path))
    try:
        methods = [m.get("method") for m in fake_chrome.messages]
        assert methods == ["Target.createTarget", "Target.attachToTarget", "Page.enable"]
        assert browser._target_id == "T1"
        assert browser._session_id == "S1"
        # Page.enable is sent scoped to the attached session, not the browser session
        page_enable = next(m for m in fake_chrome.messages if m["method"] == "Page.enable")
        assert page_enable["sessionId"] == "S1"
    finally:
        browser.close()


def test_connect_timeout_message_says_click_allow(tmp_path, fake_chrome):
    async def hang(ws, msg):
        pass  # never reply - connect() must time out

    fake_chrome.on("Target.createTarget", hang)

    with pytest.raises(cdp.CdpError, match="Allow"):
        cdp.Browser.connect(
            devtools_port_path=fake_chrome.devtools_port_file(tmp_path), handshake_timeout=0.2
        )


def test_messages_correlate_by_id_not_arrival_order(tmp_path, fake_chrome):
    pending = []

    async def handle_evaluate(ws, msg):
        pending.append((ws, msg))
        if len(pending) == 2:
            # reply to the second request first, to prove correlation is by id
            for w, m in reversed(pending):
                value = m["params"]["expression"]
                await w.send(
                    json.dumps(
                        {
                            "id": m["id"],
                            "sessionId": m.get("sessionId"),
                            "result": {"result": {"value": value}},
                        }
                    )
                )

    fake_chrome.on("Runtime.evaluate", handle_evaluate)
    browser = cdp.Browser.connect(devtools_port_path=fake_chrome.devtools_port_file(tmp_path))
    try:
        outcomes = {}

        def call(expr):
            outcomes[expr] = browser.eval(expr)

        t1 = threading.Thread(target=call, args=("'first'",))
        t2 = threading.Thread(target=call, args=("'second'",))
        t1.start()
        time.sleep(0.1)
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert outcomes == {"'first'": "'first'", "'second'": "'second'"}
    finally:
        browser.close()


def test_eval_returns_value(tmp_path, fake_chrome):
    async def handle_evaluate(ws, msg):
        await ws.send(
            json.dumps(
                {
                    "id": msg["id"],
                    "sessionId": msg.get("sessionId"),
                    "result": {"result": {"value": 42}},
                }
            )
        )

    fake_chrome.on("Runtime.evaluate", handle_evaluate)
    browser = cdp.Browser.connect(devtools_port_path=fake_chrome.devtools_port_file(tmp_path))
    try:
        assert browser.eval("21 * 2") == 42
    finally:
        browser.close()


def test_eval_raises_clear_error_on_exception_details(tmp_path, fake_chrome):
    async def handle_evaluate(ws, msg):
        await ws.send(
            json.dumps(
                {
                    "id": msg["id"],
                    "sessionId": msg.get("sessionId"),
                    "result": {
                        "result": {"type": "undefined"},
                        "exceptionDetails": {
                            "text": "Uncaught",
                            "exception": {"description": "ReferenceError: x is not defined"},
                        },
                    },
                }
            )
        )

    fake_chrome.on("Runtime.evaluate", handle_evaluate)
    browser = cdp.Browser.connect(devtools_port_path=fake_chrome.devtools_port_file(tmp_path))
    try:
        with pytest.raises(cdp.CdpError, match="ReferenceError"):
            browser.eval("x.y")
    finally:
        browser.close()


def test_navigate_without_capture_waits_for_load_event(tmp_path, fake_chrome):
    async def handle_navigate(ws, msg):
        sid = msg.get("sessionId")
        await ws.send(json.dumps({"id": msg["id"], "sessionId": sid, "result": {}}))
        await ws.send(json.dumps({"method": "Page.loadEventFired", "sessionId": sid, "params": {}}))

    fake_chrome.on("Page.navigate", handle_navigate)
    browser = cdp.Browser.connect(devtools_port_path=fake_chrome.devtools_port_file(tmp_path))
    try:
        result = browser.navigate("https://x.test/profile", wait_ms=1000)
        assert result["captured"] == []
        assert result["load_ms"] < 1000
    finally:
        browser.close()


def test_navigate_times_out_without_load_event(tmp_path, fake_chrome):
    # Page.navigate is answered but no Page.loadEventFired ever arrives.
    browser = cdp.Browser.connect(devtools_port_path=fake_chrome.devtools_port_file(tmp_path))
    try:
        result = browser.navigate("https://x.test/profile", wait_ms=100)
        assert result["captured"] == []
        assert result["load_ms"] >= 100
    finally:
        browser.close()


def test_navigate_captures_matching_response_body(tmp_path, fake_chrome):
    async def handle_navigate(ws, msg):
        sid = msg.get("sessionId")
        await ws.send(json.dumps({"id": msg["id"], "sessionId": sid, "result": {}}))
        await ws.send(
            json.dumps(
                {
                    "method": "Network.responseReceived",
                    "sessionId": sid,
                    "params": {
                        "requestId": "R1",
                        "response": {
                            "url": "https://x.test/api/profile",
                            "status": 200,
                            "mimeType": "application/json",
                        },
                    },
                }
            )
        )
        await ws.send(
            json.dumps(
                {
                    "method": "Network.loadingFinished",
                    "sessionId": sid,
                    "params": {"requestId": "R1"},
                }
            )
        )
        await ws.send(json.dumps({"method": "Page.loadEventFired", "sessionId": sid, "params": {}}))

    async def handle_get_body(ws, msg):
        await ws.send(
            json.dumps(
                {
                    "id": msg["id"],
                    "sessionId": msg.get("sessionId"),
                    "result": {"body": '{"name": "fixture"}', "base64Encoded": False},
                }
            )
        )

    fake_chrome.on("Page.navigate", handle_navigate)
    fake_chrome.on("Network.getResponseBody", handle_get_body)

    browser = cdp.Browser.connect(devtools_port_path=fake_chrome.devtools_port_file(tmp_path))
    try:
        result = browser.navigate("https://x.test/profile", wait_ms=200, capture=["/api/"])
        assert result["captured"] == [
            {
                "url": "https://x.test/api/profile",
                "status": 200,
                "mimeType": "application/json",
                "body": '{"name": "fixture"}',
            }
        ]
        network_enable = next(m for m in fake_chrome.messages if m["method"] == "Network.enable")
        assert network_enable["sessionId"] == browser._session_id == "S1"
    finally:
        browser.close()


def test_navigate_ignores_non_matching_response(tmp_path, fake_chrome):
    async def handle_navigate(ws, msg):
        sid = msg.get("sessionId")
        await ws.send(json.dumps({"id": msg["id"], "sessionId": sid, "result": {}}))
        await ws.send(
            json.dumps(
                {
                    "method": "Network.responseReceived",
                    "sessionId": sid,
                    "params": {
                        "requestId": "R1",
                        "response": {
                            "url": "https://x.test/static/logo.png",
                            "status": 200,
                            "mimeType": "image/png",
                        },
                    },
                }
            )
        )
        await ws.send(
            json.dumps(
                {
                    "method": "Network.loadingFinished",
                    "sessionId": sid,
                    "params": {"requestId": "R1"},
                }
            )
        )
        await ws.send(json.dumps({"method": "Page.loadEventFired", "sessionId": sid, "params": {}}))

    got_body_request = False

    async def handle_get_body(ws, msg):
        nonlocal got_body_request
        got_body_request = True
        await ws.send(
            json.dumps({"id": msg["id"], "sessionId": msg.get("sessionId"), "result": {}})
        )

    fake_chrome.on("Page.navigate", handle_navigate)
    fake_chrome.on("Network.getResponseBody", handle_get_body)

    browser = cdp.Browser.connect(devtools_port_path=fake_chrome.devtools_port_file(tmp_path))
    try:
        result = browser.navigate("https://x.test/profile", wait_ms=200, capture=["/api/"])
        assert result["captured"] == []
        assert got_body_request is False
    finally:
        browser.close()


def test_get_response_body_failure_does_not_abort(tmp_path, fake_chrome, mocker):
    warn = mocker.patch.object(cdp.log, "warning")

    async def handle_navigate(ws, msg):
        sid = msg.get("sessionId")
        await ws.send(json.dumps({"id": msg["id"], "sessionId": sid, "result": {}}))
        await ws.send(
            json.dumps(
                {
                    "method": "Network.responseReceived",
                    "sessionId": sid,
                    "params": {
                        "requestId": "R1",
                        "response": {
                            "url": "https://x.test/api/profile",
                            "status": 200,
                            "mimeType": "application/json",
                        },
                    },
                }
            )
        )
        await ws.send(
            json.dumps(
                {
                    "method": "Network.loadingFinished",
                    "sessionId": sid,
                    "params": {"requestId": "R1"},
                }
            )
        )
        await ws.send(json.dumps({"method": "Page.loadEventFired", "sessionId": sid, "params": {}}))

    async def handle_get_body_fail(ws, msg):
        await ws.send(
            json.dumps(
                {
                    "id": msg["id"],
                    "sessionId": msg.get("sessionId"),
                    "error": {"message": "No resource with given identifier found"},
                }
            )
        )

    fake_chrome.on("Page.navigate", handle_navigate)
    fake_chrome.on("Network.getResponseBody", handle_get_body_fail)

    browser = cdp.Browser.connect(devtools_port_path=fake_chrome.devtools_port_file(tmp_path))
    try:
        result = browser.navigate("https://x.test/profile", wait_ms=200, capture=["/api/"])
        assert result["captured"] == []
        assert warn.call_count == 1
        _, kwargs = warn.call_args
        assert kwargs["host"] == "x.test"
        assert "reason" in kwargs
    finally:
        browser.close()


def test_fetch_body_logs_and_swallows_non_protocol_exceptions(mocker):
    # A ConnectionClosed (or any non-CdpError) raised mid-fetch - e.g. the
    # websocket drops between the request and its reply - must be logged and
    # swallowed the same way, not left to surface as an unretrieved task
    # exception from asyncio.gather.
    warn = mocker.patch.object(cdp.log, "warning")
    browser = cdp.Browser(loop=asyncio.new_event_loop())

    async def boom(*args, **kwargs):
        raise ConnectionResetError("boom")

    browser._send = boom

    asyncio.run(
        browser._fetch_body(
            "R1",
            {"url": "https://x.test/api/profile", "status": 200, "mimeType": "application/json"},
        )
    )

    assert browser._captured == []
    warn.assert_called_once()
    _, kwargs = warn.call_args
    assert kwargs["host"] == "x.test"
    assert "reason" in kwargs


def test_capture_more_continues_without_renavigating(tmp_path, fake_chrome):
    async def handle_navigate(ws, msg):
        sid = msg.get("sessionId")
        await ws.send(json.dumps({"id": msg["id"], "sessionId": sid, "result": {}}))
        await ws.send(json.dumps({"method": "Page.loadEventFired", "sessionId": sid, "params": {}}))

    async def handle_get_body(ws, msg):
        await ws.send(
            json.dumps(
                {
                    "id": msg["id"],
                    "sessionId": msg.get("sessionId"),
                    "result": {"body": '{"more": true}', "base64Encoded": False},
                }
            )
        )

    fake_chrome.on("Page.navigate", handle_navigate)
    fake_chrome.on("Network.getResponseBody", handle_get_body)

    browser = cdp.Browser.connect(devtools_port_path=fake_chrome.devtools_port_file(tmp_path))
    try:
        first = browser.navigate("https://x.test/profile", wait_ms=100, capture=["/api/"])
        assert first["captured"] == []

        # simulate a late XHR firing after a scroll, with no new navigation
        async def push_late_response():
            for ws in list(fake_chrome.connections):
                await ws.send(
                    json.dumps(
                        {
                            "method": "Network.responseReceived",
                            "sessionId": "S1",
                            "params": {
                                "requestId": "R2",
                                "response": {
                                    "url": "https://x.test/api/more",
                                    "status": 200,
                                    "mimeType": "application/json",
                                },
                            },
                        }
                    )
                )
                await ws.send(
                    json.dumps(
                        {
                            "method": "Network.loadingFinished",
                            "sessionId": "S1",
                            "params": {"requestId": "R2"},
                        }
                    )
                )

        asyncio.run_coroutine_threadsafe(push_late_response(), fake_chrome._loop).result(timeout=5)

        more = browser.capture_more(0.3)
        assert more == [
            {
                "url": "https://x.test/api/more",
                "status": 200,
                "mimeType": "application/json",
                "body": '{"more": true}',
            }
        ]
    finally:
        browser.close()


def test_scroll_sends_trusted_mouse_wheel_event(tmp_path, fake_chrome):
    browser = cdp.Browser.connect(devtools_port_path=fake_chrome.devtools_port_file(tmp_path))
    try:
        browser.scroll(600)
        wheel_events = [
            m for m in fake_chrome.messages if m.get("method") == "Input.dispatchMouseEvent"
        ]
        assert len(wheel_events) == 1
        assert wheel_events[0]["params"]["type"] == "mouseWheel"
        assert wheel_events[0]["params"]["deltaY"] == 600
    finally:
        browser.close()


def test_close_sends_close_target_and_closes_socket(tmp_path, fake_chrome):
    browser = cdp.Browser.connect(devtools_port_path=fake_chrome.devtools_port_file(tmp_path))
    browser.close()
    assert any(m.get("method") == "Target.closeTarget" for m in fake_chrome.messages)
