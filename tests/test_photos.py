import base64
import hashlib
import json
from urllib.parse import unquote, urlsplit

import pytest

import httpx

from people_sync import photos


class _Resp:
    def __init__(self, json_data=None, status_code=200, content=b"", headers=None):
        self.status_code = status_code
        self._json = json_data
        self.content = content
        self.headers = headers or {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)


def _hub_put(url, **kwargs):
    """The hub decodes the request path exactly once and replies with the key it stored."""
    return _Resp(json_data={"key": unquote(urlsplit(url).path.removeprefix("/v1/files/"))})


def test_store_photo_dedupes_existing_sha(mocker):
    sql = mocker.patch("people_sync.somadata.sql", return_value=[{"id": "existing"}])
    insert = mocker.patch("people_sync.somadata.insert")
    put = mocker.patch("people_sync.photos.httpx.put")

    result = photos.store_photo("p1", "instagram", b"image-bytes", "jpg")

    assert result is None
    put.assert_not_called()
    insert.assert_not_called()
    sql.assert_called_once()


def test_store_photo_uploads_new_sha(mocker, monkeypatch):
    monkeypatch.setenv("SOMA_HUB_URL", "https://hub.test")
    monkeypatch.setenv("SOMA_HUB_TOKEN", "test-token")
    mocker.patch("people_sync.somadata.sql", return_value=[])
    insert = mocker.patch("people_sync.somadata.insert")
    mocker.patch(
        "people_sync.photos.httpx.get",
        return_value=_Resp(json_data={"result": [{"id": "acct1"}]}),
    )
    put = mocker.patch("people_sync.photos.httpx.put", side_effect=_hub_put)

    image = b"image-bytes"
    sha8 = hashlib.sha256(image).hexdigest()[:8]

    result = photos.store_photo("p1", "instagram", image, "jpg")

    expected_key = f"photos/people/p1/instagram-{sha8}.jpg"
    assert result == expected_key
    assert put.call_args.args[0].endswith(f"/v1/files/{expected_key}")
    assert put.call_args.kwargs["content"] == image

    table, rows = insert.call_args.args
    assert table == "person_photos"
    row = rows[0]
    assert row["person_id"] == "p1"
    assert row["platform"] == "instagram"
    assert row["r2_key"] == expected_key
    assert row["sha256"] == hashlib.sha256(image).hexdigest()
    assert "fetched_at" in row


def test_store_photo_dedupe_is_scoped_per_person(mocker, monkeypatch):
    monkeypatch.setenv("SOMA_HUB_URL", "https://hub.test")
    monkeypatch.setenv("SOMA_HUB_TOKEN", "test-token")
    stored_rows: list[tuple[str, str]] = []  # simulates person_photos: (person_id, sha256)

    def fake_sql(query):
        # A real dedupe query must scope by person_id, not just sha256 - if the
        # person_id clause is ever dropped from store_photo's query, this stops
        # matching and the assertions below fail.
        return [
            {"id": "x"}
            for pid, sha in stored_rows
            if f"person_id = '{pid}'" in query and f"sha256 = '{sha}'" in query
        ]

    def fake_insert(table, rows):
        for row in rows:
            stored_rows.append((row["person_id"], row["sha256"]))

    mocker.patch("people_sync.somadata.sql", side_effect=fake_sql)
    mocker.patch("people_sync.somadata.insert", side_effect=fake_insert)
    mocker.patch(
        "people_sync.photos.httpx.get",
        return_value=_Resp(json_data={"result": [{"id": "acct1"}]}),
    )
    put = mocker.patch("people_sync.photos.httpx.put", side_effect=_hub_put)

    image = b"same-image-bytes"

    first = photos.store_photo("p1", "instagram", image, "jpg")
    dup_same_person = photos.store_photo("p1", "instagram", image, "jpg")
    other_person = photos.store_photo("p2", "instagram", image, "jpg")

    assert first is not None
    assert dup_same_person is None
    assert other_person is not None
    assert put.call_count == 2


def test_fetch_url_photo_returns_bytes_on_200_image(mocker):
    mocker.patch(
        "people_sync.photos.httpx.get",
        return_value=_Resp(content=b"imgdata", headers={"content-type": "image/jpeg"}),
    )
    assert photos.fetch_url_photo("https://example.com/a.jpg") == b"imgdata"


def test_fetch_url_photo_returns_none_on_non_200(mocker):
    mocker.patch(
        "people_sync.photos.httpx.get",
        return_value=_Resp(status_code=404, headers={"content-type": "image/jpeg"}),
    )
    assert photos.fetch_url_photo("https://example.com/a.jpg") is None


def test_scrape_photo_rate_limit_raises_instead_of_retrying(mocker):
    response = httpx.Response(429, request=httpx.Request("GET", "https://example.invalid/a.jpg"))
    get = mocker.patch.object(httpx, "get", return_value=response)
    with pytest.raises(httpx.HTTPStatusError):
        photos.fetch_url_photo("https://example.invalid/a.jpg", halt_on_block=True)
    assert get.call_count == 1


def test_fetch_url_photo_returns_none_on_non_image_content_type(mocker):
    mocker.patch(
        "people_sync.photos.httpx.get",
        return_value=_Resp(content=b"<html>", headers={"content-type": "text/html"}),
    )
    assert photos.fetch_url_photo("https://example.com/a.jpg") is None


def test_fetch_url_photo_returns_none_on_request_error(mocker):
    mocker.patch(
        "people_sync.photos.httpx.get",
        side_effect=httpx.ConnectError("boom"),
    )
    assert photos.fetch_url_photo("https://example.com/a.jpg") is None


def test_fetch_google_photo_uses_person_raw_and_url(mocker):
    person = {
        "resourceName": "people/c1",
        "photos": [{"url": "https://example.com/c1.jpg", "metadata": {"primary": True}}],
    }
    mocker.patch("people_sync.sources._run", return_value=json.dumps(person))
    mocker.patch("people_sync.photos.fetch_url_photo", return_value=b"bytes")
    assert photos.fetch_google_photo("people/c1") == b"bytes"


def test_fetch_google_photo_returns_none_without_photo(mocker):
    mocker.patch(
        "people_sync.sources._run",
        return_value=json.dumps({"resourceName": "people/c1"}),
    )
    assert photos.fetch_google_photo("people/c1") is None


def test_fetch_google_photo_returns_none_on_raw_fetch_failure(mocker):
    mocker.patch("people_sync.sources._run", side_effect=RuntimeError("boom"))
    assert photos.fetch_google_photo("people/c1") is None


def test_fetch_apple_photo_extracts_base64_photo(mocker):
    payload = base64.b64encode(b"fake-image-bytes-0123456789").decode()
    vcard = f"BEGIN:VCARD\nPHOTO;ENCODING=b;TYPE=JPEG:{payload[:20]}\n {payload[20:]}\nEND:VCARD\n"
    mocker.patch("people_sync.sources._run", return_value=vcard)
    assert photos.fetch_apple_photo("XXXX:ABPerson") == base64.b64decode(payload)


def test_fetch_apple_photo_returns_none_without_photo(mocker):
    mocker.patch("people_sync.sources._run", return_value="BEGIN:VCARD\nEND:VCARD\n")
    assert photos.fetch_apple_photo("XXXX:ABPerson") is None


def test_fetch_apple_photo_returns_none_on_vcard_fetch_failure(mocker):
    mocker.patch("people_sync.sources._run", side_effect=RuntimeError("boom"))
    assert photos.fetch_apple_photo("XXXX:ABPerson") is None


def test_object_api_uses_only_scoped_life_credential(mocker, monkeypatch):
    monkeypatch.setenv("SOMA_HUB_URL", "https://hub.test/")
    monkeypatch.setenv("SOMA_HUB_TOKEN", "client-token")
    put = mocker.patch("people_sync.photos.httpx.put", side_effect=_hub_put)
    get = mocker.patch("people_sync.photos.httpx.get", return_value=_Resp(content=b"raw"))
    photos.put_object("profiles/source/a b.json", b"raw", "application/json")
    assert put.call_args.args[0] == "https://hub.test/v1/files/profiles/source/a%20b.json"
    assert put.call_args.kwargs["headers"]["Authorization"] == "Bearer client-token"
    assert photos.get_object("profiles/source/a b.json") == b"raw"
    assert get.call_count == 1
    assert get.call_args.args[0] == put.call_args.args[0]


@pytest.mark.parametrize("raw", [' {"name":"Example","path":"/user/example"} ', "{broken", None])
def test_archive_profile_retains_real_verified_envelope(monkeypatch, tmp_path, raw):
    from people_sync import captures

    stored = {}
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(photos, "put_object", lambda k, b, **kw: stored.__setitem__(k, b))
    monkeypatch.setattr(photos, "get_object", stored.__getitem__)
    context = {"dom": "<h1>Example</h1>", "boundary": "profile-header"}
    key = photos.archive_profile("spotify", "spotify:example", raw, [], context=context)
    c = captures.validate(json.loads(stored[key]))
    assert c["record_id"] == "spotify:example" and c["completeness"] == "extracted-only"
    assert c["payload"]["context"] == {"exclusions": ["unknown-fields"]}
    assert c["payload"]["eval"] == (json.loads(raw) if raw and raw != "{broken" else raw)
    if isinstance(raw, str):
        assert c["payload"]["raw_eval"] == raw
    assert (
        tmp_path / "people-sync" / "captures" / f"{c['capture_id']}.json"
    ).read_bytes() == stored[key]


@pytest.mark.parametrize("phase", ["encode", "upload", "readback"])
def test_archive_profile_failure_hides_exception_details(monkeypatch, tmp_path, phase):
    secret = "Bearer synthetic-secret"

    def failed(*args, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(photos, "put_object", failed if phase == "upload" else lambda *a, **k: None)
    monkeypatch.setattr(photos, "get_object", failed)
    raw = object() if phase == "encode" else {}
    with pytest.raises(photos.ArchiveError) as exc:
        photos.archive_profile("spotify", "spotify:example", raw, [])
    assert str(exc.value) == "raw archive failed"
    assert secret not in str(exc.value) and exc.value.__suppress_context__


def test_nonstandard_structured_json_has_explicit_privacy_exclusion(monkeypatch, tmp_path):
    stored = {}
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(photos, "put_object", lambda k, b, **kw: stored.__setitem__(k, b))
    monkeypatch.setattr(photos, "get_object", stored.__getitem__)
    raw = '{"count":NaN}'
    key = photos.archive_profile("spotify", "spotify:example", raw, [])
    payload = json.loads(stored[key])["payload"]
    assert payload["eval"] is None and "raw_eval" not in payload
    assert payload["context"]["exclusions"] == ["unsafe-or-invalid-values"]


@pytest.mark.parametrize("readback_ok", [True, False])
def test_scrape_caller_parses_only_after_verified_archive(
    monkeypatch, mocker, tmp_path, readback_ok
):
    from types import SimpleNamespace
    from people_sync.scrape import run, spotify

    stored, parsed = {}, []
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(photos, "put_object", lambda k, b, **kw: stored.__setitem__(k, b))
    monkeypatch.setattr(photos, "get_object", lambda k: stored[k] if readback_ok else b"wrong")

    def parse(raw, captured):
        assert len(list((tmp_path / "people-sync" / "captures").glob("*.json"))) == 1
        parsed.append(raw)
        return spotify.parse(raw, captured)

    module = SimpleNamespace(URL=spotify.URL, CAPTURE=[], EXTRACTOR_JS="synthetic", parse=parse)
    browser = mocker.Mock()
    browser.navigate.return_value = {"captured": []}
    browser.eval.side_effect = [
        "Example",
        [{"tag": "h1", "children": [{"text": "Example"}]}],
        '{"name":"Example","path":"/user/example"}',
    ]
    record = {"id": "spotify:example", "handle": "example"}
    if readback_ok:
        profile, key, avatar_url = run._collect_profile(browser, module, "spotify", 0, record)
        assert avatar_url is None
        assert profile.record_id == "spotify:example" and key in stored
    else:
        with pytest.raises(photos.ArchiveError):
            run._collect_profile(browser, module, "spotify", 0, record)
        assert not parsed


@pytest.mark.parametrize(
    "key",
    [
        "photos/records/facebook/facebook_profile.php?id=123-abcd1234.jpg",
        "profiles/spotify/spotify_name#1/2026-09-09T19:48:54.159Z.json",
        "photos/records/linkedin/linkedin_m\u00e9lanie-abcd1234.jpg",
        "profiles/source/a b+c.json",
    ],
)
def test_upload_stores_under_the_exact_key(mocker, monkeypatch, key):
    monkeypatch.setenv("SOMA_HUB_URL", "https://hub.test")
    monkeypatch.setenv("SOMA_HUB_TOKEN", "client-token")
    put = mocker.patch("people_sync.photos.httpx.put", side_effect=_hub_put)
    photos.put_object(key, b"raw")
    assert _hub_put(put.call_args.args[0]).json()["key"] == key


def test_upload_refuses_a_reply_naming_another_key(mocker, monkeypatch):
    """A reference must name the object the hub stored, never a near-miss spelling."""
    monkeypatch.setenv("SOMA_HUB_URL", "https://hub.test")
    monkeypatch.setenv("SOMA_HUB_TOKEN", "client-token")
    mocker.patch(
        "people_sync.photos.httpx.put",
        return_value=_Resp(json_data={"key": "profiles/spotify/spotify_name"}, status_code=201),
    )
    with pytest.raises(ValueError):
        photos.put_object("profiles/spotify/spotify_name#1/2026-09-09T19:48:54.159Z.json", b"raw")


@pytest.mark.parametrize(
    ("record_id", "segment"),
    [
        ("facebook:profile.php?id=123", "facebook_id_123"),
        ("facebook:example_o'name", "facebook_example_o_name"),
        ("instagram:example.person", "instagram_example_person"),
        ("linkedin:example-person-1a2b", "linkedin_example-person-1a2b"),
        ("spotify:example%20person", "spotify_example_20person"),
        ("partiful:AbC123", "partiful_abc123"),
    ],
)
def test_a_record_key_is_a_plain_path_segment(record_id, segment):
    assert photos.record_key(record_id) == segment


def test_a_record_id_with_nothing_to_slug_keys_by_its_hash():
    assert photos.record_key("é:ü") == hashlib.sha256("é:ü".encode()).hexdigest()


@pytest.mark.parametrize(
    "key",
    [
        "photos/records/facebook/facebook_profile.php?id=1-abcd1234.jpg",
        "photos/records/facebook/facebook_o'name-abcd1234.jpg",
        'photos/records/facebook/facebook_o"name-abcd1234.jpg',
        "photos/records/facebook/a=b-abcd1234.jpg",
        "photos/records/facebook/a?b-abcd1234.jpg",
        "photos/records/facebook/a#b-abcd1234.jpg",
        "photos/records/spotify/old%20key.jpg",
        "photos/records/spotify/bad\\key.jpg",
        "photos//a.jpg",
        "",
        None,
    ],
)
def test_a_key_a_client_cannot_request_is_not_safe(key):
    assert not photos.safe_key(key)


def test_existing_dotted_keys_stay_safe():
    assert photos.safe_key("photos/records/instagram/instagram_example.person-abcd1234.jpg")


@pytest.fixture
def estate(monkeypatch):
    """person_photos, people_sync_profiles and provenance in sqlite, behind somadata."""
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        "CREATE TABLE person_photos(id, person_id, platform, r2_key, sha256, deleted_at);"
        "CREATE TABLE people_sync_profiles(record_id, avatar_r2_key, avatar_sha256, deleted_at);"
        "CREATE TABLE provenance(id, from_kind, from_ref, to_kind, to_ref, field, rel, detail, asserted_by);"
    )
    monkeypatch.setattr(photos.somadata, "sql", lambda q: [dict(r) for r in db.execute(q)])

    def insert(table, rows):
        for row in rows:
            cols = ",".join(row)
            db.execute(
                f"INSERT INTO {table}({cols}) VALUES ({','.join('?' * len(row))})",
                list(row.values()),
            )

    monkeypatch.setattr(photos.somadata, "insert", insert)
    monkeypatch.setenv("PEOPLE_SYNC_ASSERTED_BY", "agent:test-session")
    yield db
    db.close()


OLD = "photos/records/facebook/facebook_profile.php?id=123-{sha8}.jpg"
NEW = "photos/records/facebook/facebook_id_123-{sha8}.jpg"


def _seed(db, image=b"image"):
    sha = hashlib.sha256(image).hexdigest()
    old, new = OLD.format(sha8=sha[:8]), NEW.format(sha8=sha[:8])
    db.execute(
        "INSERT INTO person_photos VALUES ('photo:p1:r1','p1','facebook',?,?,NULL)", (old, sha)
    )
    db.execute(
        "INSERT INTO person_photos VALUES ('photo:p2:r2','p2','facebook','photos/records/facebook/facebook_ok-1.jpg','x',NULL)"
    )
    db.execute(
        "INSERT INTO people_sync_profiles VALUES ('facebook:profile.php?id=123',?,?,NULL)",
        (old, sha),
    )
    db.execute(
        "INSERT INTO people_sync_profiles VALUES ('facebook:gone',?,?,'2026-01-01')", (old, sha)
    )
    return old, new, sha


def _store(mocker, objects):
    mocker.patch.object(photos, "get_object", side_effect=lambda k: objects[k])
    mocker.patch.object(
        photos, "put_object", side_effect=lambda k, d, content_type=None: objects.__setitem__(k, d)
    )


def test_rekey_dry_run_lists_the_moves_and_touches_nothing(mocker, estate):
    old, new, sha = _seed(estate)
    objects = {old: b"image"}
    _store(mocker, objects)

    result = photos.rekey(apply_writes=False)

    assert result["moves"] == [
        {
            "from": old,
            "to": new,
            "sha256": sha,
            "rows": [
                {"table": "person_photos", "id": "photo:p1:r1", "field": "r2_key"},
                {
                    "table": "people_sync_profiles",
                    "id": "facebook:profile.php?id=123",
                    "field": "avatar_r2_key",
                },
            ],
        }
    ]
    assert list(objects) == [old]
    assert estate.execute("SELECT count(*) FROM provenance").fetchone()[0] == 0


def test_rekey_copies_verifies_and_repoints_every_live_row_with_an_edge(mocker, estate):
    old, new, sha = _seed(estate)
    objects = {old: b"image"}
    _store(mocker, objects)

    photos.rekey(apply_writes=True)

    assert objects == {old: b"image", new: b"image"}  # the old object stays
    assert (
        estate.execute("SELECT r2_key FROM person_photos WHERE id='photo:p1:r1'").fetchone()[0]
        == new
    )
    keys = dict(
        estate.execute("SELECT record_id, avatar_r2_key FROM people_sync_profiles").fetchall()
    )
    assert keys == {"facebook:profile.php?id=123": new, "facebook:gone": old}
    edges = [dict(r) for r in estate.execute("SELECT * FROM provenance ORDER BY to_kind")]
    assert [
        (
            e["from_kind"],
            e["from_ref"],
            e["to_kind"],
            e["to_ref"],
            e["field"],
            e["rel"],
            e["asserted_by"],
        )
        for e in edges
    ] == [
        (
            "takeout",
            new,
            "people_sync_profiles",
            "facebook:profile.php?id=123",
            "avatar_r2_key",
            "evidence_of",
            "agent:test-session",
        ),
        (
            "takeout",
            new,
            "person_photos",
            "photo:p1:r1",
            "r2_key",
            "evidence_of",
            "agent:test-session",
        ),
    ]
    assert json.loads(edges[0]["detail"])["cue"].startswith("safe photo key migration ")

    assert photos.rekey(apply_writes=True)["moves"] == []  # idempotent


def test_rekey_refuses_bytes_that_do_not_match_their_sha256(mocker, estate):
    old, new, _ = _seed(estate)
    objects = {old: b"other bytes"}
    _store(mocker, objects)

    with pytest.raises(ValueError):
        photos.rekey(apply_writes=True)

    assert new not in objects
    assert (
        estate.execute("SELECT r2_key FROM person_photos WHERE id='photo:p1:r1'").fetchone()[0]
        == old
    )


def test_rekey_refuses_a_copy_that_does_not_read_back(mocker, estate):
    old, new, _ = _seed(estate)
    objects = {old: b"image"}
    mocker.patch.object(
        photos, "get_object", side_effect=lambda k: objects[k] if k == old else b"torn"
    )
    mocker.patch.object(photos, "put_object")

    with pytest.raises(ValueError):
        photos.rekey(apply_writes=True)

    assert (
        estate.execute("SELECT r2_key FROM person_photos WHERE id='photo:p1:r1'").fetchone()[0]
        == old
    )
    assert estate.execute("SELECT count(*) FROM provenance").fetchone()[0] == 0


def test_rekey_after_a_crash_before_the_row_update_adds_no_second_edge(mocker, estate):
    old, new, _ = _seed(estate)
    objects = {old: b"image"}
    _store(mocker, objects)
    real_update = photos.ledger.batch_update
    mocker.patch.object(photos.ledger, "batch_update", side_effect=RuntimeError("crash"))
    with pytest.raises(RuntimeError):
        photos.rekey(apply_writes=True)
    mocker.patch.object(photos.ledger, "batch_update", side_effect=real_update)

    photos.rekey(apply_writes=True)

    assert estate.execute("SELECT count(*) FROM provenance").fetchone()[0] == 2
    assert (
        estate.execute("SELECT r2_key FROM person_photos WHERE id='photo:p1:r1'").fetchone()[0]
        == new
    )
