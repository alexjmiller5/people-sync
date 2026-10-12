"""`people-sync photos pick`: synthetic estate rows and a fake People API."""

import json
import re

import pytest

from people_sync import pick

CONTACT_URL = "https://lh3.googleusercontent.com/contacts/AAA=s100"
PROFILE_URL = "https://lh3.googleusercontent.com/a-/BBB=s100"
OWN = {"url": CONTACT_URL, "metadata": {"source": {"type": "CONTACT"}}}
PROFILE = {"url": PROFILE_URL, "metadata": {"source": {"type": "PROFILE"}}}
DEFAULT = {
    "default": True,
    "url": "https://x/default=s100",
    "metadata": {"source": {"type": "CONTACT"}},
}


def _photo(pid, platform, at, n):
    return {"id": f"ph{n}", "person_id": pid, "platform": platform, "fetched_at": at}


class Estate:
    def __init__(self, people, stored=()):
        self.people, self.stored = list(people), list(stored)
        self.updates: list[str] = []

    def sql(self, query):
        if query.lstrip().startswith("UPDATE"):
            self.updates.append(query)
            return []
        if re.search(r"\bFROM person_photos\b", query):
            sha = re.search(r"sha256 = '([0-9a-f]+)'", query)
            if sha:
                return [{"id": "stored-" + sha.group(1)[:6]}]
            return list(self.stored)
        if re.search(r"\bFROM people p\b", query):
            return list(self.people)
        raise AssertionError(query)


@pytest.fixture
def world(mocker):
    def make(people, contacts, stored=()):
        estate = Estate(people, stored)
        mocker.patch("people_sync.somadata.sql", side_effect=estate.sql)
        mocker.patch("people_sync.push.read", return_value=contacts)
        fetch = mocker.patch("people_sync.photos.fetch_url_photo", return_value=b"\xff\xd8 img")
        store = mocker.patch("people_sync.photos.store_photo", return_value="photos/people/k.jpg")
        return estate, fetch, store

    return make


def _person(pid="p1", google="people/c1"):
    return {"id": pid, "name": "Nova Quill", "google_id": google}


def _picked(estate):
    (update,) = estate.updates
    return dict(re.findall(r"WHEN '([^']+)' THEN '([^']+)'", update))


def test_the_photo_already_on_the_google_contact_comes_first(world, capsys):
    stored = [_photo("p1", "instagram", "2026-10-01T00:00:00.000Z", 1)]
    estate, fetch, store = world([_person()], {"people/c1": {"photos": [OWN]}}, stored)
    pick.main(["--apply"])
    fetch.assert_called_once_with("https://lh3.googleusercontent.com/contacts/AAA=s0")
    assert store.call_args.args[:2] == ("p1", "google_contacts")
    assert _picked(estate)["p1"].startswith("stored-")
    assert "PICK Nova Quill (p1): contact" in capsys.readouterr().out


def test_newest_instagram_photo_without_a_contact_photo(world, capsys):
    stored = [
        _photo("p1", "linkedin", "2026-10-05T00:00:00.000Z", 1),
        _photo("p1", "instagram", "2026-09-01T00:00:00.000Z", 2),
        _photo("p1", "instagram", "2026-10-01T00:00:00.000Z", 3),
    ]
    estate, fetch, store = world([_person()], {"people/c1": {"photos": [DEFAULT]}}, stored)
    pick.main(["--apply"])
    assert _picked(estate) == {"p1": "ph3"}
    fetch.assert_not_called()


def test_the_google_profile_picture_before_other_platforms(world, capsys):
    stored = [_photo("p1", "linkedin", "2026-10-05T00:00:00.000Z", 1)]
    estate, fetch, store = world([_person()], {"people/c1": {"photos": [PROFILE, DEFAULT]}}, stored)
    pick.main(["--apply"])
    fetch.assert_called_once_with("https://lh3.googleusercontent.com/a-/BBB=s0")
    assert _picked(estate)["p1"].startswith("stored-")


def test_other_platforms_follow_the_order(world, capsys):
    stored = [
        _photo("p1", "venmo", "2026-10-09T00:00:00.000Z", 1),
        _photo("p1", "facebook", "2026-01-01T00:00:00.000Z", 2),
    ]
    estate, *_ = world([_person()], {"people/c1": {"photos": [DEFAULT]}}, stored)
    pick.main(["--apply"])
    assert _picked(estate) == {"p1": "ph2"}


def test_order_is_an_option(world, capsys):
    stored = [_photo("p1", "instagram", "2026-10-01T00:00:00.000Z", 1)]
    estate, fetch, _ = world([_person()], {"people/c1": {"photos": [OWN]}}, stored)
    pick.main(["--order", "instagram,contact", "--apply"])
    assert _picked(estate) == {"p1": "ph1"}
    fetch.assert_not_called()


def test_dry_run_stores_and_writes_nothing(world, capsys):
    estate, fetch, store = world([_person()], {"people/c1": {"photos": [OWN]}})
    pick.main([])
    out = capsys.readouterr().out
    assert estate.updates == [] and store.call_count == 0 and fetch.call_count == 0
    assert "PICK Nova Quill (p1): contact" in out and "1 to pick" in out


def test_nobody_without_any_photo_is_picked(world, capsys):
    estate, *_ = world([_person()], {"people/c1": {"photos": [DEFAULT]}})
    pick.main(["--apply"])
    assert estate.updates == []
    assert "0 picked, 1 without a photo" in capsys.readouterr().out


def test_an_unreadable_google_photo_falls_through_to_the_next_source(world, mocker, capsys):
    stored = [_photo("p1", "instagram", "2026-10-01T00:00:00.000Z", 1)]
    estate, fetch, _ = world([_person()], {"people/c1": {"photos": [OWN]}}, stored)
    fetch.return_value = None
    pick.main(["--apply"])
    assert _picked(estate) == {"p1": "ph1"}


def test_the_candidates_query_never_overwrites_a_choice():
    assert "contact_photo_id IS NULL" in pick.CANDIDATES_SQL


def test_unknown_order_source_is_refused(world):
    world([_person()], {"people/c1": {"photos": []}})
    with pytest.raises(SystemExit):
        pick.main(["--order", "instagram,myspace"])


def test_updates_go_in_one_statement_per_batch(world, capsys):
    people = [_person(f"p{i}", f"people/c{i}") for i in range(250)]
    stored = [_photo(f"p{i}", "instagram", "2026-10-01T00:00:00.000Z", i) for i in range(250)]
    estate, *_ = world(people, {f"people/c{i}": {"photos": []} for i in range(250)}, stored)
    pick.main(["--apply"])
    assert len(estate.updates) == 2
    assert json.dumps(estate.updates).count("WHEN '") == 250


def test_the_write_never_replaces_a_choice_made_meanwhile(world, capsys):
    stored = [_photo("p1", "instagram", "2026-10-01T00:00:00.000Z", 1)]
    estate, *_ = world([_person()], {"people/c1": {"photos": []}}, stored)
    pick.main(["--apply"])
    (update,) = estate.updates
    assert update.rstrip().endswith("AND contact_photo_id IS NULL")
