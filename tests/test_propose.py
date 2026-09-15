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
