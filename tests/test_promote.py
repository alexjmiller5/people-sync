import json

from people_sync import promote


def _profile(**kw):
    base = {
        "record_id": "facebook:r1",
        "platform": "facebook",
        "person_id": "p1",
        "location": "Testville, Michigan",
        "work": ["TestCo"],
        "birthday": "2002-12-09",
        "avatar_r2_key": "photos/records/facebook/r1-abc.jpg",
        "avatar_sha256": "abc",
        "scraped_at": "2026-09-08T00:00:00Z",
    }
    base.update(kw)
    return base


PEOPLE = {
    "p1": {"id": "p1", "name": "Test Person", "birthday": None, "slightly_known_birthday": None}
}


def test_plan_fills_every_empty_fact_with_one_op_each():
    ops = promote.plan([_profile()], PEOPLE, [], [], [], set())
    assert [(o.kind, o.value) for o in ops] == [
        ("location", "Testville, Michigan"),
        ("employment", "TestCo"),
        ("birthday", "2002-12-09"),
        ("photo", "photos/records/facebook/r1-abc.jpg"),
    ]


def test_plan_skips_what_the_estate_already_has():
    ops = promote.plan(
        [_profile()],
        {"p1": {**PEOPLE["p1"], "birthday": "2002-12-09"}},
        [{"person_id": "p1", "city": "testville michigan", "end": None}],
        [{"person_id": "p1", "company": "TESTCO", "end": None}],
        [{"person_id": "p1", "sha256": "abc"}],
        set(),
    )
    assert ops == []


def test_plan_reports_conflicts_instead_of_overwriting():
    ops = promote.plan(
        [_profile(birthday="1999-01-01")],
        {"p1": {**PEOPLE["p1"], "birthday": "2002-12-09"}},
        [{"person_id": "p1", "city": "Elsewhere", "end": None}],
        [],
        [],
        set(),
    )
    conflicts = [(o.kind, o.detail["field"], o.value) for o in ops if o.kind == "conflict"]
    assert conflicts == [
        ("conflict", "city", "Testville, Michigan"),
        ("conflict", "birthday", "1999-01-01"),
    ]


def test_plan_upgrades_a_year_unknown_birthday_and_takes_month_only_into_slightly_known():
    ops = promote.plan(
        [_profile(birthday="2002-12-09")],
        {"p1": {**PEOPLE["p1"], "birthday": "--12-09"}},
        [],
        [],
        [{"person_id": "p1", "sha256": "abc"}],
        set(),
    )
    assert [(o.kind, o.value) for o in ops if o.kind.startswith("birthday")] == [
        ("birthday", "2002-12-09")
    ]
    ops = promote.plan(
        [
            _profile(
                platform="partiful",
                record_id="partiful:u1",
                birthday="--08",
                location=None,
                work=None,
            )
        ],
        PEOPLE,
        [],
        [],
        [{"person_id": "p1", "sha256": "abc"}],
        set(),
    )
    assert [(o.kind, o.value) for o in ops] == [("birthday_month", "--08")]


def test_plan_is_idempotent_through_existing_edges():
    edges = {
        "people_sync_profiles:facebook:r1:loc:p1:facebook:r1",
        "people_sync_profiles:facebook:r1:emp:p1:facebook:r1",
        "people_sync_profiles:facebook:r1:p1:birthday",
    }
    ops = promote.plan([_profile()], PEOPLE, [], [], [{"person_id": "p1", "sha256": "abc"}], edges)
    assert ops == []


def test_apply_writes_rows_and_one_provenance_edge_per_value(mocker):
    inserts = mocker.patch("people_sync.promote.lifedata.insert")
    sql = mocker.patch("people_sync.promote.lifedata.sql")
    ops = promote.plan([_profile()], PEOPLE, [], [], [], set())

    counts = promote.apply(ops)

    assert counts == {"location": 1, "employment": 1, "birthday": 1, "photo": 1}
    tables = [c.args[0] for c in inserts.call_args_list]
    assert tables == ["person_locations", "person_employments", "person_photos", "provenance"]
    edges = inserts.call_args_list[-1].args[1]
    assert {e["to_kind"] for e in edges} == {
        "person_locations",
        "person_employments",
        "people",
        "person_photos",
    }
    assert all(
        e["from_kind"] == "people_sync_profiles"
        and e["from_ref"] == "facebook:r1"
        and e["rel"] == "evidence_of"
        for e in edges
    )
    birthday_edge = next(e for e in edges if e["to_kind"] == "people")
    assert (
        birthday_edge["id"] == "people_sync_profiles:facebook:r1:p1:birthday"
        and birthday_edge["field"] == "birthday"
    )
    assert json.loads(birthday_edge["detail"]) == {"cue": "2002-12-09", "confidence": "high"}
    assert "UPDATE people SET birthday = '2002-12-09'" in sql.call_args.args[0]


def test_run_dry_run_prints_the_plan_and_writes_nothing(mocker):
    mocker.patch(
        "people_sync.promote.load_state", return_value=([_profile()], PEOPLE, [], [], [], set())
    )
    inserts = mocker.patch("people_sync.promote.lifedata.insert")

    summary = promote.run(apply_writes=False)

    assert summary["planned"] == {"location": 1, "employment": 1, "birthday": 1, "photo": 1}
    assert summary["applied"] == {} and summary["conflicts"] == []
    inserts.assert_not_called()
