import base64
import hashlib
import json

import httpx

from contact_sync import photos


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


def test_store_photo_dedupes_existing_sha(mocker):
    sql = mocker.patch("contact_sync.lifedata.sql", return_value=[{"id": "existing"}])
    insert = mocker.patch("contact_sync.lifedata.insert")
    put = mocker.patch("contact_sync.photos.httpx.put")

    result = photos.store_photo("p1", "instagram", b"image-bytes", "jpg")

    assert result is None
    put.assert_not_called()
    insert.assert_not_called()
    sql.assert_called_once()


def test_store_photo_uploads_new_sha(mocker, monkeypatch):
    monkeypatch.setenv("CF_API_TOKEN", "test-token")
    mocker.patch("contact_sync.lifedata.sql", return_value=[])
    insert = mocker.patch("contact_sync.lifedata.insert")
    mocker.patch(
        "contact_sync.photos.httpx.get",
        return_value=_Resp(json_data={"result": [{"id": "acct1"}]}),
    )
    put = mocker.patch("contact_sync.photos.httpx.put", return_value=_Resp())

    image = b"image-bytes"
    sha8 = hashlib.sha256(image).hexdigest()[:8]

    result = photos.store_photo("p1", "instagram", image, "jpg")

    expected_key = f"photos/people/p1/instagram-{sha8}.jpg"
    assert result == expected_key
    assert put.call_args.args[0].endswith(f"/objects/{expected_key}")
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
    monkeypatch.setenv("CF_API_TOKEN", "test-token")
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

    mocker.patch("contact_sync.lifedata.sql", side_effect=fake_sql)
    mocker.patch("contact_sync.lifedata.insert", side_effect=fake_insert)
    mocker.patch(
        "contact_sync.photos.httpx.get",
        return_value=_Resp(json_data={"result": [{"id": "acct1"}]}),
    )
    put = mocker.patch("contact_sync.photos.httpx.put", return_value=_Resp())

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
        "contact_sync.photos.httpx.get",
        return_value=_Resp(content=b"imgdata", headers={"content-type": "image/jpeg"}),
    )
    assert photos.fetch_url_photo("https://example.com/a.jpg") == b"imgdata"


def test_fetch_url_photo_returns_none_on_non_200(mocker):
    mocker.patch(
        "contact_sync.photos.httpx.get",
        return_value=_Resp(status_code=404, headers={"content-type": "image/jpeg"}),
    )
    assert photos.fetch_url_photo("https://example.com/a.jpg") is None


def test_fetch_url_photo_returns_none_on_non_image_content_type(mocker):
    mocker.patch(
        "contact_sync.photos.httpx.get",
        return_value=_Resp(content=b"<html>", headers={"content-type": "text/html"}),
    )
    assert photos.fetch_url_photo("https://example.com/a.jpg") is None


def test_fetch_url_photo_returns_none_on_request_error(mocker):
    mocker.patch(
        "contact_sync.photos.httpx.get",
        side_effect=httpx.ConnectError("boom"),
    )
    assert photos.fetch_url_photo("https://example.com/a.jpg") is None


def test_fetch_google_photo_uses_person_raw_and_url(mocker):
    person = {
        "resourceName": "people/c1",
        "photos": [{"url": "https://example.com/c1.jpg", "metadata": {"primary": True}}],
    }
    mocker.patch("contact_sync.sources._run", return_value=json.dumps(person))
    mocker.patch("contact_sync.photos.fetch_url_photo", return_value=b"bytes")
    assert photos.fetch_google_photo("people/c1") == b"bytes"


def test_fetch_google_photo_returns_none_without_photo(mocker):
    mocker.patch(
        "contact_sync.sources._run",
        return_value=json.dumps({"resourceName": "people/c1"}),
    )
    assert photos.fetch_google_photo("people/c1") is None


def test_fetch_google_photo_returns_none_on_raw_fetch_failure(mocker):
    mocker.patch("contact_sync.sources._run", side_effect=RuntimeError("boom"))
    assert photos.fetch_google_photo("people/c1") is None


def test_fetch_apple_photo_extracts_base64_photo(mocker):
    payload = base64.b64encode(b"fake-image-bytes-0123456789").decode()
    vcard = f"BEGIN:VCARD\nPHOTO;ENCODING=b;TYPE=JPEG:{payload[:20]}\n {payload[20:]}\nEND:VCARD\n"
    mocker.patch("contact_sync.sources._run", return_value=vcard)
    assert photos.fetch_apple_photo("XXXX:ABPerson") == base64.b64decode(payload)


def test_fetch_apple_photo_returns_none_without_photo(mocker):
    mocker.patch("contact_sync.sources._run", return_value="BEGIN:VCARD\nEND:VCARD\n")
    assert photos.fetch_apple_photo("XXXX:ABPerson") is None


def test_fetch_apple_photo_returns_none_on_vcard_fetch_failure(mocker):
    mocker.patch("contact_sync.sources._run", side_effect=RuntimeError("boom"))
    assert photos.fetch_apple_photo("XXXX:ABPerson") is None
