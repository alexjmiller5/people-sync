"""A private review must preserve evidence and never execute source content."""

import hashlib
import json
import re
import subprocess

import pytest

from build_review import build_page


def page_data(page):
    return json.loads(
        re.search(
            r'<script id="review-data" type="application/json">(.*?)</script>', page, re.S
        ).group(1)
    )


@pytest.fixture
def proposal_input(tmp_path):
    group = {
        "current_people": [{"id": "person-1"}, {"id": "person-2"}],
        "google_candidates": [{"id": "google_contacts:1"}],
        "profiles": [
            {"record_id": "instagram:1", "bio": "First saved profile"},
            {"record_id": "instagram:1", "bio": "Second saved profile"},
            {"record_id": "person-1", "bio": "Separate record namespace"},
        ],
    }
    source = tmp_path / "context.json"
    source.write_text(json.dumps({"Example": group, "Another": group}))
    key = json.dumps(["Batch 2", "Example"], ensure_ascii=False)
    proposal = {
        "clusters": [
            {
                "label": "Example Person - school",
                "reason": "Exact full name plus same school.",
                "person_ids": ["person-1"],
                "record_ids": ["google_contacts:1", "instagram:1"],
                "uncertainty": "School dates are unknown.",
            }
        ],
        "question": "Where does the remaining person belong?",
    }
    return [("Batch 2", source)], key, proposal


def test_proposal_digest_snapshot_and_unresolved_ids(proposal_input):
    batches, key, proposal = proposal_input
    original = page_data(build_page(batches, {}))
    old_groups = [
        {**g, "name": name, "batch": batch, "key": json.dumps([batch, name], ensure_ascii=False)}
        for batch, path in batches
        for name, g in json.loads(path.read_text()).items()
    ]
    assert (
        original["snapshot_id"]
        == hashlib.sha256(json.dumps(old_groups, sort_keys=True).encode()).hexdigest()
    )
    proposals = {g["key"]: proposal for g in original["groups"]}
    first = page_data(build_page(batches, {}, proposals))
    assert first["snapshot_id"] == original["snapshot_id"]
    assert first["groups"][0]["proposal"] == proposal
    assert first["groups"][0]["unresolved"] == {
        "person_ids": ["person-2"],
        "record_ids": ["person-1"],
    }
    assert len(first["groups"][0]["profiles"]) == 3
    assert original["groups"][0]["proposal"] is None
    assert original["groups"][0]["proposal_id"] is None
    assert original["groups"][0]["unresolved"]["person_ids"] == ["person-1", "person-2"]
    proposals[key] = {**proposal, "question": "A changed question"}
    second = page_data(build_page(batches, {}, proposals))
    assert first["snapshot_id"] == second["snapshot_id"]
    assert first["groups"][0]["proposal_id"] != second["groups"][0]["proposal_id"]
    assert first["groups"][1]["proposal_id"] == second["groups"][1]["proposal_id"]
    assert first == page_data(
        build_page(batches, {}, {g["key"]: proposal for g in first["groups"]})
    )


@pytest.mark.parametrize(
    "change",
    [
        lambda p: [],
        lambda p: None,
        lambda p: {**p, "clusters": {}},
        lambda p: {**p, "question": []},
        lambda p: {"clusters": [None]},
        *[
            lambda p, field=field, value=value: {"clusters": [{**p["clusters"][0], field: value}]}
            for field, value in [
                ("label", 1),
                ("reason", None),
                ("uncertainty", {}),
                ("person_ids", "person-1"),
                ("record_ids", [1]),
                ("person_ids", ["foreign-person"]),
                ("record_ids", ["foreign-record"]),
                ("person_ids", ["google_contacts:1"]),
                ("record_ids", ["person-2"]),
                ("person_ids", ["person-1", "person-1"]),
                ("record_ids", ["instagram:1", "instagram:1"]),
            ]
        ],
        lambda p: {
            "clusters": [
                {"label": "Empty", "reason": "No evidence", "person_ids": [], "record_ids": []}
            ]
        },
        lambda p: {"clusters": [{"person_ids": ["person-1"], "record_ids": []}]},
        lambda p: {"clusters": [p["clusters"][0], p["clusters"][0]]},
        lambda p: {"clusters": [p["clusters"][0], {**p["clusters"][0], "person_ids": []}]},
    ],
)
def test_rejects_invalid_proposals(proposal_input, change):
    batches, key, proposal = proposal_input
    with pytest.raises(ValueError):
        build_page(batches, {}, {key: change(proposal)})


def test_exact_keys_namespaces_and_unsafe_proposal(proposal_input):
    batches, key, proposal = proposal_input
    for invalid in ([], {"foreign": proposal}, {key.replace(", ", ","): proposal}):
        with pytest.raises(ValueError):
            build_page(batches, {}, invalid)
    payload = '</script><img src=x onerror="window.sourceExecuted=true"> & test'
    proposal["clusters"].append(
        {"label": payload, "reason": payload, "person_ids": [], "record_ids": ["person-1"]}
    )
    page = build_page(batches, {}, {key: proposal})
    assert payload not in page
    assert page_data(page)["groups"][0]["proposal"] == proposal


def test_question_without_clusters(proposal_input):
    batches, key, _ = proposal_input
    proposal = {"clusters": [], "question": "Who is this?"}
    group = page_data(build_page(batches, {}, {key: proposal}))["groups"][0]
    assert group["proposal"] == proposal
    assert group["proposal_id"]
    assert group["unresolved"] == {
        "person_ids": ["person-1", "person-2"],
        "record_ids": ["google_contacts:1", "instagram:1", "person-1"],
    }


def test_cli_proposals(proposal_input, tmp_path, monkeypatch):
    from build_review import main

    batches, key, proposal = proposal_input
    photos, proposals, output = (
        tmp_path / name for name in ("photos.json", "proposals.json", "page.html")
    )
    photos.write_text("{}")
    proposals.write_text(json.dumps({key: proposal}))
    monkeypatch.setattr(
        "sys.argv",
        [
            "build_review",
            "--batch",
            batches[0][0],
            str(batches[0][1]),
            "--photos",
            str(photos),
            "--proposals",
            str(proposals),
            "--output",
            str(output),
        ],
    )
    main()
    assert page_data(output.read_text())["groups"][0]["proposal"] == proposal
    assert output.stat().st_mode & 0o777 == 0o600


def test_response_lifecycle_without_browser(proposal_input):
    """Exercise real state functions; DOM layout belongs to the opt-in browser check."""
    batches, key, proposal = proposal_input
    proposals = {g["key"]: proposal for g in page_data(build_page(batches, {}))["groups"]}
    page = build_page(batches, {}, proposals)
    check = r"""
const assert = require('node:assert/strict'), vm = require('node:vm');
const page = require('node:fs').readFileSync(0, 'utf8');
const data = page.match(/<script id="review-data" type="application\/json">(.*?)<\/script>/s)[1];
const code = page.match(/<script>\s*('use strict';.*?)<\/script>/s)[1].split("$('export').onclick")[0];
const items = new Map(), nodes = {'review-data': {textContent: data}};
const legacyKey = 'people-review:v1:' + JSON.parse(data).snapshot_id;
const legacy = ' {"decisions":{"pair":{"choice":"unsure"}},"notes":{"old":"Keep text"},"reviewed":{}} ';
items.set(legacyKey, legacy);
function boot() {
  const context = vm.createContext({Date, console, document: {getElementById: id => nodes[id] ||= {value: ''}},
    localStorage: {getItem: k => items.get(k) ?? null, setItem: (k,v) => items.set(k,v)},
    window: {scrollTo() {}}});
  vm.runInContext(code + '\nrender=()=>{};renderGroups=()=>{};updateStatus=()=>{};focusGroup=()=>{};', context);
  return expression => vm.runInContext(expression, context);
}
let run = boot();
run("$('review-filter').value='pending';submitResponse('approval')");
assert.equal(run('active'), 1, 'approval advances without skipping a pending group');
assert.equal(run('isReviewed(DATA.groups[0])'), true);
run("writeResponse('correction','Keep both people',false)");
assert.equal(run('isReviewed(group())'), false, 'typing is a draft');
run("submitResponse('approval')");
assert.equal(run('active'), 1, 'cannot approve a nonempty correction');
run("submitResponse('correction')");
assert.equal(run('active'), -1, 'pending queue completes without staying on a reviewed group');
const exported = run('JSON.stringify(exportReview())');
run = boot();
assert.equal(run('isReviewed(DATA.groups[0])'), true, 'approval survives reload');
assert.equal(run('isReviewed(DATA.groups[1])'), true, 'correction survives reload');
run('active=1');
run("DATA.groups[1].proposal_id='changed'");
assert.equal(run('isReviewed(group())'), false);
assert.equal(run('state.responses[group().key].text'), 'Keep both people');
assert.equal(run('isReviewed(DATA.groups[0])'), true, 'unrelated approval persists');
run("writeResponse('correction','Keep both people and every circle',false)");
assert.equal(run('state.previous_responses[group().key][0].text'), 'Keep both people');
run('active=0');
run("DATA.groups[0].proposal_id='changed'");
assert.equal(run('isReviewed(group())'), false, 'changed proposal invalidates approval');
run("writeResponse('correction','New correction',false)");
run("writeResponse('correction','',false)");
run("submitResponse('correction')");
assert.equal(run('isReviewed(group())'), false, 'empty correction cannot submit');
run("submitResponse('approval')");
assert.equal(run('isReviewed(DATA.groups[0])'), true);
run('active=0;DATA.groups[0].proposal=null;DATA.groups[0].proposal_id=null');
run("submitResponse('approval')");
assert.equal(run('isReviewed(group())'), false, 'missing proposal cannot be approved');
run("group().proposal={clusters:[],question:'Who is this?'};group().proposal_id='empty'");
run("submitResponse('approval')");
assert.equal(run('isReviewed(group())'), false, 'empty proposal cannot be approved');
run('navigate(1)');
assert.equal(run('isReviewed(group())'), false, 'skip does not mark reviewed');
const result=JSON.parse(run('JSON.stringify(exportReview())'));
assert.equal(result.schema_version, 2);
assert.equal(result.legacy_v1_raw, legacy);
assert.deepEqual(result.legacy_v1, JSON.parse(legacy));
assert.equal(items.get(legacyKey), legacy, 'never overwrite legacy storage');
assert.equal(result.groups[1].response.text, 'Keep both people and every circle');
assert.deepEqual(result.groups[1].unresolved, {person_ids:['person-2'],record_ids:['person-1']});
assert.equal(result.groups[1].proposal_id, 'changed');
assert.equal(JSON.parse(exported).groups[0].response.type, 'approval');
"""
    result = subprocess.run(["node", "-e", check], input=page, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_round_trip_and_unsafe_images(tmp_path):
    payload = '</script><img src=x onerror=alert(1)> & "quoted"'
    group = {
        "current_people": [{"id": "person-1", "name": payload, "circles": '["Circle one"]'}],
        "google_candidates": [{"id": "google_contacts:1", "name": "Example Person"}],
        "profiles": [
            {
                "record_id": "instagram:1",
                "display_name": payload,
                "avatar_r2_key": "photos/records/example.jpg",
            }
        ],
    }
    source = tmp_path / "context.json"
    source.write_text(json.dumps({"Example": group}))
    page = build_page([("Batch 2", source)], {"photos/records/example.jpg": "photos/abc.img"})
    encoded = re.search(
        r'<script id="review-data" type="application/json">(.*?)</script>', page, re.S
    ).group(1)
    data = json.loads(encoded)
    assert data["groups"][0]["profiles"][0]["display_name"] == payload
    assert data["groups"][0]["current_people"][0]["circles"] == '["Circle one"]'
    assert payload not in page
    assert data["photos"] == {"photos/records/example.jpg": "photos/abc.img"}
    assert (
        data["snapshot_id"]
        == json.loads(
            re.search(
                r'<script id="review-data" type="application/json">(.*?)</script>',
                build_page([("Batch 2", source)], {}),
                re.S,
            ).group(1)
        )["snapshot_id"]
    )
    for unsafe in ("https://remote.example/photo", "../secrets", "data:text/html,bad"):
        with pytest.raises(ValueError, match="photo path"):
            build_page([("Batch 2", source)], {"photos/records/example.jpg": unsafe})
