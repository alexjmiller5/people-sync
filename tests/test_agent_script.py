"""scripts/people-sync-agent, run for real against a stub `people-sync` and a
stub remote-debugging endpoint: in shared-endpoint mode it must attach every
platform to that endpoint and never launch a browser; a dead endpoint ends
the run before any login."""

import http.server
import os
import stat
import subprocess
import threading
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "people-sync-agent"


@pytest.fixture
def stub_bin(tmp_path):
    """A `people-sync` that records its argv and a `curl` on PATH."""
    calls = tmp_path / "calls.log"
    stub = tmp_path / "people-sync"
    stub.write_text(f'#!/bin/sh\necho "$@" >> {calls}\n')
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return stub, calls


@pytest.fixture
def endpoint():
    class Version(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path == "/json/version" else 404)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Version)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"127.0.0.1:{server.server_port}"
    server.shutdown()


def run_agent(tmp_path, stub, env):
    script = tmp_path / "agent"
    script.write_text(SCRIPT.read_text().replace("@people_sync_bin@", str(stub)))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    base = {
        "PATH": os.environ["PATH"],
        "PEOPLE_SYNC_PLATFORMS": "instagram venmo",
        "PEOPLE_SYNC_STATE_DIR": str(tmp_path / "state"),
        "PEOPLE_SYNC_LOG_DIR": str(tmp_path / "logs"),
    }
    return subprocess.run([str(script)], env={**base, **env}, capture_output=True, text=True)


def test_shared_endpoint_attaches_every_platform_and_launches_nothing(tmp_path, stub_bin, endpoint):
    stub, calls = stub_bin

    result = run_agent(
        tmp_path,
        stub,
        {"PEOPLE_SYNC_ENDPOINT": endpoint, "PEOPLE_SYNC_CHROME_PATH": "/nonexistent"},
    )

    assert result.returncode == 0, result.stderr
    lines = calls.read_text().splitlines()
    assert lines[0] == f"login instagram --endpoint {endpoint}"
    assert lines[1].startswith(f"scrape instagram --endpoint {endpoint} --state ")
    assert lines[2] == f"login venmo --endpoint {endpoint}"
    assert len(lines) == 4


def test_dead_shared_endpoint_ends_the_run_before_any_login(tmp_path, stub_bin):
    stub, calls = stub_bin

    result = run_agent(tmp_path, stub, {"PEOPLE_SYNC_ENDPOINT": "127.0.0.1:1"})

    assert result.returncode == 1
    assert not calls.exists()
    assert "not listening" in (tmp_path / "logs" / "launchd.log").read_text()


def test_without_an_endpoint_the_per_site_variables_are_required(tmp_path, stub_bin):
    stub, calls = stub_bin

    result = run_agent(tmp_path, stub, {})

    assert result.returncode != 0
    assert "PEOPLE_SYNC_PROFILE_DIR" in result.stderr
    assert not calls.exists()
