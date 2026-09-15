import json

from people_sync import propose


def _group(people=(), google=(), profiles=()):
    return {
        "current_people": [
            {"id": f"p{i}", "name": name, "circles": json.dumps(circles), "notes": None}
            for i, (name, circles) in enumerate(people)
        ],
        "google_candidates": [
            {
                "id": f"google_contacts:people/c{i}",
                "name": name,
                "raw": json.dumps({"org": [{"name": org}] if org else [], "labels": []}),
            }
            for i, (name, org) in enumerate(google)
        ],
        "profiles": [
            {
                "record_id": f"{platform}:{handle}",
                "platform": platform,
                "handle": handle,
                "display_name": name,
                "bio": bio,
                "location": None,
                "hometown": None,
                "education": education,
                "work": None,
            }
            for platform, handle, name, bio, education in profiles
        ],
    }


def _clusters(group, **kw):
    return [
        (sorted(c["person_ids"] + c["record_ids"]), c["reason"], c.get("uncertainty"))
        for c in propose.propose_group(group, **kw)["clusters"]
    ]


def test_handle_spelling_a_name_joins_across_a_surname_typo():
    group = _group(
        google=[("Annabelle Ianonne", "Westport")],
        profiles=[
            ("instagram", "annabelleiannonee", "Annabelle", "nyc | westport", None),
            ("facebook", "annabelle.iannone", "Annabelle Iannone", None, None),
            ("instagram", "annabellesanok", "annabelle", "boston", None),
        ],
    )
    clusters = _clusters(group)
    joined = next(c for c in clusters if "facebook:annabelle.iannone" in c[0])
    assert joined[0] == [
        "facebook:annabelle.iannone",
        "google_contacts:people/c0",
        "instagram:annabelleiannonee",
    ]
    assert "handle" in joined[1] and "surname" in joined[1]
    assert joined[2]  # the surname typo keeps it flagged for the photos
    assert (["instagram:annabellesanok"], None) in [(c[0], None) for c in clusters]


def test_everything_unconnected_is_its_own_person():
    group = _group(
        people=[("James", ["Fall 2021"])],
        google=[("James Brown", None), ("James Green", None)],
        profiles=[("instagram", "jimmy_k", "James K", "surfer", None)],
    )
    clusters = _clusters(group)
    assert len(clusters) == 4
    assert all("separate person" in c[1] for c in clusters)


def test_era_circle_meets_school_cue_only_when_unambiguous():
    bu_bio = ("instagram", "j.smith", "James", "@bualumni '25", None)
    group = _group(people=[("James", ["Fall 2021", "Through Karl"])], profiles=[bu_bio])
    [only] = _clusters(group)
    assert only[0] == ["instagram:j.smith", "p0"] and "shared cue: boston, bu" in only[1].lower()

    # two BU James profiles: the cue no longer decides, both stay separate
    group = _group(
        people=[("James", ["Fall 2021"])],
        profiles=[bu_bio, ("facebook", "james.smith.9", "James", None, ["Boston University"])],
    )
    assert len(_clusters(group)) == 3


def test_google_label_names_become_cues(tmp_path):
    raw = {
        "org": [],
        "labels": [{"contactGroupMembership": {"contactGroupResourceName": "contactGroups/x"}}],
    }
    group = _group(profiles=[("facebook", "amy.k", "Amy", None, ["Staples High School"])])
    group["google_candidates"] = [
        {"id": "google_contacts:people/c9", "name": "Amy", "raw": json.dumps(raw)}
    ]
    assert len(_clusters(group)) == 2
    [only] = _clusters(group, google_groups={"contactGroups/x": "Westport"})
    assert only[0] == ["facebook:amy.k", "google_contacts:people/c9"]


def test_same_platform_accounts_do_not_merge_on_a_shared_cue():
    group = _group(
        profiles=[
            ("instagram", "a_one", "Ava Long", "westport", None),
            ("instagram", "a_two", "Ava Long", "westport", None),
        ]
    )
    assert len(_clusters(group)) == 2


def test_cli_writes_keyed_proposals(tmp_path, capsys):
    ctx = tmp_path / "b.json"
    ctx.write_text(json.dumps({"Ava": _group(google=[("Ava Long", None)])}))
    out = tmp_path / "out" / "proposals.json"
    propose.main(["--batch", "Batch 2", str(ctx), "--output", str(out)])
    data = json.loads(out.read_text())
    assert list(data) == ['["Batch 2", "Ava"]'] and data['["Batch 2", "Ava"]']["clusters"]
    assert oct(out.stat().st_mode & 0o777) == "0o600"


def test_handle_abbreviating_a_surname_outranks_a_bare_place_cue():
    group = _group(
        google=[("Annabelle McGregor", "Boston")],
        profiles=[
            ("instagram", "annabelle_mcg", "Annabelle", None, None),
            ("instagram", "annabellesanok", "annabelle", "boston", None),
        ],
    )
    clusters = _clusters(group)
    joined = next(c for c in clusters if "google_contacts:people/c0" in c[0])
    assert joined[0] == ["google_contacts:people/c0", "instagram:annabelle_mcg"]
    assert "abbreviates" in joined[1] and joined[2]
    assert len(clusters) == 2


def test_list_page_heading_is_not_a_name_and_phone_links_join():
    group = _group(google=[("Caroline Odia", None)])
    group["profiles"] = [
        {
            "record_id": "partiful:abc",
            "platform": "partiful",
            "handle": "abc",
            "display_name": "Mutuals",
            "source_name": "Caroline Odia",
            "bio": None,
            "location": None,
            "hometown": None,
            "education": None,
            "work": None,
        },
        {
            "record_id": "whatsapp:lid-1",
            "platform": "whatsapp",
            "handle": None,
            "display_name": "Caro",
            "source_name": None,
            "bio": None,
            "location": None,
            "hometown": None,
            "education": None,
            "work": None,
        },
    ]
    [only] = _clusters(group, links={"whatsapp:lid-1": ["google_contacts:people/c0"]})
    assert only[0] == ["google_contacts:people/c0", "partiful:abc", "whatsapp:lid-1"]
    assert "same full name" in only[1].lower() and "same phone number" in only[1].lower()


def test_lone_partiful_mutuals_are_excluded_but_confident_joins_stay():
    group = _group(google=[("Caroline Odia", None)])
    group["profiles"] = [
        {
            "record_id": "partiful:a",
            "platform": "partiful",
            "handle": "a",
            "display_name": "Caroline Odia",
            "source_name": "Caroline Odia",
            "bio": None,
            "location": None,
            "hometown": None,
            "education": None,
            "work": None,
        },
        {
            "record_id": "partiful:b",
            "platform": "partiful",
            "handle": "b",
            "display_name": "Caroline Beans",
            "source_name": "Caroline Beans",
            "bio": None,
            "location": None,
            "hometown": None,
            "education": None,
            "work": None,
        },
        {
            "record_id": "facebook:c",
            "platform": "facebook",
            "handle": "dylan.c",
            "display_name": "Caroline Wrong",
            "source_name": "Caroline Right",
            "bio": None,
            "location": None,
            "hometown": None,
            "education": None,
            "work": None,
        },
    ]
    out = propose.propose_group(group)
    ids = [sorted(c["person_ids"] + c["record_ids"]) for c in out["clusters"]]
    assert ["google_contacts:people/c0", "partiful:a"] in ids
    assert out["excluded"] == ["partiful:b"]
    assert any(c["label"].startswith("Caroline Right") for c in out["clusters"])


def test_event_going_partiful_mutual_keeps_a_box():
    group = _group()
    group["profiles"] = [
        {
            "record_id": "partiful:b",
            "platform": "partiful",
            "handle": "b",
            "display_name": "Caroline Beans",
            "source_name": "Caroline Beans",
            "bio": None,
            "location": None,
            "hometown": None,
            "education": None,
            "work": None,
        },
    ]
    assert propose.propose_group(group)["excluded"] == ["partiful:b"]
    out = propose.propose_group(group, events={"partiful:b": ["RIP Derek"]})
    assert "excluded" not in out and "went to RIP Derek" in out["clusters"][0][
        "reason"
    ].lower().replace("rip derek", "RIP Derek")
