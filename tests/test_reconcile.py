import json

import httpx
import pytest

import reconcile

GROUPS = {"contactGroups/1": "Family", "contactGroups/2": "ΣAE"}
NOW = "2026-01-01T00:00:00.000Z"


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
    """Route lifedata.sql() reads by table; UPDATEs return nothing, like the real backend."""
    tables = {"people": people, "contact_records": records, **children}

    def _fn(query):
        if query.startswith("UPDATE"):
            return []
        for table, rows in tables.items():
            if f"FROM {table}" in query:
                return list(rows)
        return []

    return _fn


@pytest.fixture
def env(mocker, monkeypatch):
    """Everything external mocked: no life-data, no gog, no Notion, no clock drift."""
    monkeypatch.delenv("NOTION_API_TOKEN", raising=False)
    mocker.patch("reconcile.user_groups", return_value=GROUPS)
    mocker.patch("contact_sync.lifedata.now_iso", return_value=NOW)
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
        "contact_sync.lifedata.sql", side_effect=_router(people=[person], records=[_record()])
    )
    insert = env.patch("contact_sync.lifedata.insert")

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
    ledger = next(w for w in _writes(sql) if w.startswith("UPDATE contact_records"))
    assert "status = 'matched'" in ledger and "person_id = 'p1'" in ledger


def test_link_birthday_conflict_never_overwrites(env, capsys):
    person = _person(birthday="1991-08-09")
    sql = env.patch(
        "contact_sync.lifedata.sql", side_effect=_router(people=[person], records=[_record()])
    )
    env.patch("contact_sync.lifedata.insert")

    reconcile.main(["link", "p1", "google_contacts:people/c1", "--apply"])

    updates = [w for w in _writes(sql) if w.startswith("UPDATE people")]
    assert not any("birthday" in u for u in updates)
    out = capsys.readouterr().out
    assert "CONFLICT birthday" in out and "1991-08-09" in out and "1990-03-04" in out


def test_link_circle_union_keeps_existing_first(env):
    person = _person(circles=json.dumps(["NYC", "Family"]))
    sql = env.patch(
        "contact_sync.lifedata.sql", side_effect=_router(people=[person], records=[_record()])
    )
    env.patch("contact_sync.lifedata.insert")

    reconcile.main(["link", "p1", "google_contacts:people/c1", "--apply"])

    update = next(w for w in _writes(sql) if w.startswith("UPDATE people"))
    assert json.dumps(["NYC", "Family", "SAE", "chess club"]) in update


def test_link_rename_preserves_old_name_in_nickname(env):
    person = _person(name="N. Quill", nickname=None)
    sql = env.patch(
        "contact_sync.lifedata.sql", side_effect=_router(people=[person], records=[_record()])
    )
    env.patch("contact_sync.lifedata.insert")

    reconcile.main(["link", "p1", "google_contacts:people/c1", "--rename", "--apply"])

    update = next(w for w in _writes(sql) if w.startswith("UPDATE people"))
    assert "name = 'Nova Quill'" in update
    assert "nickname = 'N. Quill'" in update
    assert "aka:" not in update


def test_link_rename_falls_back_to_notes_when_nickname_taken(env):
    person = _person(name="N. Quill", nickname="Novi", notes="knows chess")
    sql = env.patch(
        "contact_sync.lifedata.sql", side_effect=_router(people=[person], records=[_record()])
    )
    env.patch("contact_sync.lifedata.insert")

    reconcile.main(["link", "p1", "google_contacts:people/c1", "--rename", "--apply"])

    update = next(w for w in _writes(sql) if w.startswith("UPDATE people"))
    assert "nickname" not in update
    assert "knows chess" in update and "aka: N. Quill" in update


def test_link_without_rename_leaves_name_alone(env):
    person = _person(name="N. Quill")
    sql = env.patch(
        "contact_sync.lifedata.sql", side_effect=_router(people=[person], records=[_record()])
    )
    env.patch("contact_sync.lifedata.insert")

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
        "contact_sync.lifedata.sql",
        side_effect=_router(people=[_person()], records=[_record()], person_accounts=accounts),
    )
    insert = env.patch("contact_sync.lifedata.insert")

    reconcile.main(["link", "p1", "google_contacts:people/c1", "--apply"])

    insert.assert_not_called()


def test_link_refuses_to_insert_beside_a_legacy_account_without_source_id(env, capsys):
    # pre-backfill rows carry the profile url instead of a source_id: uncomparable,
    # so inserting beside one would give the person two google account rows
    accounts = [
        {"id": "google_contacts:p1", "person_id": "p1", "source_id": None},
    ]
    env.patch(
        "contact_sync.lifedata.sql",
        side_effect=_router(people=[_person()], records=[_record()], person_accounts=accounts),
    )
    insert = env.patch("contact_sync.lifedata.insert")
    warn = env.patch("reconcile.log.warning")

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
        "contact_sync.lifedata.sql", side_effect=_router(people=[_person()], records=[record])
    )
    insert = env.patch("contact_sync.lifedata.insert")

    reconcile.main(["link", "p1", "google_contacts:people/c1", "--apply"])

    assert _writes(sql) == []
    insert.assert_not_called()
    assert "already matched" in capsys.readouterr().out


def test_link_dry_run_emits_no_writes(env):
    sql = env.patch(
        "contact_sync.lifedata.sql", side_effect=_router(people=[_person()], records=[_record()])
    )
    insert = env.patch("contact_sync.lifedata.insert")

    reconcile.main(["link", "p1", "google_contacts:people/c1"])

    assert _writes(sql) == []
    insert.assert_not_called()


# --- merge --------------------------------------------------------------------


def _merge_env(env, survivor, loser, **children):
    return env.patch(
        "contact_sync.lifedata.sql",
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
    assert any("UPDATE contact_records SET person_id = 's1'" in w for w in writes)
    assert any("UPDATE contact_records SET suggested_person_id = 's1'" in w for w in writes)
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


def test_merge_reports_notion_relations(env, monkeypatch, capsys):
    monkeypatch.setenv("NOTION_API_TOKEN", "token")
    _merge_env(env, _person(id="s1"), _person(id="0" * 32))
    resp = env.Mock()
    resp.json.return_value = {"results": [{"url": "https://notion.so/page1"}]}
    post = env.patch("httpx.post", return_value=resp)

    reconcile.main(["merge", "s1", "0" * 32])

    out = capsys.readouterr().out
    assert out.count("NOTION RELATION") == 4
    assert "https://notion.so/page1" in out
    assert "re-point manually" in out
    dashed = "00000000-0000-0000-0000-000000000000"
    filters = [c.kwargs["json"]["filter"] for c in post.call_args_list]
    assert all(f["relation"]["contains"] == dashed for f in filters)
    # filtered by property ID, never by name - a rename in Notion must not silence this
    assert [f["property"] for f in filters] == ["%3FT%40U", "Y%5B%3E%7B", "t%3DJH", "%3DS%60m"]
    assert [c.args[0].rsplit("/", 2)[-2] for c in post.call_args_list] == [
        "0c39fffe-c8c2-43a5-af03-0a378c682c1c",
        "18f03953-a8af-802f-8950-000b03428f8e",
        "19603953-a8af-80af-8803-000be09834a6",
        "24c03953-a8af-8036-8b1b-000bb8d77b03",
    ]


def test_merge_survives_a_notion_api_failure(env, monkeypatch, capsys):
    monkeypatch.setenv("NOTION_API_TOKEN", "token")
    sql = _merge_env(env, _person(id="s1"), _person(id="l1"))
    ok = env.Mock()
    ok.json.return_value = {"results": [{"url": "https://app.notion.com/p/page1"}]}
    warn = env.patch("reconcile.log.warning")
    env.patch("httpx.post", side_effect=[httpx.ConnectError("boom"), ok, ok, ok])

    reconcile.main(["merge", "s1", "l1", "--apply"])

    # the failed DB warns, the other three are still checked, the merge still runs
    assert warn.call_args.args[0] == "notion relation check failed"
    assert warn.call_args.kwargs == {"db": "Gifts", "reason": "ConnectError"}
    out = capsys.readouterr().out
    assert out.count("NOTION RELATION") == 3
    assert any(w.startswith("UPDATE people SET deleted_at") for w in _writes(sql))


def test_merge_skips_notion_check_without_a_token(env, capsys):
    _merge_env(env, _person(id="s1"), _person(id="l1"))
    post = env.patch("httpx.post")

    reconcile.main(["merge", "s1", "l1"])

    post.assert_not_called()
    assert "NOTION RELATION" not in capsys.readouterr().out


def test_merge_refuses_a_person_merged_into_itself(env):
    _merge_env(env, _person(id="s1"), _person(id="s1"))

    with pytest.raises(SystemExit):
        reconcile.main(["merge", "s1", "s1", "--apply"])


# --- create -------------------------------------------------------------------


def test_create_populates_every_field_with_the_dash_stripped_page_id(env):
    page_id = "12345678-90ab-cdef-1234-567890abcdef"
    stub = env.patch("contact_sync.notion_people.create_stub", return_value=page_id)
    sql = env.patch("contact_sync.lifedata.sql", side_effect=_router(records=[_record()]))
    insert = env.patch("contact_sync.lifedata.insert")

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
    ledger = next(w for w in _writes(sql) if w.startswith("UPDATE contact_records"))
    assert "status = 'matched'" in ledger and person["id"] in ledger


def test_create_reports_an_orphaned_notion_page(env, capsys):
    env.patch("contact_sync.notion_people.create_stub", return_value="abc-def")
    env.patch("contact_sync.lifedata.sql", side_effect=_router(records=[_record()]))
    env.patch("contact_sync.lifedata.insert", side_effect=RuntimeError("life is down"))

    with pytest.raises(RuntimeError):
        reconcile.main(["create", "google_contacts:people/c1", "--apply"])

    assert "orphaned notion page abc-def" in capsys.readouterr().err


def test_create_refuses_a_record_that_is_not_pending(env):
    env.patch("contact_sync.lifedata.sql", side_effect=_router(records=[_record(status="ignored")]))
    stub = env.patch("contact_sync.notion_people.create_stub")

    with pytest.raises(SystemExit):
        reconcile.main(["create", "google_contacts:people/c1", "--apply"])

    stub.assert_not_called()


def test_create_dry_run_writes_nothing(env):
    stub = env.patch("contact_sync.notion_people.create_stub")
    sql = env.patch("contact_sync.lifedata.sql", side_effect=_router(records=[_record()]))
    insert = env.patch("contact_sync.lifedata.insert")

    reconcile.main(["create", "google_contacts:people/c1"])

    stub.assert_not_called()
    insert.assert_not_called()
    assert _writes(sql) == []
