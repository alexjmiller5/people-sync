"""Synthetic capture boundary regressions; no source or estate access."""

import hashlib
import json
import sqlite3

import pytest

from people_sync import captures, cli, photos, replay
from people_sync.scrape import partiful, run, spotify, venmo


@pytest.fixture
def storage(monkeypatch, tmp_path):
    stored, events = {}, []
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(photos, "put_object", lambda k, b, **kw: stored.__setitem__(k, b))

    def get(key):
        events.append("verified-retention")
        return stored[key]

    monkeypatch.setattr(photos, "get_object", get)
    return stored, events


class ProfileBrowser:
    def __init__(self, events):
        self.events = events

    def navigate(self, *args, **kwargs):
        return {"captured": []}

    def wait_for(self, *args):
        return True

    def eval(self, js):
        if js == run.PAGE_TEXT_JS:
            return "Example"
        if js == spotify.EXTRACTOR_JS:
            self.events.append("field-extractor")
            return {"name": "Example", "path": "/user/example", "email": "secret@example.test"}
        self.events.append("source-dom")
        return [{"tag": "h1", "children": [{"text": "Example"}]}]

    def watch_blocks(self, *args):
        pass

    def close(self):
        pass

    def screenshot(self, *args):
        pass


@pytest.mark.parametrize("targets", [None, ["synthetic-tab"]])
@pytest.mark.parametrize("fail", [False, True])
def test_profile_order_and_retained_input_supplies_cache(mocker, tmp_path, storage, targets, fail):
    stored, events = storage
    browser = ProfileBrowser(events)
    mocker.patch.object(run.Browser, "connect", return_value=browser)
    mocker.patch.object(
        run, "_select_records", return_value=[{"id": "spotify:example", "handle": "example"}]
    )
    pacer = mocker.patch.object(run, "Pacer").return_value
    pacer.next_gap.return_value = 0
    original = spotify.parse

    def parse(raw, responses):
        events.append("parse-profile")
        payload = json.loads(next(iter(stored.values())))["payload"]
        assert raw == payload["eval"] and responses == payload["captured"]
        assert "email" not in raw
        return original(raw, responses)

    mocker.patch.object(spotify, "parse", side_effect=parse)
    mocker.patch.object(
        run, "upsert_profile", side_effect=lambda *a, **k: events.append("write-profile")
    )
    if fail:
        mocker.patch.object(photos, "get_object", return_value=b"wrong")
    result = run.scrape("spotify", targets=targets, state_path=str(tmp_path / "pace"))
    assert events.index("source-dom") < events.index("field-extractor")
    if fail:
        assert result["halted"] == "raw archive failed"
        assert "parse-profile" not in events and "write-profile" not in events
    else:
        assert result["done"] == 1
        assert (
            events.index("verified-retention")
            < events.index("parse-profile")
            < events.index("write-profile")
        )
        assert "secret@example.test" not in next(iter(stored.values())).decode()


@pytest.mark.parametrize("fail", [False, True])
def test_partiful_profile_uses_same_order_and_boundary(mocker, storage, fail):
    from tests.test_scrape_partiful import FakeBrowser

    stored, events = storage
    browser = FakeBrowser()
    original_eval = browser.eval

    def evaluate(js):
        if js == partiful.EXTRACTOR_JS:
            events.append("field-extractor")
        elif "people-sync-source-dom" in js:
            events.append("source-dom")
            return [{"tag": "h1", "children": [{"text": "Example"}]}]
        return original_eval(js)

    browser.eval = evaluate
    original = partiful.parse

    def parse(raw, captured=None):
        events.append("parse-profile")
        assert raw == json.loads(next(iter(stored.values())))["payload"]["eval"]
        return original(raw, captured)

    mocker.patch.object(partiful, "parse", side_effect=parse)
    mocker.patch.object(partiful.time, "sleep")
    mocker.patch("people_sync.ledger.upsert", side_effect=lambda *a: events.append("write-ledger"))
    mocker.patch(
        "people_sync.scrape.profile.upsert_profile",
        side_effect=lambda *a, **k: events.append("write-profile"),
    )
    if fail:
        mocker.patch.object(photos, "get_object", return_value=b"wrong")
        with pytest.raises(photos.ArchiveError):
            list(partiful.harvest(browser, limit=1))
        assert "write-profile" not in events and "parse-profile" not in events
    else:
        partiful.ingest_entry(next(partiful.harvest(browser, limit=1))[2])
        assert (
            events.index("verified-retention")
            < events.index("parse-profile")
            < events.index("write-ledger")
        )
    assert events.index("source-dom") < events.index("field-extractor")


def test_legacy_venmo_replay_keeps_join_date_out_of_birthday():
    raw = {
        "id": "123456789012",
        "username": "example_123456789",
        "display_name": "Example Person",
        "profile_picture_url": "https://example.test/avatar.jpg",
        "date_joined": "2020-01-02T03:04:05",
    }
    result = replay.replay_capture({"source": "venmo", "eval": raw, "captured": []})
    assert result["status"] == "ok"
    assert result["profile"]["display_name"] == "Example Person"
    assert result["profile"]["avatar_url"] == raw["profile_picture_url"]
    assert result["profile"]["birthday"] is None
    assert (
        venmo.parse({"id": "123", "username": "example", "displayName": "Web Name"}).display_name
        == "Web Name"
    )


@pytest.mark.parametrize(
    "record_id",
    ["spotify:example%20person", "spotify:bad%252fpath", "spotify:bad\\path", "spotify:bad\npath"],
)
def test_unsafe_avatar_keys_are_opaque_and_invalid_prior_key_not_reused(mocker, record_id):
    key = run._record_key(record_id)
    assert key == hashlib.sha256(record_id.encode()).hexdigest()
    mocker.patch.object(photos, "fetch_url_photo", return_value=b"image")
    put = mocker.patch.object(photos, "put_object")
    sha = hashlib.sha256(b"image").hexdigest()
    new, _ = run._resolve_avatar(
        None,
        "spotify",
        0,
        key,
        "https://example.test/a.jpg",
        "photos/records/spotify/old%20key.jpg",
        sha,
    )
    assert new == f"photos/records/spotify/{key}-{sha[:8]}.jpg"
    assert put.call_count == 1


def test_safe_avatar_keys_stay_compatible():
    assert run._record_key("instagram:example.person") == "instagram_example.person"
    assert run._record_key("facebook:profile.php?id=123") == "facebook_profile.php?id=123"


@pytest.fixture
def records_db(monkeypatch):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        "CREATE TABLE people_sync_records(id,source,handle,name,status,deleted_at,first_seen); CREATE TABLE people_sync_profiles(record_id,avatar_r2_key,avatar_sha256,scraped_at);"
    )
    for rid, source, status, deleted in [
        ("spotify:fresh", "spotify", "matched", None),
        ("spotify:stale", "spotify", "pending", None),
        ("spotify:ignored", "spotify", "ignored", None),
        ("spotify:deleted", "spotify", "pending", "now"),
        ("venmo:other", "venmo", "pending", None),
    ]:
        db.execute(
            "INSERT INTO people_sync_records VALUES (?,?,?,'Example',?,?,0)",
            (rid, source, rid.split(":")[1], status, deleted),
        )
    db.execute("INSERT INTO people_sync_profiles VALUES ('spotify:fresh',NULL,NULL,'2999-01-01')")
    monkeypatch.setattr(run.lifedata, "sql", lambda sql: [dict(r) for r in db.execute(sql)])
    yield db
    db.close()


@pytest.mark.parametrize("rid", ["spotify:fresh", "spotify:stale"])
def test_cli_record_selector_bypasses_staleness_only(
    mocker, monkeypatch, tmp_path, records_db, rid
):
    monkeypatch.setenv("LIFE_HUB_URL", "https://example.test")
    monkeypatch.setenv("LIFE_HUB_TOKEN", "synthetic")
    visited = []
    mocker.patch.object(run.Browser, "connect", return_value=ProfileBrowser([]))
    mocker.patch.object(
        run, "_collect_profile", side_effect=lambda b, m, p, i, r: (visited.append(r["id"]), "key")
    )
    mocker.patch.object(run, "_store_profile")
    pacer = mocker.patch.object(run, "Pacer").return_value
    pacer.next_gap.return_value = 0
    cli.main(["scrape", "spotify", "--record-id", rid, "--state", str(tmp_path / "pace")])
    assert visited == [rid]
    assert [r["id"] for r in run._select_records("spotify")] == ["spotify:stale"]


@pytest.mark.parametrize(
    "rid", ["missing", "venmo:other", "spotify:ignored", "spotify:deleted", ""]
)
def test_invalid_record_selector_fails_before_browser(mocker, tmp_path, records_db, rid):
    connect = mocker.patch.object(run.Browser, "connect")
    with pytest.raises(ValueError, match="record"):
        run.scrape("spotify", record_id=rid, state_path=str(tmp_path / "pace"))
    connect.assert_not_called()


def test_profile_privacy_filters_before_retention_and_validates_replay(storage):
    from people_sync.scrape import snapshot

    stored, _ = storage
    raw = {
        "username": "example_123456789",
        "full_name": "Example Person",
        "bio_lines": ["Artist", "secret@example.test"],
        "links": [
            "https://www.instagram.com/example_123456789/",
            "https://evil.test/example_123456789/",
        ],
        "password": "secret",
    }
    response = {
        "url": "https://www.instagram.com/api/v1/users/web_profile_info/?username=example_123456789",
        "body": json.dumps(
            {
                "data": {
                    "user": {
                        "username": raw["username"],
                        "id": "123456789012",
                        "biography": "Artist",
                        "category_name": "Designer",
                        "email": "secret@example.test",
                        "edge_owner_to_timeline_media": {"edges": [{"caption": "PAYMENT"}]},
                    }
                },
                "session": "SECRET",
            }
        ),
    }
    payload = snapshot.prepare("instagram", "instagram:example_123456789", raw, [response])
    key = photos.archive_profile(
        "instagram",
        "instagram:example_123456789",
        payload.get("raw_eval", payload["eval"]),
        payload["captured"],
        context=payload["context"],
    )
    saved = json.loads(stored[key])
    assert saved["payload"] == payload
    assert payload["eval"]["bio_lines"] == ["Artist"]
    assert payload["eval"]["links"] == ["https://www.instagram.com/example_123456789/"]
    assert "category_name" in payload["captured"][0]["body"]
    assert not any(
        v in stored[key].decode() for v in ["secret@example.test", "password", "PAYMENT", "SECRET"]
    )
    for evil in ["secret@example.test", "123 Main Street", "https://example.test/a?token=secret"]:
        changed = json.loads(stored[key])
        changed["payload"]["eval"]["full_name"] = evil
        changed["payload_sha256"] = hashlib.sha256(captures.encode(changed["payload"])).hexdigest()
        assert replay.replay_capture(changed)["status"] == "invalid"


def test_collection_failure_is_explicit_and_extraction_failure_keeps_safe_responses(
    mocker, storage
):
    from people_sync.scrape import instagram, snapshot

    stored, _ = storage
    browser = mocker.Mock()
    browser.eval.side_effect = RuntimeError("SECRET")
    dom = snapshot.collect(browser, "instagram")
    assert dom["status"] == "partial" and dom["reason"] == "collection-failed"
    assert "SECRET" not in json.dumps(dom)
    browser.navigate.return_value = {
        "captured": [
            {
                "url": "https://www.instagram.com/web_profile_info",
                "body": '{"data":{"user":{"username":"example","biography":"Artist","email":"secret@example.test"}}}',
            }
        ],
        "dom_ready": False,
    }
    with pytest.raises(Exception):
        run._collect_profile(
            browser, instagram, "instagram", 0, {"id": "instagram:example", "handle": "example"}
        )
    payload = json.loads(next(iter(stored.values())))["payload"]
    assert payload["captured"] and "Artist" in payload["captured"][0]["body"]
    assert "secret@example.test" not in json.dumps(payload)
    assert payload["context"]["failure"] == "readiness-failed"
    assert payload["context"]["source_dom"]["reason"] == "collection-not-reached"


@pytest.mark.parametrize(
    "source,path", [("instagram", "/example_123456789/"), ("linkedin", "/in/example-123456789/")]
)
def test_canonical_identity_url_is_typed_without_relaxing_free_text(source, path):
    from people_sync.scrape import snapshot

    url = "https://www." + source + ".com" + path
    assert snapshot.safe_url(url, source) == url
    for bad in [
        url + "?token=x",
        url + "#secret",
        url.replace("https://", "https://user@"),
        url.replace(source + ".com", "evil.test"),
    ]:
        with pytest.raises(ValueError):
            snapshot.safe_url(bad, source)
    with pytest.raises(ValueError):
        captures._check_export_value("123 Main Street")


def test_scoped_linkedin_api_keeps_professional_input_not_session_or_unrelated_users():
    from people_sync.scrape import snapshot

    body = {
        "included": [
            {
                "publicIdentifier": "example-123456789",
                "firstName": "Example",
                "lastName": "Person",
                "headline": "Engineer",
                "summary": "Makes things",
                "positions": [
                    {
                        "companyName": "Example Workshop",
                        "title": "Engineer",
                        "description": "Designs tools",
                    }
                ],
                "emailAddress": "secret@example.test",
            },
            {"publicIdentifier": "other", "firstName": "UNRELATED"},
        ],
        "session": "SECRET",
    }
    p = snapshot.prepare(
        "linkedin",
        "linkedin:example-123456789",
        {
            "name": "Example Person",
            "path": "/in/example-123456789/",
            "connections": "500+ connections",
        },
        [
            {
                "url": "https://www.linkedin.com/voyager/api/graphql?queryId=secret",
                "body": json.dumps(body),
            }
        ],
    )
    assert p["eval"]["connections"] == "500+ connections"
    assert len(p["captured"]) == 1
    assert "Designs tools" in p["captured"][0]["body"]
    assert not any(
        x in json.dumps(p) for x in ["secret@example.test", "UNRELATED", "SECRET", "queryId"]
    )


@pytest.mark.parametrize(
    "source", ["instagram", "facebook", "linkedin", "partiful", "spotify", "strava", "venmo"]
)
def test_forbidden_extracted_fields_never_reach_new_profile_captures(source, storage):
    stored, _ = storage
    key = photos.archive_profile(
        source,
        source + ":example",
        {
            "email": "secret@example.test",
            "phone": "2125550123",
            "address": "123 Main Street",
            "payments": ["secret"],
        },
        [],
    )
    payload = json.loads(stored[key])["payload"]
    assert payload["eval"] == {}
    assert "unknown-fields" in payload["context"]["exclusions"]


def test_dom_collector_executes_scoped_walk_and_filters_visible_contact_text():
    import subprocess
    from people_sync.scrape import snapshot

    # The fake DOM honors descendant exclusions and visibility. No HTML package needed.
    script = r"""
    const text = value => ({nodeType:3,textContent:value});
    const el = (tag, children=[], attrs={}, hidden=false, excluded=false) => ({
      nodeType:1,tagName:tag.toUpperCase(),childNodes:children,
      closest:()=>excluded ? {} : null,
      checkVisibility:opts=> !hidden && opts.opacityProperty && opts.visibilityProperty,
      getAttribute:k=>attrs[k] || null
    });
    const root = el('header', [text('Example Person'),el('span',[text('Useful unparsed biography')]),
      el('span',[text('secret@example.test')]),el('script',[text('SECRET')],{},false,true),
      el('input',[],{value:'PASSWORD'},false,true),el('span',[text('HIDDEN')],{},true),
      el('a',[text('website')],{href:'https://example.test/?token=SECRET'}),
      el('img',[],{src:'https://example.test/avatar.jpg',onload:'SECRET','data-token':'SECRET'})]);
    globalThis.location={href:'https://www.instagram.com/example/'};
    globalThis.document={querySelectorAll:selector=> {
      if(selector!=='main header')throw Error('scope escaped');return [root];
    }};
    """

    class Browser:
        def eval(self, js):
            return json.loads(
                subprocess.check_output(
                    ["node", "-e", script + "console.log(JSON.stringify(" + js + "));"], text=True
                )
            )

    result = snapshot.collect(Browser(), "instagram")
    assert result["status"] == "partial"
    assert "Useful unparsed biography" in json.dumps(result)
    assert not any(
        x in json.dumps(result)
        for x in ["secret@example.test", "SECRET", "PASSWORD", "HIDDEN", "onload", "data-token"]
    )


def test_venmo_collection_never_reads_dom_or_network(mocker):
    from people_sync.scrape import snapshot

    browser = mocker.Mock()
    result = snapshot.collect(browser, "venmo")
    browser.eval.assert_not_called()
    assert result["nodes"] == [] and result["reason"] == "selected-otherUser-only"


def test_dom_metadata_cannot_claim_complete_failed_collection(storage):
    from people_sync.scrape import snapshot

    dom = snapshot.collect(ProfileBrowser([]), "spotify")
    dom.update(nodes=[], status="success", reason="collection-failed")
    with pytest.raises(photos.ArchiveError):
        photos.archive_profile("spotify", "spotify:example", {}, [], context={"source_dom": dom})


@pytest.mark.parametrize(
    "raw",
    [
        '{"name":"secret@example.test","name":"Example","path":"/user/example"}',
        '{"token":"SECRET", broken',
        '{"name":"Example", "session":"SECRET",',
    ],
)
def test_hidden_or_malformed_extractor_fields_cannot_survive_in_raw_eval(raw, storage):
    stored, _ = storage
    key = photos.archive_profile("spotify", "spotify:example", raw, [])
    assert (
        "SECRET" not in stored[key].decode() and "secret@example.test" not in stored[key].decode()
    )
    assert json.loads(stored[key])["payload"]["context"]["exclusions"]


def test_policy_cannot_be_removed_from_dom_capture(storage):
    from people_sync.scrape import snapshot

    stored, _ = storage
    key = photos.archive_profile(
        "spotify",
        "spotify:example",
        {},
        [],
        context={"source_dom": snapshot.collect(ProfileBrowser([]), "spotify")},
    )
    c = json.loads(stored[key])
    c["exclusions"] = []
    with pytest.raises(ValueError):
        captures.validate(c)


def test_extractor_exception_retains_dom_and_marks_partial(mocker, storage):
    stored, events = storage
    browser = ProfileBrowser(events)
    evaluate = browser.eval

    def broken(js):
        if js == spotify.EXTRACTOR_JS:
            raise RuntimeError("SECRET")
        return evaluate(js)

    browser.eval = broken
    with pytest.raises(RuntimeError):
        run._collect_profile(
            browser, spotify, "spotify", 0, {"id": "spotify:example", "handle": "example"}
        )
    c = json.loads(next(iter(stored.values())))
    assert c["completeness"] == "partial"
    assert c["payload"]["context"]["source_dom"]["nodes"]
    assert c["payload"]["context"]["failure"] == "extraction-failed"
    assert "SECRET" not in json.dumps(c)


@pytest.mark.parametrize(
    "value", ["https://example.test/image.%25jpg", "https://example.test/image.%252f"]
)
def test_avatar_extension_cannot_reintroduce_invalid_key(mocker, value):
    mocker.patch.object(photos, "fetch_url_photo", return_value=b"image")
    mocker.patch.object(photos, "put_object")
    key, _ = run._resolve_avatar(None, "spotify", 0, "spotify_example", value, None, None)
    assert key.endswith(".jpg") and "%" not in key
