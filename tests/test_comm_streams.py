"""Synthetic exact-stream HTTP contract; no estate, native stores or credentials."""

import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from people_sync import comm


def event(source="apple_messages", **over):
    return {
        "v": 1,
        "event_id": "e1",
        "at": "2026-10-01T12:00:00.000Z",
        "channel": "phone"
        if source == "apple_calls"
        else "whatsapp"
        if source.startswith("whatsapp")
        else "sms",
        "direction": "inbound",
        "conversation_kind": "direct",
        "participant_ref": "a" * 64,
        "person_id": "b" * 32,
        **(
            {"outcome": "answered", "duration_seconds": 12.5} if source in comm.CALL_SOURCES else {}
        ),
        **over,
    }


def page(records, cursor=None, key="object1"):
    return {
        "entries": [{"id": hashlib.sha256(key.encode()).hexdigest(), "body": json.dumps(records)}],
        "next_cursor": cursor,
    }


def service(monkeypatch, handle):
    monkeypatch.setenv("SOMA_HUB_URL", "https://hub.example.test")
    monkeypatch.setenv("SOMA_HUB_TOKEN", "files-token")
    monkeypatch.setenv("SOMA_COMM_HUB_TOKEN", "comm-token")
    monkeypatch.setattr(
        comm.subprocess, "run", lambda *a, **kw: pytest.fail("archive/CLI refresh forbidden")
    )
    original = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handle), **kw)
    )


def test_refresh_uses_only_four_exact_read_routes_and_dedupes_pages(monkeypatch):
    calls = []

    def handle(req):
        calls.append(req)
        assert req.method == "GET" and req.headers["Authorization"] == "Bearer comm-token"
        assert req.url.path in {f"/v1/streams/{s}/records" for s in comm.STREAM.values()}
        assert req.url.params["limit"] == "20"
        source = req.url.path.split("/")[3].removeprefix("comm_")
        if "cursor" not in req.url.params:
            return httpx.Response(200, json=page([event(source)], "opaque+/=cursor"))
        assert req.url.params["cursor"] == "opaque+/=cursor"
        return httpx.Response(
            200,
            json=page(
                [event(source), event(source, event_id="e2", at="2026-10-02T12:00:00.000Z")],
                key="object2",
            ),
        )

    service(monkeypatch, handle)
    rows = comm.hub_aggregates()
    assert len(calls) == 8 and len(rows) == 4
    assert sum(r["n"] for r in rows) == 8
    assert all(r["last_at"] == "2026-10-02T12:00:00.000Z" for r in rows)
    assert next(r for r in rows if r["channel"] == "phone")["last_seconds"] == 12.5


@pytest.mark.parametrize("bad", [None, "", 12, True, "bad\nidentity"])
def test_invalid_event_id_refuses_partial_results(monkeypatch, bad):
    service(monkeypatch, lambda req: httpx.Response(200, json=page([event(event_id=bad)])))
    with pytest.raises(comm.HubError):
        comm.hub_aggregates()


def test_conflicting_event_identity_refuses_partial_results(monkeypatch):
    service(
        monkeypatch,
        lambda req: httpx.Response(200, json=page([event(), event(direction="outbound")])),
    )
    with pytest.raises(comm.HubError):
        comm.hub_aggregates()


@pytest.mark.parametrize("status", [401, 403, 413, 429, 500, 503])
def test_refusal_does_not_try_archive_replay_or_broader_routes(monkeypatch, status):
    calls = []

    def handle(req):
        calls.append(req.url.path)
        return httpx.Response(status, json={"error": "refused"})

    service(monkeypatch, handle)
    with pytest.raises(comm.HubError):
        comm.hub_aggregates()
    assert calls == ["/v1/streams/comm_apple_messages/records"] * (5 if status == 413 else 1)


def test_repeated_cursor_is_rejected(monkeypatch):
    service(
        monkeypatch,
        lambda req: httpx.Response(
            200,
            json=page(
                [], cursor="repeat", key="first" if "cursor" not in req.url.params else "second"
            ),
        ),
    )
    with pytest.raises(comm.HubError):
        comm.hub_aggregates()


@pytest.mark.parametrize("late", [False, True])
@pytest.mark.parametrize("status", [403, 413])
def test_refresh_falls_back_to_mirror_after_http_failure(
    monkeypatch, tmp_path, capsys, late, status
):
    service(
        monkeypatch,
        lambda req: (
            httpx.Response(200, json=page([event(), event(event_id="remote-only")]))
            if late and req.url.path == "/v1/streams/comm_apple_messages/records"
            else httpx.Response(status)
        ),
    )
    state = comm.State(tmp_path / "state")
    with state.db:
        state.add(comm.STREAM["apple_messages"], event())
        state.mark_landed(comm.STREAM["apple_messages"], ["e1"])
    monkeypatch.setattr(comm, "_state", lambda args: state)
    monkeypatch.setattr(comm, "load_resolver", lambda stores: (comm.Resolver({}), {}))
    monkeypatch.setattr(
        comm.somadata, "sql", lambda query: [{"id": "b" * 32}] if "FROM people" in query else []
    )
    written = []
    monkeypatch.setattr(comm, "write_summaries", lambda rows: written.extend(rows) or {})
    comm.cmd_refresh(SimpleNamespace(local=False))
    assert json.loads(capsys.readouterr().out)["source"] == "local"
    assert len(written) == 1 and written[0]["inbound_count"] == 1
    state.db.close()


@pytest.mark.parametrize(
    "stream",
    ["unrelated", "comm_apple_calls/replay", "../archive/query", "comm_apple_calls/../unrelated"],
)
def test_append_refuses_unrelated_stream_before_cli(monkeypatch, stream):
    monkeypatch.setattr(comm.subprocess, "run", lambda *a, **kw: pytest.fail("must not invoke CLI"))
    with pytest.raises(comm.AppendError):
        comm.soma_append(stream, [event()])


@pytest.mark.parametrize("source", comm.SOURCES)
def test_each_exact_stream_can_append_without_archive_or_replay(monkeypatch, source):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(comm.subprocess, "run", run)
    comm.soma_append(comm.STREAM[source], [event(source)])
    assert calls == [["soma", "stream", "import", comm.STREAM[source], "--chunk", "1000"]]


@pytest.mark.parametrize(
    "bad_page",
    [
        {"entries": [], "next_cursor": ""},
        {"entries": [], "next_cursor": 10},
        {"entries": {}, "next_cursor": None},
        page({"event_id": "broken"}),
        page([event(channel="whatsapp")]),
        page([event(at="2026-10-01T12:00:00")]),
        page([event(at="bad")]),
        page([event(person_id="not-a-person-id")]),
    ],
)
def test_invalid_pages_or_events_never_publish_partial_aggregates(monkeypatch, bad_page):
    service(monkeypatch, lambda req: httpx.Response(200, json=bad_page))
    with pytest.raises(comm.HubError):
        comm.hub_aggregates()


def test_response_byte_cap_is_enforced(monkeypatch):
    monkeypatch.setattr(comm, "STREAM_PAGE_BYTES", 50)
    service(monkeypatch, lambda req: httpx.Response(200, json=page([event()])))
    with pytest.raises(comm.HubError, match="byte limit"):
        comm.hub_aggregates()


def test_stream_scan_budget_is_not_a_partial_success(monkeypatch):
    monkeypatch.setattr(comm, "STREAM_MAX_PAGES", 1)
    service(monkeypatch, lambda req: httpx.Response(200, json=page([event()], cursor="more")))
    with pytest.raises(comm.HubError, match="budget"):
        comm.hub_aggregates()


def test_repeated_object_is_rejected(monkeypatch):
    service(
        monkeypatch,
        lambda req: httpx.Response(
            200, json=page([event()], cursor="more" if "cursor" not in req.url.params else None)
        ),
    )
    with pytest.raises(comm.HubError, match="repeated stream object"):
        comm.hub_aggregates()


def test_redirect_does_not_send_credentials_to_another_server(monkeypatch):
    calls = []

    def handle(req):
        calls.append(req)
        return httpx.Response(
            302, headers={"location": "https://other.example.test/v1/archive/query"}
        )

    service(monkeypatch, handle)
    with pytest.raises(comm.HubError):
        comm.hub_aggregates()
    assert len(calls) == 1


def test_event_projection_canonicalizes_time_and_drops_unknown_content():
    record = comm._aggregate_event(
        event(at="2026-10-01T14:00:00+02:00", body="CANARY-NOT-METADATA"), "apple_messages"
    )
    assert record["at"] == "2026-10-01T12:00:00.000Z"
    assert "CANARY" not in json.dumps(record)


def test_outbox_uses_mirror_without_reading_hub(monkeypatch, tmp_path, capsys):
    service(monkeypatch, lambda req: pytest.fail("outbox must use mirror"))
    state = comm.State(tmp_path / "state")
    with state.db:
        state.add(comm.STREAM["apple_messages"], event())
    monkeypatch.setattr(comm, "_state", lambda args: state)
    monkeypatch.setattr(comm, "load_resolver", lambda stores: (comm.Resolver({}), {}))
    monkeypatch.setattr(
        comm.somadata, "sql", lambda query: [{"id": "b" * 32}] if "FROM people" in query else []
    )
    monkeypatch.setattr(comm, "write_summaries", lambda rows: {})
    comm.cmd_refresh(SimpleNamespace(local=False))
    assert json.loads(capsys.readouterr().out)["source"] == "local"
    assert state.waiting() == 1
    state.db.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://outside.example.test",
        "https://user:pass@hub.example.test",
        "https://hub.example.test?token=x",
        "https://[",
    ],
)
def test_invalid_hub_url_fails_closed_before_requests(monkeypatch, url):
    service(monkeypatch, lambda req: pytest.fail("invalid URL must not send a credential"))
    monkeypatch.setenv("SOMA_HUB_URL", url)
    with pytest.raises(comm.HubError):
        comm.hub_aggregates()


@pytest.mark.parametrize("stream", ["unrelated", "comm_apple_calls/replay", "../archive/query"])
def test_reader_refuses_noncanonical_stream_routes(monkeypatch, stream):
    service(
        monkeypatch,
        lambda req: pytest.fail("must not request an unrelated or replay/archive route"),
    )
    with httpx.Client() as client, pytest.raises(comm.HubError):
        comm._stream_page(client, "https://hub.example.test", stream, "comm-token", None)


@pytest.mark.parametrize("at", [None, True, 123])
def test_invalid_timestamp_types_trigger_safe_mirror_fallback_error(monkeypatch, at):
    service(monkeypatch, lambda req: httpx.Response(200, json=page([event(at=at)])))
    with pytest.raises(comm.HubError):
        comm.hub_aggregates()


def test_server_shaped_413_reduces_page_size_preserving_cursor_scope_and_dedupe(monkeypatch):
    stream = "comm_apple_messages"
    cursors = {2: "opaque+/=2", 4: "opaque+/=4"}
    objects = []
    for i, event_id in enumerate(["e1", "e2", "e1", "e3", "e4", "e5"]):
        objects.extend(
            page([event(event_id=event_id, padding="x" * 600_000)], key=str(i))["entries"]
        )
    calls = []

    def handle(req):
        assert req.method == "GET" and req.headers["Authorization"] == "Bearer comm-token"
        assert req.url.path in {f"/v1/streams/{s}/records" for s in comm.STREAM.values()}
        assert set(req.url.params) <= {"limit", "cursor"}
        if req.url.path != f"/v1/streams/{stream}/records":
            return httpx.Response(200, json={"entries": [], "next_cursor": None})
        limit = int(req.url.params["limit"])
        requested = req.url.params.get("cursor")
        calls.append((requested, limit))
        assert requested in (None, *cursors.values())
        start = 0 if requested is None else next(i for i, c in cursors.items() if c == requested)
        entries = objects[start : start + limit]
        result = {"entries": entries, "next_cursor": cursors.get(start + len(entries))}
        body = json.dumps(result, separators=(",", ":")).encode()
        if len(body) > 2_000_000:
            return httpx.Response(413, json={"error": "stream page too large"})
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    service(monkeypatch, handle)
    (row,) = comm.hub_aggregates()
    assert row["n"] == 5
    assert calls == [
        *((c, limit) for c in [None, cursors[2]] for limit in [20, 10, 5, 2]),
        (cursors[4], 20),
    ]


def test_413_single_object_stops_with_same_cursor_and_no_partial_result(monkeypatch):
    cursor = "opaque+/=after-first"
    calls = []

    def handle(req):
        requested = req.url.params.get("cursor")
        calls.append((req.url.path, requested, int(req.url.params["limit"])))
        assert len(calls) <= 6, "unbounded 413 retry"
        if requested is None:
            return httpx.Response(200, json=page([event()], cursor=cursor))
        return httpx.Response(413, json={"error": "stream page too large"})

    service(monkeypatch, handle)
    with pytest.raises(comm.HubError, match="413"):
        comm.hub_aggregates()
    assert [(requested, limit) for _, requested, limit in calls] == [
        (None, 20),
        *((cursor, n) for n in [20, 10, 5, 2, 1]),
    ]
    assert {path for path, _, _ in calls} == {"/v1/streams/comm_apple_messages/records"}


def test_reduced_page_still_enforces_response_bytes_without_further_retry(monkeypatch):
    calls = []

    def handle(req):
        calls.append(int(req.url.params["limit"]))
        if len(calls) == 1:
            return httpx.Response(413, json={"error": "stream page too large"})
        return httpx.Response(200, content=b" " * (comm.STREAM_PAGE_BYTES + 1))

    service(monkeypatch, handle)
    with pytest.raises(comm.HubError, match="byte limit"):
        comm.hub_aggregates()
    assert calls == [20, 10]


def test_server_must_honor_reduced_object_count(monkeypatch):
    def handle(req):
        if int(req.url.params["limit"]) > 1:
            return httpx.Response(413, json={"error": "stream page too large"})
        first = page([event()], key="one")["entries"]
        second = page([event(event_id="e2")], key="two")["entries"]
        return httpx.Response(200, json={"entries": first + second, "next_cursor": None})

    service(monkeypatch, handle)
    with pytest.raises(comm.HubError, match="invalid stream entries"):
        comm.hub_aggregates()


@pytest.mark.parametrize("location", ["page", "landing"])
@pytest.mark.parametrize("late", [False, True])
def test_deep_stream_json_uses_mirror_and_discards_partial_hub_result(
    monkeypatch, tmp_path, capsys, location, late
):
    deep = "[" * 10_000 + "0" + "]" * 10_000

    def handle(req):
        if late and req.url.path == "/v1/streams/comm_apple_messages/records":
            return httpx.Response(200, json=page([event(), event(event_id="remote-only")]))
        if location == "page":
            return httpx.Response(200, content=deep.encode())
        payload = page([])
        payload["entries"][0]["body"] = deep
        return httpx.Response(200, json=payload)

    service(monkeypatch, handle)
    state = comm.State(tmp_path / "state")
    with state.db:
        state.add(comm.STREAM["apple_messages"], event())
        state.mark_landed(comm.STREAM["apple_messages"], ["e1"])
    monkeypatch.setattr(comm, "_state", lambda args: state)
    monkeypatch.setattr(comm, "load_resolver", lambda stores: (comm.Resolver({}), {}))
    monkeypatch.setattr(
        comm.somadata, "sql", lambda query: [{"id": "b" * 32}] if "FROM people" in query else []
    )
    written = []
    monkeypatch.setattr(comm, "write_summaries", lambda rows: written.extend(rows) or {})
    try:
        comm.cmd_refresh(SimpleNamespace(local=False))
        report = json.loads(capsys.readouterr().out)
        assert report["source"] == "local" and "JSON" in report["why"]
        assert len(written) == 1 and written[0]["inbound_count"] == 1
        assert state.waiting() == 0
    finally:
        state.db.close()


def test_unrelated_recursion_errors_are_not_swallowed_as_parse_failures(monkeypatch):
    service(monkeypatch, lambda req: httpx.Response(200, json=page([event()])))

    def unexpected(*args):
        raise RecursionError("unrelated application failure")

    monkeypatch.setattr(comm, "_aggregate_event", unexpected)
    with pytest.raises(RecursionError, match="unrelated application failure"):
        comm.hub_aggregates()
