import base64
import hashlib
import json
import sqlite3
import shutil
import subprocess

import pytest

from people_sync.scrape import run
from people_sync.scrape.cdp import CdpError
from people_sync.scrape.profile import ExtractError, Profile


@pytest.fixture(autouse=True)
def no_live_archive(mocker):
    mocker.patch("people_sync.photos.put_object")


@pytest.mark.skipif(shutil.which("node") is None, reason="execute browser fetch guard")
@pytest.mark.parametrize(
    "ok,mime,expected",
    [
        (False, "text/html", None),
        (True, "text/html", None),
        (True, "image/jpeg", b"image"),
    ],
)
def test_avatar_page_fetch_rejects_errors_and_non_images(ok, mime, expected):
    class Browser:
        def eval(self, js):
            response = json.dumps({"ok": ok, "mime": mime})
            script = (
                f"const r={response};globalThis.fetch=async()=>({{...r,"
                "headers:{get:()=>r.mime},arrayBuffer:async()=>new TextEncoder().encode('image')});"
                f"Promise.resolve({js}).then(v=>console.log(JSON.stringify(v)));"
            )
            return json.loads(subprocess.check_output(["node", "-e", script], text=True))

    assert run._fetch_avatar_via_page(Browser(), "https://example.invalid/avatar") == expected


class FakeModule:
    """Stands in for a platform module (instagram.py's shape)."""

    URL = "https://example.test/{handle}/"
    CAPTURE = [r"web_profile_info"]
    EXTRACTOR_JS = "EXTRACT()"

    @staticmethod
    def parse(eval_result, captured):
        return Profile(
            platform="testplatform",
            profile_url=f"https://example.test/{eval_result.get('username')}/",
            platform_id=eval_result.get("username"),
            display_name=eval_result.get("full_name"),
            avatar_url="https://example.test/avatar.jpg",
        )


class FakeBrowser:
    def __init__(self, page_text="Ordinary Title\nOrdinary bio text"):
        self.page_text = page_text
        self.navigated: list[str] = []
        self.closed = False

    def navigate(self, url, wait_ms, capture=None):
        self.navigated.append(url)
        return {"captured": [], "load_ms": 10.0}

    def eval(self, js):
        if "innerText" in js:
            return self.page_text
        if "fetch(" in js:
            return None
        return json.dumps({"username": "u1", "full_name": "Test User"})

    def close(self):
        self.closed = True


def _record(**overrides) -> dict:
    row = {"id": "testplatform:u1", "handle": "u1", "avatar_r2_key": None, "avatar_sha256": None}
    row.update(overrides)
    return row


def _patch_common(mocker, records, browser=None, allow=True):
    mocker.patch("people_sync.scrape.run._select_records", return_value=records)
    mocker.patch("people_sync.scrape.run.import_module", return_value=FakeModule)
    browser = browser or FakeBrowser()
    mocker.patch("people_sync.scrape.run.Browser.connect", return_value=browser)
    pacer_cls = mocker.patch("people_sync.scrape.run.Pacer")
    pacer = pacer_cls.return_value
    pacer.allow.return_value = allow
    pacer.next_gap.return_value = 0.0
    mocker.patch("people_sync.scrape.run.time.sleep")
    return browser, pacer


def test_scrape_passes_endpoint_and_data_dir_to_browser_connect(mocker):
    mocker.patch("people_sync.scrape.run._select_records", return_value=[])
    mocker.patch("people_sync.scrape.run.import_module", return_value=FakeModule)
    connect = mocker.patch("people_sync.scrape.run.Browser.connect", return_value=FakeBrowser())
    mocker.patch("people_sync.scrape.run.Pacer")

    run.scrape(
        "testplatform",
        endpoint="mini.local:9333",
        data_dir="/tmp/profile",
        approve_command="approve-helper 25",
    )

    connect.assert_called_once_with(
        endpoint="mini.local:9333", data_dir="/tmp/profile", approve_command="approve-helper 25"
    )


def test_scrape_defaults_endpoint_and_data_dir_to_none(mocker):
    mocker.patch("people_sync.scrape.run._select_records", return_value=[])
    mocker.patch("people_sync.scrape.run.import_module", return_value=FakeModule)
    connect = mocker.patch("people_sync.scrape.run.Browser.connect", return_value=FakeBrowser())
    mocker.patch("people_sync.scrape.run.Pacer")

    run.scrape("testplatform")

    connect.assert_called_once_with(endpoint=None, data_dir=None, approve_command=None)


def test_cap_reached_stops_before_navigating(mocker):
    browser, pacer = _patch_common(mocker, [_record()], allow=False)
    put_object = mocker.patch("people_sync.photos.put_object")
    upsert = mocker.patch("people_sync.scrape.run.upsert_profile")

    result = run.scrape("testplatform")

    assert result == {"done": 0, "skipped": 0, "halted": "daily cap reached"}
    assert browser.navigated == []
    assert browser.closed is True
    put_object.assert_not_called()
    upsert.assert_not_called()
    pacer.record.assert_not_called()


def test_challenge_page_halts_and_writes_nothing(mocker):
    browser, pacer = _patch_common(
        mocker, [_record()], browser=FakeBrowser(page_text="Please log in to continue")
    )
    put_object = mocker.patch("people_sync.photos.put_object")
    upsert = mocker.patch("people_sync.scrape.run.upsert_profile")

    result = run.scrape("testplatform")

    assert result == {"done": 0, "skipped": 0, "halted": "challenge page: log in to continue"}
    assert browser.navigated == ["https://example.test/u1/"]
    put_object.assert_not_called()
    upsert.assert_not_called()
    pacer.record.assert_not_called()
    assert browser.closed is True


def test_normal_record_uploads_raw_before_upsert_and_calls_pace(mocker):
    browser, pacer = _patch_common(mocker, [_record()])
    mocker.patch("people_sync.photos.fetch_url_photo", return_value=b"avatar-bytes")
    put_object = mocker.patch("people_sync.photos.put_object")
    upsert = mocker.patch("people_sync.scrape.run.upsert_profile")

    manager = mocker.MagicMock()
    manager.attach_mock(put_object, "put_object")
    manager.attach_mock(upsert, "upsert_profile")

    result = run.scrape("testplatform")

    assert result == {"done": 1, "skipped": 0, "halted": None}

    call_names = [c[0] for c in manager.mock_calls]
    assert call_names.index("put_object") < call_names.index("upsert_profile")

    raw_calls = [c for c in put_object.call_args_list if c.args[0].startswith("profiles/")]
    assert len(raw_calls) == 1
    assert raw_calls[0].args[0].startswith("profiles/testplatform/testplatform_u1/")
    payload = json.loads(raw_calls[0].args[1])
    assert payload["eval"]["username"] == "u1"

    avatar_calls = [c for c in put_object.call_args_list if c.args[0].startswith("photos/records/")]
    assert len(avatar_calls) == 1
    sha = hashlib.sha256(b"avatar-bytes").hexdigest()
    assert avatar_calls[0].args[0] == f"photos/records/testplatform/testplatform_u1-{sha[:8]}.jpg"

    profile, avatar_key, avatar_sha, raw_key = upsert.call_args.args
    assert profile.record_id == "testplatform:u1"
    assert avatar_key == avatar_calls[0].args[0]
    assert avatar_sha == sha
    assert raw_key == raw_calls[0].args[0]

    pacer.record.assert_called_once()
    pacer.next_gap.assert_called_once()


def test_avatar_dedupe_skips_reupload_when_sha_matches(mocker):
    sha = hashlib.sha256(b"avatar-bytes").hexdigest()
    record = _record(
        avatar_r2_key="photos/records/testplatform/testplatform_u1-existing.jpg",
        avatar_sha256=sha,
    )
    browser, pacer = _patch_common(mocker, [record])
    mocker.patch("people_sync.photos.fetch_url_photo", return_value=b"avatar-bytes")
    put_object = mocker.patch("people_sync.photos.put_object")
    upsert = mocker.patch("people_sync.scrape.run.upsert_profile")

    run.scrape("testplatform")

    avatar_calls = [c for c in put_object.call_args_list if c.args[0].startswith("photos/records/")]
    assert avatar_calls == []  # no re-upload, bytes are identical

    profile, avatar_key, avatar_sha, raw_key = upsert.call_args.args
    assert avatar_key == "photos/records/testplatform/testplatform_u1-existing.jpg"
    assert avatar_sha == sha


def test_avatar_upload_happens_when_sha_changes(mocker):
    record = _record(
        avatar_r2_key="photos/records/testplatform/testplatform_u1-old.jpg",
        avatar_sha256="a-different-sha",
    )
    browser, pacer = _patch_common(mocker, [record])
    mocker.patch("people_sync.photos.fetch_url_photo", return_value=b"new-avatar-bytes")
    put_object = mocker.patch("people_sync.photos.put_object")
    upsert = mocker.patch("people_sync.scrape.run.upsert_profile")

    run.scrape("testplatform")

    avatar_calls = [c for c in put_object.call_args_list if c.args[0].startswith("photos/records/")]
    assert len(avatar_calls) == 1
    profile, avatar_key, avatar_sha, raw_key = upsert.call_args.args
    assert avatar_key == avatar_calls[0].args[0]
    assert avatar_sha == hashlib.sha256(b"new-avatar-bytes").hexdigest()


def test_avatar_falls_back_to_page_fetch_when_direct_fetch_fails(mocker):
    _patch_common(mocker, [_record()])
    mocker.patch("people_sync.photos.fetch_url_photo", return_value=None)
    b64 = base64.b64encode(b"page-fetched-bytes").decode()

    class PageFetchBrowser(FakeBrowser):
        def eval(self, js):
            if "innerText" in js:
                return self.page_text
            if "fetch(" in js:
                return b64
            return json.dumps({"username": "u1", "full_name": "Test User"})

    mocker.patch("people_sync.scrape.run.Browser.connect", return_value=PageFetchBrowser())
    put_object = mocker.patch("people_sync.photos.put_object")
    upsert = mocker.patch("people_sync.scrape.run.upsert_profile")

    run.scrape("testplatform")

    sha = hashlib.sha256(b"page-fetched-bytes").hexdigest()
    avatar_calls = [c for c in put_object.call_args_list if c.args[0].startswith("photos/records/")]
    assert len(avatar_calls) == 1
    assert avatar_calls[0].args[1] == b"page-fetched-bytes"
    _, avatar_key, avatar_sha, _ = upsert.call_args.args
    assert avatar_sha == sha


def test_records_with_no_handle_are_skipped_not_navigated(mocker):
    browser, pacer = _patch_common(mocker, [_record(handle=None)])
    put_object = mocker.patch("people_sync.photos.put_object")

    result = run.scrape("testplatform")

    assert result == {"done": 0, "skipped": 1, "halted": None}
    assert browser.navigated == []
    put_object.assert_not_called()


def test_max_n_limits_records_processed(mocker):
    records = [
        {
            "id": f"testplatform:u{i}",
            "handle": f"u{i}",
            "avatar_r2_key": None,
            "avatar_sha256": None,
        }
        for i in range(3)
    ]
    browser, pacer = _patch_common(mocker, records)
    mocker.patch("people_sync.photos.fetch_url_photo", return_value=None)
    mocker.patch("people_sync.photos.put_object")
    mocker.patch("people_sync.scrape.run.upsert_profile")

    result = run.scrape("testplatform", max_n=2)

    assert result["done"] == 2
    assert len(browser.navigated) == 2


def test_record_failure_is_isolated_and_next_record_still_processes(mocker):
    records = [
        {"id": "testplatform:u0", "handle": "u0", "avatar_r2_key": None, "avatar_sha256": None},
        {"id": "testplatform:u1", "handle": "u1", "avatar_r2_key": None, "avatar_sha256": None},
        {"id": "testplatform:u2", "handle": "u2", "avatar_r2_key": None, "avatar_sha256": None},
    ]
    parse_calls = {"n": 0}

    class FlakyModule(FakeModule):
        @staticmethod
        def parse(eval_result, captured):
            parse_calls["n"] += 1
            if parse_calls["n"] == 2:
                raise ValueError("boom")
            return FakeModule.parse(eval_result, captured)

    mocker.patch("people_sync.scrape.run._select_records", return_value=records)
    mocker.patch("people_sync.scrape.run.import_module", return_value=FlakyModule)
    browser = FakeBrowser()
    mocker.patch("people_sync.scrape.run.Browser.connect", return_value=browser)
    pacer_cls = mocker.patch("people_sync.scrape.run.Pacer")
    pacer = pacer_cls.return_value
    pacer.allow.return_value = True
    pacer.next_gap.return_value = 0.0
    sleep = mocker.patch("people_sync.scrape.run.time.sleep")
    mocker.patch("people_sync.photos.fetch_url_photo", return_value=None)
    put_object = mocker.patch("people_sync.photos.put_object")
    upsert = mocker.patch("people_sync.scrape.run.upsert_profile")
    warn = mocker.patch.object(run.log, "warning")

    result = run.scrape("testplatform")

    assert result == {"done": 2, "skipped": 1, "halted": None}
    assert upsert.call_count == 2
    assert pacer.record.call_count == 2
    # Parsing failures must retain their original payload too.
    raw_calls = [c for c in put_object.call_args_list if c.args[0].startswith("profiles/")]
    assert len(raw_calls) == 3
    warn.assert_any_call("record failed", platform="testplatform", index=1, reason="ValueError")
    # a failure still sleeps the normal gap - it must not speed up the loop
    assert sleep.call_count == 3


def test_extractor_error_sentinel_archives_without_cache_writes(mocker):
    class SentinelModule(FakeModule):
        @staticmethod
        def parse(eval_result, captured):
            raise ExtractError(eval_result.get("error", "no-header"))

    browser, pacer = _patch_common(mocker, [_record()])
    mocker.patch("people_sync.scrape.run.import_module", return_value=SentinelModule)
    put_object = mocker.patch("people_sync.photos.put_object")
    upsert = mocker.patch("people_sync.scrape.run.upsert_profile")
    warn = mocker.patch.object(run.log, "warning")

    result = run.scrape("testplatform")

    assert result == {"done": 0, "skipped": 1, "halted": None}
    put_object.assert_called_once()
    upsert.assert_not_called()
    pacer.record.assert_not_called()
    pacer.next_gap.assert_called_once()
    warn.assert_any_call("record failed", platform="testplatform", index=0, reason="no-header")


def test_browser_lost_error_halts_cleanly_and_closes_browser(mocker):
    class DyingBrowser(FakeBrowser):
        def navigate(self, url, wait_ms, capture=None):
            raise CdpError("boom")

    browser = DyingBrowser()
    mocker.patch("people_sync.scrape.run._select_records", return_value=[_record()])
    mocker.patch("people_sync.scrape.run.import_module", return_value=FakeModule)
    mocker.patch("people_sync.scrape.run.Browser.connect", return_value=browser)
    pacer_cls = mocker.patch("people_sync.scrape.run.Pacer")
    pacer = pacer_cls.return_value
    pacer.allow.return_value = True
    put_object = mocker.patch("people_sync.photos.put_object")
    upsert = mocker.patch("people_sync.scrape.run.upsert_profile")

    result = run.scrape("testplatform")

    assert result == {"done": 0, "skipped": 0, "halted": "browser lost"}
    assert browser.closed is True
    put_object.assert_not_called()
    upsert.assert_not_called()
    pacer.record.assert_not_called()


def test_records_sql_filters_deleted_ignored_and_stale_window():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE people_sync_records "
        "(id TEXT, source TEXT, handle TEXT, name TEXT, status TEXT, "
        "deleted_at TEXT, first_seen TEXT)"
    )
    conn.execute(
        "CREATE TABLE people_sync_profiles (record_id TEXT, scraped_at TEXT, "
        "avatar_r2_key TEXT, avatar_sha256 TEXT)"
    )
    conn.executemany(
        "INSERT INTO people_sync_records VALUES (?,?,?,?,?,?,?)",
        [
            ("testplatform:new", "testplatform", "new", None, "pending", None, "3"),
            ("testplatform:stale", "testplatform", "stale", None, "matched", None, "2"),
            ("testplatform:fresh", "testplatform", "fresh", None, "pending", None, "1"),
            (
                "testplatform:deleted",
                "testplatform",
                "deleted",
                None,
                "pending",
                "2026-01-01T00:00:00.000Z",
                "4",
            ),
            ("testplatform:ignored", "testplatform", "ignored", None, "ignored", None, "5"),
            ("other:someone", "other", "someone", None, "pending", None, "6"),
        ],
    )
    conn.executemany(
        "INSERT INTO people_sync_profiles VALUES (?,?,?,?)",
        [
            ("testplatform:fresh", "2026-08-01T00:00:00.000Z", None, None),  # recent: excluded
            ("testplatform:stale", "2026-01-01T00:00:00.000Z", None, None),  # stale: included
        ],
    )
    conn.commit()

    query = run._records_sql("testplatform", "2026-03-01T00:00:00.000Z")
    ids = [row[0] for row in conn.execute(query).fetchall()]

    assert ids == ["testplatform:stale", "testplatform:new"]


def test_scrape_waits_for_a_module_ready_predicate_before_extracting(mocker):
    class ReadyModule(FakeModule):
        READY_JS = "READY()"

    class RecordingBrowser(FakeBrowser):
        def __init__(self):
            super().__init__()
            self.calls: list[str] = []

        def wait_for(self, js, timeout):
            self.calls.append(f"wait:{js}:{timeout}")
            return True

        def eval(self, js):
            self.calls.append(f"eval:{js}")
            return super().eval(js)

    browser = RecordingBrowser()
    _patch_common(mocker, [_record()], browser=browser)
    mocker.patch("people_sync.scrape.run.import_module", return_value=ReadyModule)
    mocker.patch("people_sync.scrape.run.photos.fetch_url_photo", return_value=None)
    mocker.patch("people_sync.scrape.run.upsert_profile")

    run.scrape("testplatform")

    wait = browser.calls.index(f"wait:READY():{run.READY_TIMEOUT_S}")
    assert wait < browser.calls.index("eval:EXTRACT()")


def test_challenge_halt_names_the_marker_and_saves_a_screenshot(mocker, tmp_path):
    browser = FakeBrowser(page_text="Please log in to continue")
    browser.screenshot = mocker.Mock()
    _patch_common(mocker, [_record()], browser=browser)

    result = run.scrape("testplatform", state_path=str(tmp_path / "state.json"))

    assert result["halted"] == "challenge page: log in to continue"
    shot = browser.screenshot.call_args.args[0]
    assert (
        shot.startswith(str(tmp_path)) and "scrape-testplatform-" in shot and shot.endswith(".png")
    )


def test_unavailable_profile_gets_a_placeholder_row_and_is_not_retried(mocker):
    class GoneModule(FakeModule):
        @staticmethod
        def parse(eval_result, captured):
            raise ExtractError(run.UNAVAILABLE)

    browser, pacer = _patch_common(mocker, [_record()])
    mocker.patch("people_sync.scrape.run.import_module", return_value=GoneModule)
    upsert = mocker.patch("people_sync.scrape.run.upsert_profile")
    upload = mocker.patch("people_sync.photos.put_object")

    result = run.scrape("testplatform")

    assert result == {"done": 0, "skipped": 1, "halted": None}
    placeholder = upsert.call_args.args[0]
    assert placeholder.record_id == "testplatform:u1" and placeholder.raw == {"unavailable": True}
    assert placeholder.display_name is None
    assert upsert.call_args.kwargs["raw_r2_key"] == upload.call_args.args[0]


def test_other_extract_errors_leave_cache_unchanged(mocker):
    class BrokenModule(FakeModule):
        @staticmethod
        def parse(eval_result, captured):
            raise ExtractError("no-header")

    _patch_common(mocker, [_record()])
    mocker.patch("people_sync.scrape.run.import_module", return_value=BrokenModule)
    upsert = mocker.patch("people_sync.scrape.run.upsert_profile")

    result = run.scrape("testplatform")

    assert result["skipped"] == 1
    upsert.assert_not_called()


def test_scrape_merges_a_module_enrich_hook_into_the_captured_entries(mocker):
    class EnrichingModule(FakeModule):
        seen = []

        @staticmethod
        def enrich(browser, handle):
            EnrichingModule.seen.append(handle)
            return [{"url": "https://x/web_profile_info/?username=u1", "body": "{}"}]

        @staticmethod
        def parse(eval_result, captured):
            assert captured and captured[-1]["url"].endswith("username=u1")
            return FakeModule.parse(eval_result, captured)

    _patch_common(mocker, [_record()])
    mocker.patch("people_sync.scrape.run.import_module", return_value=EnrichingModule)
    mocker.patch("people_sync.photos.put_object")
    mocker.patch("people_sync.scrape.run.photos.fetch_url_photo", return_value=None)
    mocker.patch("people_sync.scrape.run.upsert_profile")

    result = run.scrape("testplatform")

    assert result["done"] == 1 and EnrichingModule.seen == ["u1"]


@pytest.mark.parametrize("targets", [None, ["test-tab"]])
@pytest.mark.parametrize("payload", ['{"username":"u1","future_field":[1,2]}', "{broken"])
def test_original_is_durable_before_decode_or_platform_parse(mocker, tmp_path, targets, payload):
    archived = {}
    captured = [{"url": "https://example.test/profile", "body": '{"extra":true}'}]

    class Browser(FakeBrowser):
        def navigate(self, *args, **kwargs):
            super().navigate(*args, **kwargs)
            return {"captured": captured}

        def eval(self, js):
            return payload if js == FakeModule.EXTRACTOR_JS else super().eval(js)

        def watch_blocks(self, *args):
            pass

    browser, _ = _patch_common(mocker, [_record()], browser=Browser())

    def parse(raw, responses):
        assert archived, "parser ran before durable archival"
        raw.clear()  # Even a destructive parser must not change the original.
        raise ExtractError("no-header")

    mocker.patch.object(FakeModule, "parse", side_effect=parse)
    mocker.patch(
        "people_sync.photos.put_object",
        side_effect=lambda key, body, **kw: archived.update({key: body}),
    )
    cache = mocker.patch.object(run, "upsert_profile")
    run.scrape("testplatform", targets=targets, state_path=str(tmp_path / "state.json"))

    assert len(archived) == 1
    saved = json.loads(next(iter(archived.values())))
    assert saved["raw_eval"] == payload
    assert saved["captured"] == captured
    cache.assert_not_called()
    assert browser.closed


@pytest.mark.parametrize("targets", [None, ["test-tab"]])
def test_archive_failure_stops_before_parsing_or_next_navigation(mocker, tmp_path, targets):
    browser, _ = _patch_common(mocker, [_record(), _record(id="testplatform:u2", handle="u2")])
    browser.watch_blocks = lambda *args: None
    parse = mocker.patch.object(FakeModule, "parse")
    cache = mocker.patch.object(run, "upsert_profile")
    mocker.patch("people_sync.photos.put_object", side_effect=OSError("secret storage detail"))

    result = run.scrape("testplatform", targets=targets, state_path=str(tmp_path / "state.json"))

    assert result["halted"] == "raw archive failed"
    assert len(browser.navigated) == 1
    parse.assert_not_called()
    cache.assert_not_called()


def test_readiness_failure_preserves_responses_already_received(mocker, tmp_path):
    captured = [{"url": "https://example.test/profile", "body": "original response"}]
    browser, _ = _patch_common(mocker, [_record()])
    browser.navigate = lambda *args, **kw: {"captured": captured, "dom_ready": False}
    mocker.patch.object(FakeModule, "READY_JS", "READY()", create=True)
    mocker.patch.object(FakeModule, "capture_ready", return_value=False, create=True)
    upload = mocker.patch("people_sync.photos.put_object")
    cache = mocker.patch.object(run, "upsert_profile")

    run.scrape("testplatform", state_path=str(tmp_path / "state.json"))

    assert json.loads(upload.call_args.args[1])["captured"] == captured
    cache.assert_not_called()


def test_shared_stop_after_collection_keeps_archive_without_cache_write(mocker, tmp_path):
    browser, _ = _patch_common(mocker, [_record()])

    def watch_blocks(url, halt, stop):
        browser.halt = halt

    browser.watch_blocks = watch_blocks
    original_parse = FakeModule.parse

    def stop_after_capture(raw, captured):
        browser.halt("HTTP 429")
        return original_parse(raw, captured)

    mocker.patch.object(FakeModule, "parse", side_effect=stop_after_capture)
    upload = mocker.patch("people_sync.photos.put_object")
    cache = mocker.patch.object(run, "upsert_profile")
    result = run.scrape(
        "testplatform", targets=["test-tab"], state_path=str(tmp_path / "state.json")
    )
    assert result["halted"] == "HTTP 429"
    assert json.loads(upload.call_args.args[1])["eval"]["username"] == "u1"
    cache.assert_not_called()


def test_two_snapshots_at_same_timestamp_do_not_overwrite(mocker):
    archived = {}
    mocker.patch("people_sync.lifedata.now_iso", return_value="2026-01-01T00:00:00.000Z")
    mocker.patch(
        "people_sync.photos.put_object",
        side_effect=lambda key, body, **kw: archived.update({key: body}),
    )
    for raw in ('{"bio":"before"}', '{"bio":"after"}'):
        run.photos.archive_profile("testplatform", "testplatform:u1", raw, [])
    assert {json.loads(body)["eval"]["bio"] for body in archived.values()} == {"before", "after"}
