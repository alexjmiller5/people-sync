import asyncio
import http.server
import json
import threading
import time

import pytest
from websockets.asyncio.server import serve

from people_sync.scrape import cdp


class FakeChrome:
    """A minimal in-process CDP server. The connect handshake (createTarget /
    attachToTarget / Page.enable) is answered automatically; tests register
    extra behavior for specific methods via `on(method, handler)`.
    """

    def __init__(self, handshake_delay: float = 0.0):
        self.handshake_delay = handshake_delay  # stalls the websocket upgrade
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
        self._server = await serve(
            self._handle, "127.0.0.1", 0, process_request=self._process_request
        )
        return self._server.sockets[0].getsockname()[1]

    async def _process_request(self, connection, request):
        """Stall before the upgrade is accepted, the way a Chrome waiting on
        its "Allow remote debugging?" sheet does."""
        if self.handshake_delay:
            await asyncio.sleep(self.handshake_delay)
        return None

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


def test_timeout_finishes_transport_cancellation_before_stopping_loop(
    tmp_path, fake_chrome, monkeypatch
):
    cleaned = threading.Event()

    async def stalled_connect(*args, **kwargs):
        try:
            await asyncio.sleep(30)
        finally:
            await asyncio.sleep(0.01)
            cleaned.set()

    monkeypatch.setattr(cdp, "ws_connect", stalled_connect)
    with pytest.raises(cdp.CdpError, match="Allow"):
        cdp.Browser.connect(
            devtools_port_path=fake_chrome.devtools_port_file(tmp_path), handshake_timeout=0.1
        )
    assert cleaned.is_set()


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
        assert result["load_event"] is True
    finally:
        browser.close()


def test_navigate_times_out_without_load_event(tmp_path, fake_chrome):
    # Page.navigate is answered but no Page.loadEventFired ever arrives - a
    # single-page app (e.g. Facebook's /friends_all) that never fires it.
    # navigate() must not raise: it proceeds and reports load_event False.
    browser = cdp.Browser.connect(devtools_port_path=fake_chrome.devtools_port_file(tmp_path))
    try:
        result = browser.navigate("https://x.test/profile", wait_ms=100)
        assert result["captured"] == []
        assert result["load_ms"] >= 100
        assert result["load_event"] is False
    finally:
        browser.close()


def test_navigate_without_load_event_still_returns_captured_bodies(tmp_path, fake_chrome):
    # Same SPA-never-loads scenario, but with a capture pattern active and a
    # normal (fast) matching response - proves the capture window still runs
    # and completes even though load_event never fires.
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
                            "url": "https://x.test/api/friends",
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
        # deliberately never sends Page.loadEventFired

    async def handle_get_body(ws, msg):
        await ws.send(
            json.dumps(
                {
                    "id": msg["id"],
                    "sessionId": msg.get("sessionId"),
                    "result": {"body": '{"friends": []}', "base64Encoded": False},
                }
            )
        )

    fake_chrome.on("Page.navigate", handle_navigate)
    fake_chrome.on("Network.getResponseBody", handle_get_body)

    browser = cdp.Browser.connect(devtools_port_path=fake_chrome.devtools_port_file(tmp_path))
    try:
        result = browser.navigate("https://x.test/friends_all", wait_ms=100, capture=["/api/"])
        assert result["load_event"] is False
        assert result["captured"] == [
            {
                "url": "https://x.test/api/friends",
                "status": 200,
                "mimeType": "application/json",
                "body": '{"friends": []}',
            }
        ]
    finally:
        browser.close()


def test_navigate_raises_cdp_error_on_page_navigate_rpc_error(tmp_path, fake_chrome):
    async def handle_navigate(ws, msg):
        await ws.send(
            json.dumps(
                {
                    "id": msg["id"],
                    "sessionId": msg.get("sessionId"),
                    "error": {"message": "No target with given id found"},
                }
            )
        )

    fake_chrome.on("Page.navigate", handle_navigate)
    browser = cdp.Browser.connect(devtools_port_path=fake_chrome.devtools_port_file(tmp_path))
    try:
        with pytest.raises(cdp.CdpError, match="No target with given id found"):
            browser.navigate("https://x.test/profile", wait_ms=100)
    finally:
        browser.close()


def test_fetch_body_that_never_replies_times_out_instead_of_hanging(tmp_path, fake_chrome, mocker):
    # The actual bug: Chrome never answers Network.getResponseBody for one
    # request. Without a bound, navigate() hangs until the outer sync
    # wrapper's own timeout, and the still-running coroutine gets destroyed
    # while pending. Patched to a tiny bound so the test stays fast.
    mocker.patch("people_sync.scrape.cdp.RESPONSE_BODY_TIMEOUT", 0.05)
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
                            "url": "https://x.test/api/stuck",
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

    async def hang(ws, msg):
        pass  # never reply to Network.getResponseBody

    fake_chrome.on("Page.navigate", handle_navigate)
    fake_chrome.on("Network.getResponseBody", hang)

    browser = cdp.Browser.connect(devtools_port_path=fake_chrome.devtools_port_file(tmp_path))
    try:
        result = browser.navigate("https://x.test/profile", wait_ms=100, capture=["/api/"])
        assert result["captured"] == []
        warn.assert_called_once()
        _, kwargs = warn.call_args
        assert kwargs["host"] == "x.test"
    finally:
        browser.close()


def test_close_cancels_pending_fetch_tasks_without_warnings(tmp_path, fake_chrome):
    browser = cdp.Browser.connect(devtools_port_path=fake_chrome.devtools_port_file(tmp_path))

    async def inject_hanging_task():
        async def never_finishes():
            await asyncio.sleep(3600)

        task = asyncio.ensure_future(never_finishes())
        browser._capture_tasks.append(task)
        return task

    task = asyncio.run_coroutine_threadsafe(inject_hanging_task(), browser._loop).result(timeout=5)

    browser.close()

    assert task.cancelled()


def test_text_returns_title_and_body_text(tmp_path, fake_chrome):
    async def handle_evaluate(ws, msg):
        assert "innerText" in msg["params"]["expression"]
        await ws.send(
            json.dumps(
                {
                    "id": msg["id"],
                    "sessionId": msg.get("sessionId"),
                    "result": {"result": {"value": "Profile\nSome body text"}},
                }
            )
        )

    fake_chrome.on("Runtime.evaluate", handle_evaluate)
    browser = cdp.Browser.connect(devtools_port_path=fake_chrome.devtools_port_file(tmp_path))
    try:
        assert browser.text() == "Profile\nSome body text"
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


# -- endpoint resolution (R1) ------------------------------------------------


class FakeJsonVersionServer:
    """A minimal /json/version HTTP endpoint - status/body configurable per
    test, standing in for a real Chrome's remote-debugging HTTP surface."""

    def __init__(self, status: int = 200, body: dict | None = None):
        self.status = status
        self.body = body or {}
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                payload = json.dumps(outer.body).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        self._server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self.endpoint = f"127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def fake_json_server():
    servers = []

    def make(status: int = 200, body: dict | None = None) -> FakeJsonVersionServer:
        server = FakeJsonVersionServer(status=status, body=body)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.stop()


def _data_dir(tmp_path, port: str, ws_path: str):
    (tmp_path / "DevToolsActivePort").write_text(f"{port}\n{ws_path}\n")
    return tmp_path


def test_resolve_ws_url_endpoint_arg_wins_over_env(monkeypatch, fake_json_server):
    winner = fake_json_server(body={"webSocketDebuggerUrl": "ws://winner/"})
    loser = fake_json_server(body={"webSocketDebuggerUrl": "ws://loser/"})
    monkeypatch.setenv(cdp.CDP_ENDPOINT_ENV, loser.endpoint)

    assert cdp._resolve_ws_url(winner.endpoint, None, None) == "ws://winner/"


def test_resolve_ws_url_env_endpoint_used_when_arg_absent(monkeypatch, fake_json_server):
    server = fake_json_server(body={"webSocketDebuggerUrl": "ws://from-env/"})
    monkeypatch.setenv(cdp.CDP_ENDPOINT_ENV, server.endpoint)

    assert cdp._resolve_ws_url(None, None, None) == "ws://from-env/"


def test_resolve_ws_url_endpoint_wins_over_data_dir_when_both_set(tmp_path, fake_json_server):
    server = fake_json_server(body={"webSocketDebuggerUrl": "ws://from-endpoint/"})
    data_dir = _data_dir(tmp_path, "9999", "/devtools/browser/should-not-be-used")

    assert cdp._resolve_ws_url(server.endpoint, str(data_dir), None) == "ws://from-endpoint/"


def test_resolve_ws_url_data_dir_reads_two_line_file(monkeypatch, tmp_path):
    monkeypatch.delenv(cdp.CDP_ENDPOINT_ENV, raising=False)
    monkeypatch.delenv(cdp.CHROME_DATA_DIR_ENV, raising=False)
    data_dir = _data_dir(tmp_path, "9333", "/devtools/browser/abc")

    assert (
        cdp._resolve_ws_url(None, str(data_dir), None) == "ws://127.0.0.1:9333/devtools/browser/abc"
    )


def test_resolve_ws_url_data_dir_env_used_when_arg_absent(monkeypatch, tmp_path):
    monkeypatch.delenv(cdp.CDP_ENDPOINT_ENV, raising=False)
    data_dir = _data_dir(tmp_path, "9334", "/devtools/browser/def")
    monkeypatch.setenv(cdp.CHROME_DATA_DIR_ENV, str(data_dir))

    assert cdp._resolve_ws_url(None, None, None) == "ws://127.0.0.1:9334/devtools/browser/def"


def test_resolve_ws_url_default_used_when_nothing_set(monkeypatch, tmp_path):
    monkeypatch.delenv(cdp.CDP_ENDPOINT_ENV, raising=False)
    monkeypatch.delenv(cdp.CHROME_DATA_DIR_ENV, raising=False)
    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_text("9335\n/devtools/browser/default\n")

    assert (
        cdp._resolve_ws_url(None, None, str(port_file))
        == "ws://127.0.0.1:9335/devtools/browser/default"
    )


def test_resolve_ws_url_endpoint_404_without_data_dir_raises_naming_both_options(
    monkeypatch, fake_json_server
):
    monkeypatch.delenv(cdp.CHROME_DATA_DIR_ENV, raising=False)
    server = fake_json_server(status=404)

    with pytest.raises(cdp.CdpError) as exc_info:
        cdp._resolve_ws_url(server.endpoint, None, None)

    message = str(exc_info.value)
    assert "data_dir" in message
    assert cdp.CHROME_DATA_DIR_ENV in message


def test_resolve_ws_url_endpoint_connection_failure_raises_cdp_error_naming_both_options(
    monkeypatch,
):
    import socket

    monkeypatch.delenv(cdp.CHROME_DATA_DIR_ENV, raising=False)
    # Grab a free port and close it immediately, so nothing listens there -
    # a deterministic connection-refused.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()

    with pytest.raises(cdp.CdpError) as exc_info:
        cdp._resolve_ws_url(f"127.0.0.1:{dead_port}", None, None)

    message = str(exc_info.value)
    assert "data_dir" in message
    assert cdp.CHROME_DATA_DIR_ENV in message


def test_resolve_ws_url_endpoint_missing_ws_url_key_raises_clear_cdp_error(fake_json_server):
    server = fake_json_server(body={"Browser": "Chrome/1.0"})

    with pytest.raises(cdp.CdpError, match=f"webSocketDebuggerUrl.*{server.endpoint}"):
        cdp._resolve_ws_url(server.endpoint, None, None)


def test_resolve_ws_url_endpoint_404_falls_back_to_data_dir(
    monkeypatch, tmp_path, fake_json_server
):
    server = fake_json_server(status=404)
    data_dir = _data_dir(tmp_path, "9336", "/devtools/browser/fallback")

    result = cdp._resolve_ws_url(server.endpoint, str(data_dir), None)

    assert result == "ws://127.0.0.1:9336/devtools/browser/fallback"


def test_connect_via_endpoint_performs_full_handshake(fake_chrome, fake_json_server):
    server = fake_json_server(
        body={"webSocketDebuggerUrl": f"ws://127.0.0.1:{fake_chrome.port}/devtools/browser/fake"}
    )
    browser = cdp.Browser.connect(endpoint=server.endpoint)
    try:
        assert browser._target_id == "T1"
        assert browser._session_id == "S1"
    finally:
        browser.close()


def test_connect_via_data_dir_performs_full_handshake(tmp_path, fake_chrome):
    data_dir = _data_dir(tmp_path, str(fake_chrome.port), "/devtools/browser/fake")

    browser = cdp.Browser.connect(data_dir=str(data_dir))
    try:
        assert browser._target_id == "T1"
        assert browser._session_id == "S1"
    finally:
        browser.close()


# -- trusted input helpers (R3) ----------------------------------------------


def _key_events(fake_chrome) -> list[dict]:
    return [m for m in fake_chrome.messages if m.get("method") == "Input.dispatchKeyEvent"]


def test_type_text_emits_one_keydown_keyup_pair_per_character(tmp_path, fake_chrome, mocker):
    """keyDown carries the text; a separate `char` event on top of it would
    insert every character twice (seen live: "aalleexx")."""
    mocker.patch.object(cdp, "_sleep")
    browser = cdp.Browser.connect(devtools_port_path=fake_chrome.devtools_port_file(tmp_path))
    try:
        browser.type_text("ab")
        events = _key_events(fake_chrome)
        assert [e["params"]["type"] for e in events] == ["keyDown", "keyUp", "keyDown", "keyUp"]
        assert [e["params"]["key"] for e in events] == ["a", "a", "b", "b"]
        assert events[0]["params"]["text"] == "a"
        assert "text" not in events[1]["params"]
        assert all(e["sessionId"] == "S1" for e in events)
    finally:
        browser.close()


def test_type_text_pauses_between_characters_within_jitter_range(tmp_path, fake_chrome, mocker):
    sleep = mocker.patch.object(cdp, "_sleep")
    browser = cdp.Browser.connect(devtools_port_path=fake_chrome.devtools_port_file(tmp_path))
    try:
        browser.type_text("hello", jitter_ms=(80, 200))
        delays = [call.args[0] for call in sleep.call_args_list]
        assert len(delays) == 5
        assert all(0.08 <= d <= 0.2 for d in delays)
        assert len(set(delays)) > 1  # jittered, not a fixed cadence
    finally:
        browser.close()


def test_insert_text_uses_insert_text_not_key_events(tmp_path, fake_chrome):
    browser = cdp.Browser.connect(devtools_port_path=fake_chrome.devtools_port_file(tmp_path))
    try:
        browser.insert_text("pasted")
        assert not _key_events(fake_chrome)
        inserts = [m for m in fake_chrome.messages if m.get("method") == "Input.insertText"]
        assert inserts and inserts[0]["params"]["text"] == "pasted"
    finally:
        browser.close()


def _rect_evaluate(rect):
    async def handle(ws, msg):
        await ws.send(
            json.dumps(
                {
                    "id": msg["id"],
                    "sessionId": msg.get("sessionId"),
                    "result": {"result": {"value": rect}},
                }
            )
        )

    return handle


def test_click_dispatches_trusted_press_and_release_at_element_center(tmp_path, fake_chrome):
    fake_chrome.on(
        "Runtime.evaluate",
        _rect_evaluate({"x": 10.0, "y": 20.0, "width": 100.0, "height": 40.0}),
    )
    browser = cdp.Browser.connect(devtools_port_path=fake_chrome.devtools_port_file(tmp_path))
    try:
        browser.click("input#username")
        mouse = [m for m in fake_chrome.messages if m.get("method") == "Input.dispatchMouseEvent"]
        assert [m["params"]["type"] for m in mouse] == [
            "mouseMoved",
            "mousePressed",
            "mouseReleased",
        ]
        for m in mouse:
            assert abs(m["params"]["x"] - 60.0) <= 5
            assert abs(m["params"]["y"] - 40.0) <= 5
        assert mouse[1]["params"]["button"] == "left"
        assert mouse[1]["params"]["clickCount"] == 1
        assert mouse[1]["params"]["buttons"] == 1
        assert mouse[2]["params"]["buttons"] == 0  # the button is up again
        assert len({(m["params"]["x"], m["params"]["y"]) for m in mouse}) == 1
    finally:
        browser.close()


def test_click_raises_when_selector_is_not_on_the_page(tmp_path, fake_chrome):
    fake_chrome.on("Runtime.evaluate", _rect_evaluate(None))
    browser = cdp.Browser.connect(devtools_port_path=fake_chrome.devtools_port_file(tmp_path))
    try:
        with pytest.raises(cdp.CdpError, match="input#nope"):
            browser.click("input#nope")
    finally:
        browser.close()


def test_wait_for_returns_true_once_the_predicate_turns_true(tmp_path, fake_chrome, mocker):
    mocker.patch.object(cdp, "_sleep")
    answers = [False, False, True]

    async def handle(ws, msg):
        await ws.send(
            json.dumps(
                {
                    "id": msg["id"],
                    "sessionId": msg.get("sessionId"),
                    "result": {"result": {"value": answers.pop(0)}},
                }
            )
        )

    fake_chrome.on("Runtime.evaluate", handle)
    browser = cdp.Browser.connect(devtools_port_path=fake_chrome.devtools_port_file(tmp_path))
    try:
        assert browser.wait_for("ready", timeout_s=5) is True
        assert answers == []
    finally:
        browser.close()


def test_wait_for_returns_false_when_the_predicate_never_turns_true(tmp_path, fake_chrome, mocker):
    mocker.patch.object(cdp, "_sleep")
    clock = {"t": 0.0}
    mocker.patch.object(
        cdp, "_now", side_effect=lambda: clock.__setitem__("t", clock["t"] + 0.4) or clock["t"]
    )
    fake_chrome.on("Runtime.evaluate", _rect_evaluate(False))
    browser = cdp.Browser.connect(devtools_port_path=fake_chrome.devtools_port_file(tmp_path))
    try:
        assert browser.wait_for("ready", timeout_s=1) is False
    finally:
        browser.close()


def test_screenshot_writes_decoded_png_bytes(tmp_path, fake_chrome):
    import base64

    async def handle(ws, msg):
        await ws.send(
            json.dumps(
                {
                    "id": msg["id"],
                    "sessionId": msg.get("sessionId"),
                    "result": {"data": base64.b64encode(b"PNGBYTES").decode()},
                }
            )
        )

    fake_chrome.on("Page.captureScreenshot", handle)
    browser = cdp.Browser.connect(devtools_port_path=fake_chrome.devtools_port_file(tmp_path))
    try:
        out = tmp_path / "shots" / "shot.png"
        browser.screenshot(out)
        assert out.read_bytes() == b"PNGBYTES"
    finally:
        browser.close()


# -- visibility, key codes, field clearing (R3 fix round 1) ------------------


def test_rect_js_and_visible_js_both_require_real_dimensions():
    """A display:none element returns a 0x0 rect, so a null-check alone lets
    a hidden field pass. Both predicates must test size and visibility."""
    for expression in (cdp.visible_js("input#x"), cdp.rect_js("input#x")):
        assert "checkVisibility" in expression
        assert "width>0" in expression.replace(" ", "")
        assert "height>0" in expression.replace(" ", "")


def test_click_refuses_an_element_with_no_visible_box(tmp_path, fake_chrome):
    fake_chrome.on("Runtime.evaluate", _rect_evaluate(None))
    browser = cdp.Browser.connect(devtools_port_path=fake_chrome.devtools_port_file(tmp_path))
    try:
        with pytest.raises(cdp.CdpError, match="input#hidden"):
            browser.click("input#hidden")
        assert not any(m.get("method") == "Input.dispatchMouseEvent" for m in fake_chrome.messages)
    finally:
        browser.close()


def test_type_text_sends_the_physical_key_code(tmp_path, fake_chrome, mocker):
    mocker.patch.object(cdp, "_sleep")
    browser = cdp.Browser.connect(devtools_port_path=fake_chrome.devtools_port_file(tmp_path))
    try:
        browser.type_text("a1. ")
        codes = [e["params"].get("code") for e in _key_events(fake_chrome)]
        assert codes == ["KeyA"] * 2 + ["Digit1"] * 2 + ["Period"] * 2 + ["Space"] * 2
    finally:
        browser.close()


def test_type_text_marks_shifted_characters_with_the_shift_modifier(tmp_path, fake_chrome, mocker):
    mocker.patch.object(cdp, "_sleep")
    browser = cdp.Browser.connect(devtools_port_path=fake_chrome.devtools_port_file(tmp_path))
    try:
        browser.type_text("aA!")
        events = _key_events(fake_chrome)
        lower, upper, bang = events[0:2], events[2:4], events[4:6]
        assert [e["params"].get("modifiers", 0) for e in lower] == [0, 0]
        assert [e["params"]["modifiers"] for e in upper] == [8, 8]
        assert upper[0]["params"]["code"] == "KeyA"  # the physical key is unshifted
        assert upper[0]["params"]["key"] == "A"
        assert bang[0]["params"]["code"] == "Digit1"
        assert bang[0]["params"]["modifiers"] == 8
    finally:
        browser.close()


def test_type_text_falls_back_to_insert_text_for_unmapped_characters(tmp_path, fake_chrome, mocker):
    """No physical key produces "é" on a US layout, and a made-up virtual key
    code is worse than none - that one character is inserted instead."""
    mocker.patch.object(cdp, "_sleep")
    browser = cdp.Browser.connect(devtools_port_path=fake_chrome.devtools_port_file(tmp_path))
    try:
        browser.type_text("aé")
        assert [e["params"]["key"] for e in _key_events(fake_chrome)] == ["a", "a"]
        inserts = [m for m in fake_chrome.messages if m.get("method") == "Input.insertText"]
        assert [m["params"]["text"] for m in inserts] == ["é"]
    finally:
        browser.close()


def test_clear_field_selects_all_and_deletes(tmp_path, fake_chrome, mocker):
    mocker.patch.object(cdp, "_sleep")
    browser = cdp.Browser.connect(devtools_port_path=fake_chrome.devtools_port_file(tmp_path))
    try:
        browser.clear_field()
        events = _key_events(fake_chrome)
        select_all = [e for e in events if e["params"].get("key") == "a"]
        assert select_all and all(e["params"]["modifiers"] == 4 for e in select_all)  # Meta
        assert any(e["params"].get("key") == "Backspace" for e in events)
        assert not any(e["params"]["type"] == "char" for e in events)
    finally:
        browser.close()


def test_press_backspace_sends_one_trusted_pair_per_press(tmp_path, fake_chrome, mocker):
    mocker.patch.object(cdp, "_sleep")
    browser = cdp.Browser.connect(devtools_port_path=fake_chrome.devtools_port_file(tmp_path))
    try:
        browser.press_backspace(3)
        events = [e for e in _key_events(fake_chrome) if e["params"].get("key") == "Backspace"]
        assert [e["params"]["type"] for e in events] == ["keyDown", "keyUp"] * 3
        assert not any(e["params"]["type"] == "char" for e in events)
    finally:
        browser.close()


# -- handshake timeout + remote-debugging approval command (R1b) -------------


def test_connect_passes_handshake_timeout_to_the_websocket_open(tmp_path, fake_chrome, mocker):
    """The documented `handshake_timeout` must reach the websocket handshake
    itself. Without it the library's own 10 s open timeout aborts the upgrade
    while the approval sheet is still waiting to be clicked."""
    real_connect = cdp.ws_connect
    seen = {}

    def spy(url, **kwargs):
        seen.update(kwargs)
        return real_connect(url, **kwargs)

    mocker.patch.object(cdp, "ws_connect", spy)
    browser = cdp.Browser.connect(
        devtools_port_path=fake_chrome.devtools_port_file(tmp_path), handshake_timeout=25.0
    )
    try:
        assert seen["open_timeout"] == 25.0
    finally:
        browser.close()


def test_connect_times_out_when_the_upgrade_is_never_accepted(tmp_path):
    slow = FakeChrome(handshake_delay=1.0)
    try:
        with pytest.raises(cdp.CdpError, match="Allow"):
            cdp.Browser.connect(
                devtools_port_path=slow.devtools_port_file(tmp_path), handshake_timeout=0.3
            )
    finally:
        slow.stop()


def test_connect_survives_an_upgrade_slower_than_the_library_default(tmp_path):
    """A delay under `handshake_timeout` must connect, not abort."""
    slow = FakeChrome(handshake_delay=0.5)
    try:
        browser = cdp.Browser.connect(
            devtools_port_path=slow.devtools_port_file(tmp_path), handshake_timeout=20.0
        )
        browser.close()
    finally:
        slow.stop()


def _popen_spy(mocker):
    return mocker.patch.object(cdp.subprocess, "Popen")


def test_approve_command_is_spawned_detached_on_the_approval_path(
    tmp_path, fake_chrome, mocker, monkeypatch
):
    monkeypatch.delenv(cdp.CDP_APPROVE_COMMAND_ENV, raising=False)
    popen = _popen_spy(mocker)
    browser = cdp.Browser.connect(
        devtools_port_path=fake_chrome.devtools_port_file(tmp_path),
        approve_command="approve-helper 25",
    )
    try:
        assert popen.call_count == 1
        assert popen.call_args.args[0] == ["approve-helper", "25"]
        assert popen.call_args.kwargs["start_new_session"] is True
        devnull = cdp.subprocess.DEVNULL
        for stream in ("stdin", "stdout", "stderr"):
            assert popen.call_args.kwargs[stream] == devnull
    finally:
        browser.close()


def test_approve_command_falls_back_to_the_environment(tmp_path, fake_chrome, mocker, monkeypatch):
    monkeypatch.setenv(cdp.CDP_APPROVE_COMMAND_ENV, "approve-helper")
    popen = _popen_spy(mocker)
    browser = cdp.Browser.connect(devtools_port_path=fake_chrome.devtools_port_file(tmp_path))
    try:
        assert popen.call_args.args[0] == ["approve-helper"]
    finally:
        browser.close()


def test_approve_command_is_never_spawned_for_an_explicit_endpoint(
    fake_chrome, fake_json_server, mocker, monkeypatch
):
    """An explicit host:port is a dedicated profile: no approval sheet ever
    appears, so clicking at one would click something else."""
    monkeypatch.setenv(cdp.CDP_APPROVE_COMMAND_ENV, "approve-helper")
    popen = _popen_spy(mocker)
    server = fake_json_server(
        body={"webSocketDebuggerUrl": f"ws://127.0.0.1:{fake_chrome.port}/devtools/browser/fake"}
    )
    browser = cdp.Browser.connect(endpoint=server.endpoint, approve_command="approve-helper")
    try:
        assert popen.call_count == 0
    finally:
        browser.close()


def test_no_approve_command_spawns_nothing(tmp_path, fake_chrome, mocker, monkeypatch):
    monkeypatch.delenv(cdp.CDP_APPROVE_COMMAND_ENV, raising=False)
    popen = _popen_spy(mocker)
    browser = cdp.Browser.connect(devtools_port_path=fake_chrome.devtools_port_file(tmp_path))
    try:
        assert popen.call_count == 0
    finally:
        browser.close()


def test_approve_command_that_cannot_start_names_only_the_option(
    tmp_path, fake_chrome, mocker, monkeypatch
):
    """The command line can embed a path from the host it runs on, so a
    failure names the kwarg and the env var and nothing else."""
    monkeypatch.delenv(cdp.CDP_APPROVE_COMMAND_ENV, raising=False)
    mocker.patch.object(cdp.subprocess, "Popen", side_effect=OSError("no such file: /secret/path"))

    with pytest.raises(cdp.CdpError) as excinfo:
        cdp.Browser.connect(
            devtools_port_path=fake_chrome.devtools_port_file(tmp_path),
            approve_command="/secret/path/approve-helper",
        )

    message = str(excinfo.value)
    assert "approve_command" in message
    assert cdp.CDP_APPROVE_COMMAND_ENV in message
    assert "/secret/path" not in message
    assert excinfo.value.__cause__ is None


def test_existing_target_is_attached_and_never_closed(tmp_path, fake_chrome, monkeypatch):
    monkeypatch.setenv("PEOPLE_SYNC_CDP_TARGET", "selected-tab")
    browser = cdp.Browser.connect(devtools_port_path=fake_chrome.devtools_port_file(tmp_path))
    browser.close()
    methods = [m["method"] for m in fake_chrome.messages]
    assert "Target.createTarget" not in methods
    assert "Target.closeTarget" not in methods
    attach = next(m for m in fake_chrome.messages if m["method"] == "Target.attachToTarget")
    assert attach["params"]["targetId"] == "selected-tab"
