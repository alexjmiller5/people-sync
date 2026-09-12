"""Synthetic incremental source observations; no browser or storage service."""

import json
from unittest.mock import Mock

import pytest

from people_sync import captures, photos, replay
from people_sync.scrape import facebook, partiful, spotify, strava


@pytest.fixture(autouse=True)
def no_estate(mocker):
    mocker.patch("people_sync.lifedata.sql", return_value=[])
    mocker.patch(
        "people_sync.lifedata.insert", side_effect=AssertionError("unexpected estate write")
    )


@pytest.fixture
def retained(mocker, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    stored = {}
    mocker.patch(
        "people_sync.photos.put_object", side_effect=lambda k, b, **kw: stored.update({k: b})
    )
    mocker.patch("people_sync.photos.get_object", side_effect=stored.__getitem__)
    mocker.patch("time.sleep")
    return stored


def observations(stored):
    return [
        (k, json.loads(b)["payload"]) for k, b in stored.items() if json.loads(b)["kind"] == "list"
    ]


def test_facebook_retains_duplicates_and_virtualized_rows_before_scroll(retained):
    first = [{"handle": "one", "name": "Example One"}] * 2
    second = [{"handle": "two", "name": "Example Two"}]
    browser = Mock()
    browser.eval.side_effect = [first, second]
    browser.scroll.side_effect = lambda *_: (
        observations(retained)[0][1]["entries"] == first
        or pytest.fail("not retained before scroll")
    )
    rows = facebook.list_friends(browser, max_scrolls=1, settle_s=0)
    pages = observations(retained)
    assert pages[0][1]["entries"] == first
    assert pages[1][1]["ordinal"] == 1
    assert pages[-1][1]["complete"] is False
    assert pages[-1][1]["reason"] == "scroll-limit"
    assert len(rows[0]["capture_refs"]) == 2
    proposal = facebook.assign_handles(
        rows, [{"id": "facebook:example_one", "name": "Example One", "handle": None}]
    )[0]
    assert proposal["capture_refs"] == rows[0]["capture_refs"]
    assert proposal["capture_refs"][0]["capture_key"] == pages[0][0]


def spotify_browser():
    b = Mock()
    b.wait_for.return_value = True
    one = {"href": "/user/example123456789", "name": "Example One"}
    two = {"href": "/artist/artist", "name": "Example Artist"}
    b.eval.side_effect = [
        "/user/owner",
        {"followers": 2, "following": 1},
        [one, one],
        True,
        [two],
        [one],
    ]
    return b, one, two


def test_spotify_retains_artist_duplicates_and_both_scope_references(retained, mocker):
    b, one, two = spotify_browser()
    rows = spotify.list_users(b)
    pages = observations(retained)
    assert pages[0][1]["entries"] == [one, one]
    assert pages[1][1]["entries"] == [two]
    assert pages[1][1]["ordinal"] == 1
    assert pages[-1][1]["complete"] is True
    assert len(rows) == 1
    write = mocker.patch("people_sync.ledger.upsert")
    spotify.ingest_entries(rows)
    record = write.call_args.args[0][0]
    assert {r["scope"] for r in record.capture_refs} == {"followers", "following"}
    assert len(record.capture_refs) == 3
    assert "capture_refs" not in record.raw


def test_strava_partial_lists_never_infer_false_and_keep_all_refs(retained, mocker):
    b = Mock()
    row = {"id": "123456789", "name": "Example One", "location": "Exampleville"}
    b.eval.side_effect = ["999", [row, row], [row, {"id": "2", "name": "Example Two"}]]
    rows = strava.list_athletes(b)
    pages = observations(retained)
    assert pages[0][1]["entries"] == [row, row]
    assert pages[1][1]["ordinal"] == 1
    assert all(p["complete"] is False for _, p in pages)
    assert rows[1]["follows_me"] is None
    write = mocker.patch("people_sync.ledger.upsert")
    strava.ingest_entries(rows)
    record = write.call_args.args[0][0]
    assert len(record.capture_refs) == 3
    assert "capture_refs" not in record.raw
    assert "seen[m[1]]" not in strava.LIST_JS
    assert ", a[href*=" not in strava.LIST_JS


def test_partiful_retains_each_row_before_click_and_keeps_profile_ref(retained, mocker):
    from tests.test_scrape_partiful import FakeBrowser

    b = FakeBrowser()
    click = b.click

    def checked(selector):
        if selector.startswith("#ps_row_"):
            assert observations(retained)
            assert observations(retained)[-1][1]["entries"][0]["name"].startswith("Person")
        click(selector)

    b.click = checked
    entries = list(partiful.harvest(b, limit=1))
    pages = observations(retained)
    assert pages[0][1]["entry_ordinals"] == [0]
    assert pages[-1][1]["reason"] == "row-limit"
    assert pages[-1][1]["complete"] is False
    entry = entries[0][2]
    write = mocker.patch("people_sync.ledger.upsert")
    mocker.patch("people_sync.scrape.profile.upsert_profile")
    partiful.ingest_entry(entry)
    record = write.call_args.args[0][0]
    assert record.capture_key == entry["raw_r2_key"]
    assert record.capture_refs[0]["capture_key"] == pages[0][0]
    assert "_avatar_url" not in entry
    assert "capture_refs" not in record.raw


@pytest.mark.parametrize("source", ["facebook", "spotify", "strava", "partiful"])
def test_list_archive_failure_stops_before_next_action(source, retained, mocker):
    b = Mock()
    b.wait_for.return_value = True
    if source == "facebook":
        b.eval.return_value = [{"handle": "one", "name": "Example One"}]

        def run():
            return facebook.list_friends(b, max_scrolls=1)
    elif source == "spotify":
        b, _, _ = spotify_browser()

        def run():
            return spotify.list_users(b)
    elif source == "strava":
        b.eval.side_effect = ["999", [{"id": "1", "name": "Example One"}]]

        def run():
            return strava.list_athletes(b)
    else:
        from tests.test_scrape_partiful import FakeBrowser

        b = FakeBrowser()

        def run():
            return list(partiful.harvest(b))

    write = mocker.patch("people_sync.ledger.upsert")
    mocker.patch("people_sync.photos.put_object", side_effect=OSError("failure"))
    with pytest.raises(photos.ArchiveError):
        run()
    write.assert_not_called()
    if source == "partiful":
        assert b.path == "/mutuals"
    elif source == "facebook":
        b.scroll.assert_not_called()
    else:
        assert not any("following" in c.args[0] for c in b.navigate.call_args_list)


def test_list_replay_is_explicitly_unsupported_and_privacy_revalidated(retained):
    from people_sync.scrape import snapshot

    payload, key = snapshot.retain_list(
        "spotify",
        [{"href": "/user/123456789", "name": "person@example.test", "session": "secret"}],
        ordinal=0,
        scope="followers",
    )
    assert payload["entries"] == [{"href": "/user/123456789"}]
    c = json.loads(retained[key])
    result = replay.replay_capture(c)
    assert result["status"] == "unsupported"
    assert "list" in " ".join(result["limitations"])
    c["payload"]["entries"][0]["session"] = "secret"
    c["payload_sha256"] = __import__("hashlib").sha256(captures.encode(c["payload"])).hexdigest()
    with pytest.raises(ValueError):
        captures.validate(c)


def test_facebook_file_config_required_before_browser(mocker, monkeypatch):
    from people_sync import cli

    monkeypatch.delenv("LIFE_HUB_URL", raising=False)
    monkeypatch.delenv("LIFE_HUB_TOKEN", raising=False)
    connect = mocker.patch("people_sync.scrape.cdp.Browser.connect")
    with pytest.raises(SystemExit):
        cli.main(["list", "facebook"])
    connect.assert_not_called()


@pytest.mark.parametrize("source", ["facebook", "spotify", "strava", "partiful"])
def test_later_acquisition_failure_keeps_earlier_observation(source, retained, mocker):
    b = Mock()
    b.wait_for.return_value = True
    if source == "facebook":
        b.eval.side_effect = [[{"handle": "one", "name": "Example One"}], OSError("private detail")]

        def run():
            return facebook.list_friends(b, max_scrolls=2)
    elif source == "spotify":
        b.eval.side_effect = [
            "/user/owner",
            {"followers": 2, "following": 0},
            [{"href": "/user/one", "name": "Example One"}],
            True,
            OSError("private detail"),
        ]

        def run():
            return spotify.list_users(b)
    elif source == "strava":
        b.eval.side_effect = [
            "999",
            [{"id": "1", "name": "Example One"}],
            OSError("private detail"),
        ]

        def run():
            return strava.list_athletes(b)
    else:
        from tests.test_scrape_partiful import FakeBrowser

        b = FakeBrowser()
        original = b.eval

        def fail_second(js):
            if js == partiful.ROW_JS % 1:
                raise OSError("private detail")
            return original(js)

        b.eval = fail_second

        def run():
            return list(partiful.harvest(b))

    with pytest.raises(Exception, match="list-acquisition-failed"):
        run()
    pages = observations(retained)
    assert pages[0][1]["entries"]
    assert pages[-1][1]["reason"] == "acquisition-failed"
    assert pages[-1][1]["complete"] is False
    assert pages[-1][1]["ordinal"] == 1
    assert "private detail" not in str(retained)


@pytest.mark.parametrize("source", ["facebook", "spotify", "strava", "partiful"])
def test_second_upload_failure_leaves_first_capture_and_stops_new_writes(source, retained, mocker):
    b = Mock()
    b.wait_for.return_value = True
    if source == "facebook":
        b.eval.return_value = [{"handle": "one", "name": "Example One"}]

        def run():
            return facebook.list_friends(b, max_scrolls=2)
    elif source == "spotify":
        b, _, _ = spotify_browser()

        def run():
            return spotify.list_users(b)
    elif source == "strava":
        b.eval.side_effect = [
            "999",
            [{"id": "1", "name": "Example One"}],
            [{"id": "1", "name": "Example One"}],
        ]

        def run():
            return strava.list_athletes(b)
    else:
        from tests.test_scrape_partiful import FakeBrowser

        b = FakeBrowser()

        def run():
            return [partiful.ingest_entry(e) for _, _, e in partiful.harvest(b)]

    original = photos.put_object.side_effect

    def fail_second(key, body, **kw):
        if retained:
            raise OSError("upload failed")
        original(key, body, **kw)

    photos.put_object.side_effect = fail_second
    write = mocker.patch("people_sync.ledger.upsert")
    cache = mocker.patch("people_sync.scrape.profile.upsert_profile")
    with pytest.raises(photos.ArchiveError):
        run()
    assert len(retained) == 1
    write.assert_not_called()
    cache.assert_not_called()


def test_partiful_repeated_virtual_rows_keep_distinct_immutable_refs(retained, mocker):
    from tests.test_scrape_partiful import FakeBrowser

    b = FakeBrowser()
    original = b.eval

    def repeated(js):
        if js.startswith("(function(i)"):
            return {"selector": "#ps_row_0", "name": "Example Same", "shared_events": 2}
        return original(js)

    b.eval = repeated
    entries = [e for _, _, e in partiful.harvest(b)]
    assert [e["uid"] for e in entries] == ["uid0", "uid0"]
    assert entries[0]["capture_refs"] != entries[1]["capture_refs"]
    pages = observations(retained)
    assert pages[0][1]["entries"] == pages[1][1]["entries"]
    assert pages[0][1]["entry_ordinals"] == [0]
    assert pages[1][1]["entry_ordinals"] == [1]
    assert entries[0]["raw_r2_key"] != entries[1]["raw_r2_key"]


@pytest.mark.parametrize(
    "source,row",
    [
        (
            "facebook",
            {
                "handle": "profile.php?id=123456789",
                "name": "123456789",
                "mutual_text": "12 mutual friends",
            },
        ),
        ("spotify", {"href": "/user/test%231", "name": "Example"}),
        (
            "strava",
            {
                "id": "123456789",
                "name": "person@example.test",
                "avatar": "https://example.test/a?token=secret",
            },
        ),
        (
            "partiful",
            {
                "name": "Example",
                "shared_events": 12,
                "last_seen": "12 days ago",
                "selector": "#private",
            },
        ),
    ],
)
def test_list_typed_identities_do_not_relax_names_or_urls(source, row, retained):
    from people_sync.scrape import snapshot

    scope = next(iter(snapshot.LIST_SCOPES[source]))
    payload, key = snapshot.retain_list(source, [row], ordinal=0, scope=scope)
    safe = payload["entries"][0]
    assert "selector" not in safe
    if source == "facebook":
        assert safe == {"handle": row["handle"], "mutual_text": row["mutual_text"]}
    elif source == "spotify":
        assert safe == row
    elif source == "strava":
        assert safe == {"id": "123456789"}
    else:
        assert safe == {k: v for k, v in row.items() if k != "selector"}
    assert captures.validate(json.loads(retained[key]))


def test_unknown_list_input_is_retained_as_failed_before_raising(retained):
    from people_sync.scrape import snapshot

    with pytest.raises(ValueError, match="invalid list source"):
        snapshot.retain_list("facebook", "{broken", ordinal=0, scope="friends")
    assert observations(retained)[0][1]["reason"] == "invalid-source"


def test_normalized_records_ignore_only_in_memory_capture_metadata():
    import copy

    row = {
        "source": "strava",
        "source_id": "1",
        "name": "Example One",
        "raw": {"location": "Exampleville"},
    }
    old = {"status": "ok", "records": [row]}
    new = {
        "status": "ok",
        "records": [
            {
                **row,
                "capture_key": "profiles/strava/first.json",
                "capture_refs": [
                    {
                        "capture_key": "profiles/strava/second.json",
                        "ordinal": 0,
                        "entry_ordinal": 1,
                        "scope": "followers",
                    }
                ],
            }
        ],
    }
    original = copy.deepcopy(new)
    assert replay.normalized(old) == replay.normalized(new)
    new["records"][0]["capture_key"] = "profiles/strava/third.json"
    assert replay.normalized(original) == replay.normalized(new)
    assert original["records"][0]["capture_refs"] == new["records"][0]["capture_refs"]
    assert len(new["records"][0]["capture_refs"]) == 1
    changed = copy.deepcopy(new)
    changed["records"][0]["raw"]["location"] = "Elsewhere"
    assert replay.normalized(new) != replay.normalized(changed)


@pytest.mark.parametrize("source", ["facebook", "strava"])
def test_list_dom_does_not_dedupe_before_retention(source):
    from tests.test_dom_js import NODE, run

    if not NODE:
        pytest.skip("node")
    if source == "facebook":
        setup = """
        document.els = [0, 1].map(() => el({href:'https://www.facebook.com/example.one',
          closest(s) {return s === '[role=tablist]' ? null : {innerText:'Example One'};}}));
        document.querySelector = () => document;
        globalThis.location = {pathname:'/owner/friends',search:''};
        """
        rows = json.loads(run(setup, facebook.LIST_ENTRIES_JS))
    else:
        setup = """
        document.els = [0, 1].map(() => el({getAttribute(){return '/athletes/123456789';},
          closest(){return {innerText:'Example One\\nExampleville',querySelector(){return null;}};}}));
        document.querySelector = () => document;
        """
        rows = json.loads(run(setup, strava.LIST_JS % '"999"'))
    assert len(rows) == 2
    assert rows[0] == rows[1]


def test_partiful_decisions_remain_untouched_with_capture_refs(retained, mocker):
    from tests.test_scrape_partiful import FakeBrowser

    entry = next(partiful.harvest(FakeBrowser(), limit=1))[2]
    sql = mocker.patch("people_sync.lifedata.sql", return_value=[{"id": "partiful:uid0"}])
    mocker.patch("people_sync.scrape.profile.upsert_profile")
    partiful.ingest_entry(entry)
    update = sql.call_args.args[0]
    assert "UPDATE people_sync_records" in update
    assert all(
        field not in update
        for field in (
            "status",
            "person_id",
            "suggested_person_id",
            "capture_refs",
            "capture_key",
            "_avatar_url",
        )
    )


@pytest.mark.parametrize("failure", [None, "upload", "readback", "index"])
@pytest.mark.parametrize("virtualize", [False, True])
def test_partiful_executed_dom_retention_precedes_scroll_and_click(
    retained, mocker, failure, virtualize
):
    from tests.test_dom_js import NODE, PARTIFUL_ROW_DOM, run
    from tests.test_scrape_partiful import FakeBrowser

    if not NODE:
        pytest.skip("node")

    events = []

    class Browser(FakeBrowser):
        dialog = False

        def __init__(self):
            super().__init__()
            self.dialog = False

        def eval(self, js):
            if js.startswith("(function(i)") or js.startswith("(function prepareRow(i)"):
                result = run(
                    PARTIFUL_ROW_DOM + f"globalThis.virtualize = {json.dumps(virtualize)};",
                    "(() => {const value = " + js + ";return {value, events};})()",
                )
                events.extend(result["events"])
                return result["value"]
            return super().eval(js)

        def click(self, selector):
            events.append("click")
            super().click(selector)

    upload = photos.put_object.side_effect
    readback = photos.get_object.side_effect

    def put(key, body, **kw):
        events.append("upload")
        if failure == "upload":
            raise OSError("synthetic upload failure")
        upload(key, body, **kw)

    def get(key):
        if failure == "readback":
            return b"wrong"
        body = readback(key)
        events.append("verified")
        return body

    photos.put_object.side_effect = put
    photos.get_object.side_effect = get
    if failure == "index":
        mocker.patch(
            "people_sync.captures.write_private", side_effect=OSError("synthetic index failure")
        )
    b = Browser()
    if failure:
        with pytest.raises(photos.ArchiveError):
            next(partiful.harvest(b, limit=1))
        assert "scroll" not in events and "click" not in events
    elif virtualize:
        with pytest.raises(Exception, match="list-acquisition-failed"):
            next(partiful.harvest(b, limit=1))
        assert "click" not in events
        assert events.index("verified") < events.index("scroll")
    else:
        entry = next(partiful.harvest(b, limit=1))[2]
        assert entry["name"] == "Example Before"
        assert events.index("verified") < events.index("scroll") < events.index("click")
    if failure != "upload":
        assert observations(retained)[0][1]["entries"][0]["name"] == "Example Before"


@pytest.mark.parametrize("identity", ["test%231", "123456789", "Example%20User"])
def test_spotify_list_identity_retained_for_rows_and_owner(identity, retained):
    from people_sync.scrape import snapshot

    payload, key = snapshot.retain_list(
        "spotify",
        [{"href": "/user/" + identity}],
        ordinal=0,
        scope="followers",
        account_id=identity,
    )
    assert payload["entries"] == [{"href": "/user/" + identity}]
    assert captures.validate(json.loads(retained[key]))["payload"]["account_id"] == identity


@pytest.mark.parametrize(
    "identity",
    [
        "test#1",
        "test?token=x",
        "person%40example.test",
        "test%2Fother",
        "test%25231",
        "test%00name",
        "access%20token",
        "test%3Asecret",
    ],
)
def test_spotify_list_identity_exception_does_not_allow_unsafe_values(identity, retained):
    from people_sync.scrape import snapshot

    payload, _ = snapshot.retain_list(
        "spotify", [{"href": "/user/" + identity}], ordinal=0, scope="followers"
    )
    assert payload["entries"] == [{}]
    with pytest.raises(ValueError):
        snapshot.retain_list("spotify", [], ordinal=0, scope="followers", account_id=identity)


def test_spotify_typed_hash_does_not_relax_other_snapshot_fields(retained):
    from people_sync.scrape import snapshot

    for value in (
        "https://open.spotify.com/user/test%231",
        "https://example.test/a?token=x",
        "https://person:secret@example.test/a",
    ):
        with pytest.raises(ValueError):
            snapshot.safe_url(value, "spotify")
    payload, _ = snapshot.retain_list(
        "spotify",
        [
            {
                "href": "/user/test%231",
                "name": "person@example.test",
                "avatar": "https://example.test/a?token=x",
                "session": "secret",
            }
        ],
        ordinal=0,
        scope="followers",
    )
    assert payload["entries"] == [{"href": "/user/test%231"}]
