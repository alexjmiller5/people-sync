import json
import re

import httpx
import pytest

from people_sync import reconcile

GROUPS = {"contactGroups/1": "Family", "contactGroups/2": "ΣAE"}
NOW = "2026-01-01T00:00:00.000Z"


@pytest.fixture(autouse=True)
def _notion_configured(monkeypatch):
    """These tests exercise the Notion-anchored id path; local ids are covered elsewhere."""
    monkeypatch.setenv("PEOPLE_SYNC_NOTION_PEOPLE_DS", "ds-synthetic")


def _raw(**over):
    raw = {
        "names": [{"displayName": "Nova Quill", "givenName": "Nova", "familyName": "Quill"}],
        "labels": [
            {"contactGroupMembership": {"contactGroupResourceName": "contactGroups/1"}},
            {"contactGroupMembership": {"contactGroupResourceName": "contactGroups/2"}},
            {"contactGroupMembership": {"contactGroupResourceName": "contactGroups/myContacts"}},
        ],
        "org": [{"name": "chess club"}],
        "birthday": [{"date": {"year": 1990, "month": 3, "day": 4}}],
        "photo_url": None,
    }
    raw.update(over)
    return raw


def _record(raw=None, **over):
    rec = {
        "id": "google_contacts:people/c1",
        "source": "google_contacts",
        "source_id": "people/c1",
        "handle": None,
        "name": "Nova Quill",
        "raw": json.dumps(_raw() if raw is None else raw),
        "status": "pending",
        "person_id": None,
    }
    rec.update(over)
    return rec


def _person(**over):
    person = {
        "id": "p1",
        "name": "Nova Quill",
        "first_name": None,
        "middle_name": None,
        "last_name": None,
        "nickname": None,
        "surname_at_birth": None,
        "gender": None,
        "birthday": None,
        "slightly_known_birthday": None,
        "death_date": None,
        "deceased": None,
        "likes": None,
        "dislikes": None,
        "circles": None,
        "notes": None,
        "notify_birthday": 0,
        "created_at": "t0",
        "updated_at": "t0",
        "deleted_at": None,
    }
    person.update(over)
    return person


def _router(people=(), records=(), **children):
    """Route somadata.sql() reads by table; UPDATEs return nothing, like the real
    backend. Table names are matched on a word boundary: `FROM people` is a
    prefix of `FROM people_sync_records`, so a plain substring test routes the
    ledger read to the people table."""
    tables = {"people": people, "people_sync_records": records, **children}

    def _fn(query):
        if query.startswith("UPDATE"):
            return []
        for table, rows in tables.items():
            if re.search(rf"\bFROM {table}\b", query):
                return list(rows)
        return []

    return _fn


@pytest.fixture
def env(mocker, monkeypatch):
    """Everything external mocked: no soma, no gog, no Notion, no clock drift."""
    monkeypatch.delenv("NOTION_API_TOKEN", raising=False)
    monkeypatch.setenv(
        reconcile.RELATIONS_ENV,
        json.dumps(
            {
                "Gifts": ["ds-gifts", "%3FT%40U"],
                "Quotes": ["ds-quotes", "Y%5B%3E%7B"],
                "Trips": ["ds-trips", "t%3DJH"],
                "Calendar": ["ds-calendar", "%3DS%60m"],
            }
        ),
    )
    mocker.patch("people_sync.reconcile.user_groups", return_value=GROUPS)
    mocker.patch("people_sync.somadata.now_iso", return_value=NOW)
    return mocker


def _writes(sql):
    return [c.args[0] for c in sql.call_args_list if not c.args[0].lstrip().startswith("SELECT")]


# --- helpers ------------------------------------------------------------------


def test_parse_record_maps_circles_and_birthday():
    parsed = reconcile.parse_record(_record(), GROUPS)
    assert parsed["display_name"] == "Nova Quill"
    assert parsed["first_name"] == "Nova"
    assert parsed["last_name"] == "Quill"
    assert parsed["birthday"] == "1990-03-04"
    # myContacts is a system group and is never a circle; ΣAE maps to SAE
    assert parsed["circles"] == ["Family", "SAE", "chess club"]


def test_parse_record_yearless_birthday():
    raw = _raw(birthday=[{"date": {"month": 12, "day": 7}}])
    assert reconcile.parse_record(_record(raw=raw), GROUPS)["birthday"] == "--12-07"


def test_union_circles_preserves_existing_order_and_dedupes():
    assert reconcile.union_circles(["NYC", "Family"], ["Family", "SAE"]) == ["NYC", "Family", "SAE"]


# --- link ---------------------------------------------------------------------


def test_link_fills_empty_fields_and_preserves_non_empty(env):
    person = _person(first_name=None, last_name="Quillon", birthday=None)
    sql = env.patch(
        "people_sync.somadata.sql", side_effect=_router(people=[person], records=[_record()])
    )
    insert = env.patch("people_sync.somadata.insert")

    reconcile.main(["link", "p1", "google_contacts:people/c1", "--apply"])

    people_update = next(w for w in _writes(sql) if w.startswith("UPDATE people"))
    assert "first_name = 'Nova'" in people_update
    # last_name is already set and differs: never overwritten, preserved in notes
    assert "last_name = 'Quill'" not in people_update
    assert "google_last_name: Quill" in people_update
    assert "birthday = '1990-03-04'" in people_update
    account = insert.call_args_list[0].args[1][0]
    assert account["id"] == "google_contacts:p1:people/c1"
    assert account["platform"] == "google_contacts"
    assert account["source_id"] == "people/c1"
    assert account["display_name"] == "Nova Quill"
    assert account["active"] == 1
    ledger = next(w for w in _writes(sql) if w.startswith("UPDATE people_sync_records"))
    assert "status = 'matched'" in ledger and "person_id = 'p1'" in ledger


def test_link_birthday_conflict_never_overwrites(env, capsys):
    person = _person(birthday="1991-08-09")
    sql = env.patch(
        "people_sync.somadata.sql", side_effect=_router(people=[person], records=[_record()])
    )
    env.patch("people_sync.somadata.insert")

    reconcile.main(["link", "p1", "google_contacts:people/c1", "--apply"])

    updates = [w for w in _writes(sql) if w.startswith("UPDATE people")]
    assert not any("birthday" in u for u in updates)
    out = capsys.readouterr().out
    assert "CONFLICT birthday" in out and "1991-08-09" in out and "1990-03-04" in out


def test_link_circle_union_keeps_existing_first(env):
    person = _person(circles=json.dumps(["NYC", "Family"]))
    sql = env.patch(
        "people_sync.somadata.sql", side_effect=_router(people=[person], records=[_record()])
    )
    env.patch("people_sync.somadata.insert")

    reconcile.main(["link", "p1", "google_contacts:people/c1", "--apply"])

    update = next(w for w in _writes(sql) if w.startswith("UPDATE people"))
    assert json.dumps(["NYC", "Family", "SAE", "chess club"]) in update


def test_link_rename_preserves_old_name_in_nickname(env):
    person = _person(name="N. Quill", nickname=None)
    sql = env.patch(
        "people_sync.somadata.sql", side_effect=_router(people=[person], records=[_record()])
    )
    env.patch("people_sync.somadata.insert")

    reconcile.main(["link", "p1", "google_contacts:people/c1", "--rename", "--apply"])

    update = next(w for w in _writes(sql) if w.startswith("UPDATE people"))
    assert "name = 'Nova Quill'" in update
    assert "nickname = 'N. Quill'" in update
    assert "aka:" not in update


def test_link_rename_falls_back_to_notes_when_nickname_taken(env):
    person = _person(name="N. Quill", nickname="Novi", notes="knows chess")
    sql = env.patch(
        "people_sync.somadata.sql", side_effect=_router(people=[person], records=[_record()])
    )
    env.patch("people_sync.somadata.insert")

    reconcile.main(["link", "p1", "google_contacts:people/c1", "--rename", "--apply"])

    update = next(w for w in _writes(sql) if w.startswith("UPDATE people"))
    assert "nickname" not in update
    assert "knows chess" in update and "aka: N. Quill" in update


def test_link_without_rename_leaves_name_alone(env):
    person = _person(name="N. Quill")
    sql = env.patch(
        "people_sync.somadata.sql", side_effect=_router(people=[person], records=[_record()])
    )
    env.patch("people_sync.somadata.insert")

    reconcile.main(["link", "p1", "google_contacts:people/c1", "--apply"])

    update = next(w for w in _writes(sql) if w.startswith("UPDATE people"))
    assert "SET name" not in update and "nickname" not in update


def test_link_skips_duplicate_account(env):
    accounts = [
        {
            "id": "google_contacts:p1:people/c1",
            "person_id": "p1",
            "platform": "google_contacts",
            "source_id": "people/c1",
        }
    ]
    env.patch(
        "people_sync.somadata.sql",
        side_effect=_router(people=[_person()], records=[_record()], person_accounts=accounts),
    )
    insert = env.patch("people_sync.somadata.insert")

    reconcile.main(["link", "p1", "google_contacts:people/c1", "--apply"])

    insert.assert_not_called()


def test_link_refuses_to_insert_beside_a_legacy_account_without_source_id(env, capsys):
    # pre-backfill rows carry the profile url instead of a source_id: uncomparable,
    # so inserting beside one would give the person two google account rows
    accounts = [
        {"id": "google_contacts:p1", "person_id": "p1", "source_id": None},
    ]
    env.patch(
        "people_sync.somadata.sql",
        side_effect=_router(people=[_person()], records=[_record()], person_accounts=accounts),
    )
    insert = env.patch("people_sync.somadata.insert")
    warn = env.patch("people_sync.reconcile.log.warning")

    reconcile.main(["link", "p1", "google_contacts:people/c1", "--apply"])

    insert.assert_not_called()
    assert "google_contacts:p1" in capsys.readouterr().out
    event, kwargs = warn.call_args.args[0], warn.call_args.kwargs
    assert "legacy google account row without source_id" in event
    assert kwargs == {
        "source": "google_contacts",
        "person_id": "p1",
        "account_id": "google_contacts:p1",
    }


def test_link_on_matched_record_is_a_noop(env, capsys):
    record = _record(status="matched", person_id="p1")
    sql = env.patch(
        "people_sync.somadata.sql", side_effect=_router(people=[_person()], records=[record])
    )
    insert = env.patch("people_sync.somadata.insert")

    reconcile.main(["link", "p1", "google_contacts:people/c1", "--apply"])

    assert _writes(sql) == []
    insert.assert_not_called()
    assert "already matched" in capsys.readouterr().out


def test_link_dry_run_emits_no_writes(env):
    sql = env.patch(
        "people_sync.somadata.sql", side_effect=_router(people=[_person()], records=[_record()])
    )
    insert = env.patch("people_sync.somadata.insert")

    reconcile.main(["link", "p1", "google_contacts:people/c1"])

    assert _writes(sql) == []
    insert.assert_not_called()


# --- merge --------------------------------------------------------------------


def _merge_env(env, survivor, loser, **children):
    return env.patch(
        "people_sync.somadata.sql",
        # reversed on purpose: survivor/loser are resolved by id, not row order
        side_effect=_router(people=[loser, survivor], **children),
    )


def test_merge_repoints_every_table_and_soft_deletes_the_loser(env):
    survivor = _person(id="s1", name="Nova Quill")
    loser = _person(id="l1", name="N Quill")
    children = {
        "person_accounts": [
            {"id": "a-l", "person_id": "l1", "platform": "instagram", "source_id": "ig1"}
        ],
        "person_photos": [{"id": "ph-l", "person_id": "l1", "sha256": "aa"}],
        "person_locations": [
            {
                "id": "lo-l",
                "person_id": "l1",
                "city": "Springfield",
                "country": "US",
                "start": None,
                "end": None,
            }
        ],
        "person_employments": [
            {
                "id": "em-l",
                "person_id": "l1",
                "company": "Acme",
                "title": None,
                "start": None,
                "end": None,
            }
        ],
    }
    sql = _merge_env(env, survivor, loser, **children)

    reconcile.main(["merge", "s1", "l1", "--apply"])

    writes = _writes(sql)
    for table, row_id in (
        ("person_accounts", "a-l"),
        ("person_photos", "ph-l"),
        ("person_locations", "lo-l"),
        ("person_employments", "em-l"),
    ):
        assert any(
            w == f"UPDATE {table} SET person_id = 's1' WHERE id = '{row_id}'" for w in writes
        ), table
    assert any("UPDATE people_sync_records SET person_id = 's1'" in w for w in writes)
    assert any("UPDATE people_sync_records SET suggested_person_id = 's1'" in w for w in writes)
    assert any(
        w.startswith("UPDATE people SET deleted_at = ") and "'l1'" in w and NOW in w for w in writes
    )


@pytest.mark.parametrize(
    ("table", "shared", "different"),
    [
        ("person_photos", {"sha256": "aa"}, {"sha256": "bb"}),
        (
            "person_locations",
            {"city": "Springfield", "country": "US", "start": None, "end": None},
            {"city": "Shelbyville", "country": "US", "start": None, "end": None},
        ),
        (
            "person_employments",
            {"company": "Acme", "title": None, "start": None, "end": None},
            {"company": "Globex", "title": None, "start": None, "end": None},
        ),
    ],
)
def test_merge_dedupes_child_rows_by_their_key(env, table, shared, different):
    rows = [
        {"id": "keep", "person_id": "s1", **shared},
        {"id": "dupe", "person_id": "l1", **shared},
        {"id": "other", "person_id": "l1", **different},
    ]
    sql = _merge_env(env, _person(id="s1"), _person(id="l1"), **{table: rows})

    reconcile.main(["merge", "s1", "l1", "--apply"])

    writes = _writes(sql)
    assert any(w.startswith(f"UPDATE {table} SET deleted_at") and "'dupe'" in w for w in writes)
    assert f"UPDATE {table} SET person_id = 's1' WHERE id = 'other'" in writes
    assert not any("SET person_id = 's1'" in w and "'dupe'" in w for w in writes)


def test_merge_ignores_an_inactive_survivor_row_when_deduping(env):
    accounts = [
        # the survivor's row is retired (a renamed handle): history, not a duplicate
        {"id": "a-s", "person_id": "s1", "platform": "instagram", "source_id": "ig1", "active": 0},
        {"id": "a-l", "person_id": "l1", "platform": "instagram", "source_id": "ig1", "active": 1},
    ]
    sql = _merge_env(env, _person(id="s1"), _person(id="l1"), person_accounts=accounts)

    reconcile.main(["merge", "s1", "l1", "--apply"])

    writes = _writes(sql)
    assert "UPDATE person_accounts SET person_id = 's1' WHERE id = 'a-l'" in writes
    assert not any(w.startswith("UPDATE person_accounts SET deleted_at") for w in writes)


def test_merge_dedupes_an_account_that_would_duplicate(env):
    survivor = _person(id="s1")
    loser = _person(id="l1")
    accounts = [
        {"id": "a-s", "person_id": "s1", "platform": "instagram", "source_id": "ig1"},
        {"id": "a-l", "person_id": "l1", "platform": "instagram", "source_id": "ig1"},
        {"id": "b-l", "person_id": "l1", "platform": "snapchat", "source_id": "sn1"},
    ]
    sql = _merge_env(env, survivor, loser, person_accounts=accounts)

    reconcile.main(["merge", "s1", "l1", "--apply"])

    writes = _writes(sql)
    assert any(
        w.startswith("UPDATE person_accounts SET deleted_at") and "'a-l'" in w for w in writes
    )
    assert any("UPDATE person_accounts SET person_id = 's1'" in w and "'b-l'" in w for w in writes)
    assert not any("SET person_id = 's1'" in w and "'a-l'" in w for w in writes)


def test_merge_soft_deletes_a_self_referential_relation(env):
    survivor = _person(id="s1")
    loser = _person(id="l1")
    relations = [
        {"id": "r1", "person_id": "s1", "related_id": "l1", "relation_type": "partner"},
        {"id": "r2", "person_id": "l1", "related_id": "x9", "relation_type": "father"},
    ]
    sql = _merge_env(env, survivor, loser, person_relations=relations)

    reconcile.main(["merge", "s1", "l1", "--apply"])

    writes = _writes(sql)
    assert any(
        w.startswith("UPDATE person_relations SET deleted_at") and "'r1'" in w for w in writes
    )
    assert any("UPDATE person_relations SET person_id = 's1'" in w and "'r2'" in w for w in writes)


def test_merge_soft_deletes_a_relation_that_would_duplicate(env):
    survivor = _person(id="s1")
    loser = _person(id="l1")
    relations = [
        {"id": "r1", "person_id": "s1", "related_id": "x9", "relation_type": "father"},
        {"id": "r2", "person_id": "l1", "related_id": "x9", "relation_type": "father"},
    ]
    sql = _merge_env(env, survivor, loser, person_relations=relations)

    reconcile.main(["merge", "s1", "l1", "--apply"])

    writes = _writes(sql)
    assert any(
        w.startswith("UPDATE person_relations SET deleted_at") and "'r2'" in w for w in writes
    )
    assert not any("SET person_id = 's1'" in w and "'r2'" in w for w in writes)


def test_merge_folds_scalars_and_drops_nothing(env):
    survivor = _person(id="s1", name="Nova Quill", gender="f", likes="chess", notes="met at work")
    loser = _person(
        id="l1",
        name="N Quill",
        gender="female",
        birthday="1990-03-04",
        notify_birthday=1,
        circles=json.dumps(["SAE"]),
        notes="plays chess",
    )
    survivor["circles"] = json.dumps(["Family"])
    sql = _merge_env(env, survivor, loser)

    reconcile.main(["merge", "s1", "l1", "--apply"])

    update = next(w for w in _writes(sql) if w.startswith("UPDATE people SET") and "'s1'" in w)
    # empty on the survivor: copied straight over
    assert "birthday = '1990-03-04'" in update
    # both set and different: survivor keeps its value, the loser's lands in notes
    assert "gender = " not in update
    assert "merged from N Quill (l1): gender=female" in update
    assert "merged from N Quill (l1): name=N Quill" in update
    # notes concatenated, survivor first
    assert update.index("met at work") < update.index("plays chess")
    assert "notify_birthday = 1" in update
    assert json.dumps(["Family", "SAE"]) in update


def test_merge_dry_run_emits_no_writes(env):
    sql = _merge_env(env, _person(id="s1"), _person(id="l1"))

    reconcile.main(["merge", "s1", "l1"])

    assert _writes(sql) == []


def _rel(*ids, has_more=False):
    return {
        "id": "x",
        "type": "relation",
        "relation": [{"id": i} for i in ids],
        "has_more": has_more,
    }


# The People data source (ds-synthetic, from the autouse fixture): Father/Mother are
# two-way self relations, Partner is one-way (the target page does not show it).
PEOPLE_SCHEMA = {
    "Name": {"id": "title", "type": "title"},
    "Father": {
        "id": "fa",
        "type": "relation",
        "relation": {"type": "dual_property", "data_source_id": "ds-synthetic"},
    },
    "Partner": {
        "id": "pa",
        "type": "relation",
        "relation": {"type": "single_property", "data_source_id": "ds-synthetic"},
    },
    "Gifts": {
        "id": "gi",
        "type": "relation",
        "relation": {"type": "dual_property", "data_source_id": "ds-gifts"},
    },
}


def _notion(env, page, *, items=None, hits=None, fail=()):
    """Synthetic Notion API: GET pages/<id>, pages/<id>/properties/<prop>,
    data_sources/<id>; POST data_sources/<id>/query. Paged bodies are keyed by
    their start_cursor; anything in `fail` raises."""
    items, hits = items or {}, hits or {}

    def respond(body):
        resp = env.Mock()
        resp.json.return_value = body
        return resp

    def get(url, **kw):
        path = url.split("/v1/", 1)[1]
        if path in fail:
            raise httpx.ConnectError("boom")
        cursor = (kw.get("params") or {}).get("start_cursor")
        if "/properties/" in path:
            return respond(items[(path.rsplit("/", 1)[1], cursor)])
        if path.startswith("pages/"):
            return respond({"object": "page", "properties": page})
        return respond({"properties": PEOPLE_SCHEMA})

    def post(url, json, **kw):
        ds = url.split("/data_sources/", 1)[1].split("/")[0]
        if ds in fail:
            raise httpx.ConnectError("boom")
        key = (ds, json["filter"]["property"], json.get("start_cursor"))
        return respond(hits.get(key, {"results": [], "has_more": False}))

    return env.patch("httpx.get", side_effect=get), env.patch("httpx.post", side_effect=post)


def _relation_lines(out):
    return [line for line in out.splitlines() if line.startswith("NOTION RELATION ")]


def test_merge_dry_run_prints_the_loser_pages_own_father_and_mother(env, monkeypatch, capsys):
    # 2026-10-06: the dry run printed nothing while the loser page held Father and
    # Mother - two-way People self relations, in no configured database.
    monkeypatch.setenv("NOTION_API_TOKEN", "token")
    sql = _merge_env(env, _person(id="s1"), _person(id="0" * 32))
    page = {
        "Name": {"id": "title", "type": "title", "title": []},
        "Father": _rel("f0000001"),
        "Mother": _rel("m0000001"),
        "Gifts": _rel(),
    }
    get, post = _notion(env, page)

    reconcile.main(["merge", "s1", "0" * 32])

    out = capsys.readouterr().out
    lines = _relation_lines(out)
    assert len(lines) == 2
    assert "Father" in lines[0] and "f0000001" in lines[0] and "re-point manually" in lines[0]
    assert "Mother" in lines[1] and "m0000001" in lines[1]
    assert "NOTION RELATIONS CHECKED: 2 to re-point" in out
    assert get.call_args_list[0].args[0].endswith("/pages/00000000-0000-0000-0000-000000000000")
    # the one-way self relation is queried like a configured database, by property id
    queried = [(c.args[0].split("/")[-2], c.kwargs["json"]["filter"]) for c in post.call_args_list]
    assert (
        "ds-synthetic",
        {"property": "pa", "relation": {"contains": "0" * 8 + "-0000-0000-0000-" + "0" * 12}},
    ) in queried
    assert _writes(sql) == []


def test_merge_reads_every_entry_of_a_truncated_relation(env, monkeypatch, capsys):
    monkeypatch.setenv("NOTION_API_TOKEN", "token")
    _merge_env(env, _person(id="s1"), _person(id="l1"))
    page = {"Father's Children": {**_rel("c1", has_more=True), "id": "ch"}}

    def item(i):
        return {"object": "property_item", "type": "relation", "relation": {"id": i}}

    items = {
        ("ch", None): {"results": [item("c1"), item("c2")], "has_more": True, "next_cursor": "k2"},
        ("ch", "k2"): {"results": [item("c3")], "has_more": False, "next_cursor": None},
    }
    _notion(env, page, items=items)

    reconcile.main(["merge", "s1", "l1"])

    lines = _relation_lines(capsys.readouterr().out)
    assert [line.split(" -> ")[1].split()[0] for line in lines] == ["c1", "c2", "c3"]


def test_merge_follows_query_cursors_and_reports_each_page_once(env, monkeypatch, capsys):
    monkeypatch.setenv("NOTION_API_TOKEN", "token")
    _merge_env(env, _person(id="s1"), _person(id="l1"))
    hits = {
        ("ds-gifts", "%3FT%40U", None): {
            "results": [{"id": "g1", "url": "https://notion.so/gift-g1"}],
            "has_more": True,
            "next_cursor": "k2",
        },
        ("ds-gifts", "%3FT%40U", "k2"): {
            "results": [{"id": "g2", "url": "https://notion.so/gift-g2"}],
            "has_more": False,
        },
        ("ds-calendar", "%3DS%60m", None): {
            "results": [{"id": "e1", "url": "https://notion.so/event-e1"}],
            "has_more": False,
        },
    }
    # g1 also shows on the loser page (Gifts is two-way): one line, not two
    _, post = _notion(env, {"Gifts": _rel("g1")}, hits=hits)

    reconcile.main(["merge", "s1", "l1"])

    out = capsys.readouterr().out
    lines = _relation_lines(out)
    assert len(lines) == 3
    assert sum("g1" in line for line in lines) == 1
    assert any("gift-g2" in line for line in lines) and any("event-e1" in line for line in lines)
    assert "NOTION RELATIONS CHECKED: 3 to re-point" in out
    gifts = [c for c in post.call_args_list if "ds-gifts" in c.args[0]]
    assert [c.kwargs["json"].get("start_cursor") for c in gifts] == [None, "k2"]
    # configured databases are still filtered by property ID, never by name
    filters = [c.kwargs["json"]["filter"]["property"] for c in post.call_args_list]
    assert {"%3FT%40U", "Y%5B%3E%7B", "t%3DJH", "%3DS%60m"} <= set(filters)


def test_merge_reports_a_failed_check_as_incomplete_and_still_merges(env, monkeypatch, capsys):
    monkeypatch.setenv("NOTION_API_TOKEN", "token")
    sql = _merge_env(env, _person(id="s1"), _person(id="l1"))
    hits = {
        ("ds-quotes", "Y%5B%3E%7B", None): {
            "results": [{"id": "q1", "url": "https://notion.so/quote-q1"}],
            "has_more": False,
        }
    }
    _notion(env, {"Mother": _rel("m1")}, hits=hits, fail={"ds-gifts"})

    reconcile.main(["merge", "s1", "l1", "--apply"])

    out = capsys.readouterr().out
    assert len(_relation_lines(out)) == 2
    assert "NOTION RELATIONS INCOMPLETE" in out and "Gifts" in out and "ConnectError" in out
    assert "NOTION RELATIONS CHECKED" not in out
    assert any(w.startswith("UPDATE people SET deleted_at") for w in _writes(sql))


def test_merge_reports_an_unreadable_loser_page_as_incomplete(env, monkeypatch, capsys):
    monkeypatch.setenv("NOTION_API_TOKEN", "token")
    _merge_env(env, _person(id="s1"), _person(id="l1"))
    _notion(env, {}, fail={"pages/l1"})

    reconcile.main(["merge", "s1", "l1"])

    out = capsys.readouterr().out
    assert "NOTION RELATIONS INCOMPLETE" in out and "loser page" in out


def test_merge_without_a_token_says_the_check_did_not_run(env, capsys):
    _merge_env(env, _person(id="s1"), _person(id="l1"))
    get, post = env.patch("httpx.get"), env.patch("httpx.post")

    reconcile.main(["merge", "s1", "l1"])

    get.assert_not_called()
    post.assert_not_called()
    out = capsys.readouterr().out
    assert "NOTION RELATIONS INCOMPLETE" in out and "NOTION_API_TOKEN" in out


def test_merge_without_relation_config_still_reads_the_loser_page(env, monkeypatch, capsys):
    monkeypatch.setenv("NOTION_API_TOKEN", "token")
    monkeypatch.delenv(reconcile.RELATIONS_ENV, raising=False)
    _merge_env(env, _person(id="s1"), _person(id="l1"))
    _notion(env, {"Father": _rel("f1")})

    reconcile.main(["merge", "s1", "l1"])

    out = capsys.readouterr().out
    assert len(_relation_lines(out)) == 1
    assert "NOTION RELATIONS INCOMPLETE" in out and reconcile.RELATIONS_ENV in out


def test_merge_without_notion_says_so(env, monkeypatch, capsys):
    monkeypatch.setenv("NOTION_API_TOKEN", "token")
    monkeypatch.delenv(reconcile.RELATIONS_ENV, raising=False)
    monkeypatch.delenv("PEOPLE_SYNC_NOTION_PEOPLE_DS", raising=False)
    _merge_env(env, _person(id="s1"), _person(id="l1"))
    get, post = env.patch("httpx.get"), env.patch("httpx.post")

    reconcile.main(["merge", "s1", "l1"])

    get.assert_not_called()
    post.assert_not_called()
    assert "NOTION RELATIONS NOT CHECKED: Notion is not configured" in capsys.readouterr().out


def test_merge_refuses_a_person_merged_into_itself(env):
    _merge_env(env, _person(id="s1"), _person(id="s1"))

    with pytest.raises(SystemExit):
        reconcile.main(["merge", "s1", "s1", "--apply"])


# --- create -------------------------------------------------------------------


@pytest.mark.parametrize("year,expected", [(1604, "--03-04"), (0, "--03-04"), (1990, "1990-03-04")])
def test_create_preview_does_not_treat_contact_placeholder_as_birth_year(
    env, capsys, year, expected
):
    record = _record(raw=_raw(birthday=[{"date": {"year": year, "month": 3, "day": 4}}]))
    env.patch("people_sync.somadata.sql", side_effect=_router(records=[record]))

    reconcile.main(["create", "google_contacts:people/c1"])

    row = next(
        line[8:] for line in capsys.readouterr().out.splitlines() if line.startswith("DRY-RUN {")
    )
    assert json.loads(row)["birthday"] == expected


def test_create_populates_every_field_with_the_dash_stripped_page_id(env):
    page_id = "12345678-90ab-cdef-1234-567890abcdef"
    stub = env.patch("people_sync.notion_people.create_stub", return_value=page_id)
    sql = env.patch("people_sync.somadata.sql", side_effect=_router(records=[_record()]))
    insert = env.patch("people_sync.somadata.insert")

    reconcile.main(["create", "google_contacts:people/c1", "--apply"])

    stub.assert_called_once_with("Nova Quill")
    person = insert.call_args_list[0].args[1][0]
    assert person["id"] == "1234567890abcdef1234567890abcdef"
    assert person["name"] == "Nova Quill"
    assert person["first_name"] == "Nova"
    assert person["last_name"] == "Quill"
    assert person["birthday"] == "1990-03-04"
    assert json.loads(person["circles"]) == ["Family", "SAE", "chess club"]
    account = insert.call_args_list[1].args[1][0]
    assert account["id"] == "google_contacts:1234567890abcdef1234567890abcdef:people/c1"
    assert account["person_id"] == person["id"]
    ledger = next(w for w in _writes(sql) if w.startswith("UPDATE people_sync_records"))
    assert "status = 'matched'" in ledger and person["id"] in ledger


def test_create_reports_an_orphaned_notion_page(env, capsys):
    env.patch("people_sync.notion_people.create_stub", return_value="abc-def")
    env.patch("people_sync.somadata.sql", side_effect=_router(records=[_record()]))
    env.patch("people_sync.somadata.insert", side_effect=RuntimeError("soma is down"))

    with pytest.raises(RuntimeError):
        reconcile.main(["create", "google_contacts:people/c1", "--apply"])

    assert "orphaned notion page abc-def" in capsys.readouterr().err


def test_create_refuses_a_record_that_is_not_pending(env):
    env.patch("people_sync.somadata.sql", side_effect=_router(records=[_record(status="ignored")]))
    stub = env.patch("people_sync.notion_people.create_stub")

    with pytest.raises(SystemExit):
        reconcile.main(["create", "google_contacts:people/c1", "--apply"])

    stub.assert_not_called()


def test_create_dry_run_writes_nothing(env):
    stub = env.patch("people_sync.notion_people.create_stub")
    sql = env.patch("people_sync.somadata.sql", side_effect=_router(records=[_record()]))
    insert = env.patch("people_sync.somadata.insert")

    reconcile.main(["create", "google_contacts:people/c1"])

    stub.assert_not_called()
    insert.assert_not_called()
    assert _writes(sql) == []


def test_notion_relations_come_from_config(monkeypatch):
    monkeypatch.delenv(reconcile.RELATIONS_ENV, raising=False)
    assert reconcile.notion_relations() == {}
    monkeypatch.setenv(reconcile.RELATIONS_ENV, json.dumps({"Gifts": ["ds-1", "%3FT%40U"]}))
    assert reconcile.notion_relations() == {"Gifts": ("ds-1", "%3FT%40U")}
    monkeypatch.setenv(reconcile.RELATIONS_ENV, json.dumps({"Gifts": "ds-1"}))
    with pytest.raises(ValueError):
        reconcile.notion_relations()


def _generic_env(env, person, record):
    def sql(query):
        if query.startswith("SELECT * FROM people ") and person["id"] in query:
            return [person]
        if query.startswith("SELECT * FROM people_sync_records") and record["id"] in query:
            return [record]
        if query.startswith("SELECT id, source_id FROM person_accounts"):
            return []
        return []

    return env.patch("people_sync.somadata.sql", side_effect=sql), env.patch(
        "people_sync.somadata.insert"
    )


def test_link_any_source_writes_platform_account_and_renames_losslessly(env):
    person = _person(id="p1", name="Andrea", nickname=None, notes=None)
    record = {
        "id": "instagram:andrea.garcia2",
        "source": "instagram",
        "source_id": "andrea.garcia2",
        "handle": "andrea.garcia2",
        "name": "Andrea Garcia",
        "raw": json.dumps(
            {
                "followers": {
                    "string_list_data": [{"href": "https://www.instagram.com/andrea.garcia2"}]
                }
            }
        ),
        "status": "pending",
        "person_id": None,
    }
    sql, insert = _generic_env(env, person, record)
    reconcile.main(["link", "p1", "instagram:andrea.garcia2", "--name", "Andrea Garcia", "--apply"])
    writes = _writes(sql)
    [update] = [w for w in writes if w.startswith("UPDATE people SET")]
    for part in (
        "name = 'Andrea Garcia'",
        "nickname = 'Andrea'",
        "first_name = 'Andrea'",
        "last_name = 'Garcia'",
    ):
        assert part in update
    assert any(
        "UPDATE people_sync_records SET status = 'matched', person_id = 'p1'" in w for w in writes
    )
    [(table, rows)] = [c.args for c in insert.call_args_list]
    assert table == "person_accounts"
    assert rows[0]["id"] == "instagram:p1:andrea.garcia2" and rows[0]["platform"] == "instagram"
    assert rows[0]["url"] == "https://www.instagram.com/andrea.garcia2"
    assert rows[0]["display_name"] == "Andrea Garcia"


def test_create_from_any_source_requires_a_name(env, monkeypatch):
    record = {
        "id": "instagram:ana.harr",
        "source": "instagram",
        "source_id": "ana.harr",
        "handle": "ana.harr",
        "name": None,
        "raw": "{}",
        "status": "pending",
        "person_id": None,
    }
    sql, insert = _generic_env(env, _person(id="x"), record)
    with pytest.raises(SystemExit):
        reconcile.main(["create", "instagram:ana.harr", "--apply"])
    env.patch("people_sync.reconcile.notion_people.create_stub", return_value="aaaa-bbbb")
    reconcile.main(["create", "instagram:ana.harr", "--name", "Anabelle Mufson Harr", "--apply"])
    tables = [c.args[0] for c in insert.call_args_list]
    assert tables == ["people", "person_accounts"]
    row = insert.call_args_list[0].args[1][0]
    assert row["id"] == "aaaabbbb" and row["name"] == "Anabelle Mufson Harr"
    assert (
        row["first_name"] == "Anabelle"
        and row["last_name"] == "Harr"
        and row["middle_name"] == "Mufson"
    )
    assert insert.call_args_list[1].args[1][0]["platform"] == "instagram"


def test_corrected_spelling_is_a_note_not_a_nickname(env):
    person = _person(
        id="p1", name="Amanda Fercheck", last_name="Fercheck", nickname=None, notes=None
    )
    record = {
        "id": "linkedin:af",
        "source": "linkedin",
        "source_id": "af",
        "handle": "af",
        "name": "Amanda Ferchak",
        "raw": "{}",
        "status": "pending",
        "person_id": None,
    }
    sql, insert = _generic_env(env, person, record)
    reconcile.main(["link", "p1", "linkedin:af", "--name", "Amanda Ferchak", "--apply"])
    [update] = [w for w in _writes(sql) if w.startswith("UPDATE people SET")]
    assert "name = 'Amanda Ferchak'" in update and "last_name = 'Ferchak'" in update
    assert "spelling: Amanda Fercheck" in update and "nickname" not in update
    assert reconcile._spelling_variant("Anabelle Broadsky", "Anabelle Brodsky")
    assert not reconcile._spelling_variant("Andrea", "Andrea Garcia")
    assert not reconcile._spelling_variant("Amanda Klein", "Amanda Booth")


def test_create_adds_the_given_circles(env):
    record = {
        "id": "venmo:1",
        "source": "venmo",
        "source_id": "1",
        "handle": "jt",
        "name": "Joe Example",
        "raw": "{}",
        "status": "pending",
        "person_id": None,
    }
    sql, insert = _generic_env(env, _person(id="x"), record)
    env.patch("people_sync.reconcile.notion_people.create_stub", return_value="aaaa-bbbb")
    reconcile.main(
        ["create", "venmo:1", "--name", "Joe Example", "--circle", "Through Fyn", "--apply"]
    )
    row = insert.call_args_list[0].args[1][0]
    assert json.loads(row["circles"]) == ["Through Fyn"]


def test_ignore_marks_pending_records_and_refuses_matched_ones(env, capsys):
    rows = {
        "instagram:a": {
            "id": "instagram:a",
            "source": "instagram",
            "status": "pending",
            "person_id": None,
        },
        "venmo:b": {"id": "venmo:b", "source": "venmo", "status": "matched", "person_id": "p9"},
    }
    queries = []

    def sql(query):
        queries.append(query)
        return [r for i, r in rows.items() if i in query] if query.startswith("SELECT") else []

    env.patch("people_sync.somadata.sql", side_effect=sql)
    rows["strava:c"] = {
        "id": "strava:c",
        "source": "strava",
        "status": "ignored",
        "person_id": None,
    }
    reconcile.main(["ignore", "instagram:a", "venmo:b", "strava:c", "--apply"])
    updates = [q for q in queries if q.startswith("UPDATE")]
    assert len(updates) == 1 and "'ignored'" in updates[0] and "instagram:a" in updates[0]
    out = capsys.readouterr().out
    assert "venmo:b" in out and "strava:c is already ignored" in out


def test_merge_repoints_every_cataloged_reference_to_people(env):
    queries = []

    def sql(query):
        queries.append(query)
        if "FROM catalog_properties" in query:
            return [
                {"tbl": "quotes", "col": "person_ids", "type": "multi_ref"},
                {"tbl": "splits", "col": "counterparty", "type": "ref"},
                {"tbl": "person_accounts", "col": "person_id", "type": "ref"},
            ]
        if query.startswith("SELECT id, person_ids FROM quotes"):
            return [
                {"id": "q1", "person_ids": json.dumps(["loser"])},
                {"id": "q2", "person_ids": json.dumps(["survivor", "loser", "other"])},
            ]
        return []

    env.patch("people_sync.somadata.sql", side_effect=sql)
    reconcile._merge_foreign_refs("survivor", "loser", reconcile.Ops(True))
    updates = [q for q in queries if q.startswith("UPDATE")]
    assert any(
        "UPDATE splits SET counterparty = 'survivor'" in q and "'loser'" in q for q in updates
    )
    assert any("UPDATE quotes" in q and "'q1'" in q and '["survivor"]' in q for q in updates)
    assert any("'q2'" in q and '["survivor", "other"]' in q for q in updates)
    assert not any(
        "person_accounts" in q for q in updates
    )  # merge already re-points its own tables
