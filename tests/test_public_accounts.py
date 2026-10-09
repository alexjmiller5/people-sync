"""Public classification must survive ingest and never enter personal triage."""

import sqlite3

import pytest

from people_sync import cli, ledger, somadata, match, reconcile, unfollow


@pytest.fixture
def db(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE people_sync_records (
            id TEXT PRIMARY KEY, source TEXT, source_id TEXT, name TEXT, handle TEXT,
            status TEXT, person_id TEXT, suggested_person_id TEXT, raw TEXT,
            follows_me INT, i_follow INT, first_seen TEXT, last_seen TEXT, deleted_at TEXT);
        CREATE TABLE people (id TEXT, name TEXT, first_name TEXT, last_name TEXT, nickname TEXT, deleted_at TEXT);
        CREATE TABLE person_accounts (id TEXT, person_id TEXT, handle TEXT, platform TEXT, source_id TEXT, url TEXT, display_name TEXT, active INT, notes TEXT, deleted_at TEXT);
        CREATE TABLE organizations (id TEXT PRIMARY KEY, name TEXT, deleted_at TEXT);
        CREATE TABLE public_figures (id TEXT PRIMARY KEY, name TEXT, deleted_at TEXT);
        CREATE TABLE music_festivals (id TEXT PRIMARY KEY, name TEXT, deleted_at TEXT);
        CREATE TABLE public_accounts (
            id TEXT PRIMARY KEY, record_id TEXT, organization_id TEXT,
            public_figure_id TEXT, music_festival_id TEXT, deleted_at TEXT);
        INSERT INTO organizations VALUES ('org-1', 'Synthetic Coffee Van', NULL);
        INSERT INTO public_figures VALUES ('figure-1', 'Synthetic Performer', NULL);
        INSERT INTO music_festivals VALUES ('festival-1', 'Synthetic Festival', NULL);
        INSERT INTO people_sync_records
            (id, source, source_id, name, handle, status, raw, i_follow)
            VALUES ('instagram:synthetic_van', 'instagram', 'synthetic_van',
                    'Synthetic Coffee Van', 'synthetic_van', 'pending', '{}', 1);
    """)

    def sql(query):
        return [dict(r) for r in conn.execute(query).fetchall()]

    def insert(table, rows):
        for row in rows:
            conn.execute(
                f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
                list(row.values()),
            )

    monkeypatch.setattr(somadata, "sql", sql)
    monkeypatch.setattr(somadata, "insert", insert)
    monkeypatch.setattr(ledger, "imported_from_many", lambda *a: None)
    yield conn
    conn.close()


def classify(*args):
    reconcile.main(["public", "instagram:synthetic_van", *args])


@pytest.mark.parametrize(
    "flag,owner,column",
    [
        ("--organization", "org-1", "organization_id"),
        ("--figure", "figure-1", "public_figure_id"),
        ("--festival", "festival-1", "music_festival_id"),
    ],
)
def test_classify_owner_and_repeat_without_duplicate(db, flag, owner, column):
    classify(flag, owner, "--apply")
    classify(flag, owner, "--apply")
    row = dict(db.execute("SELECT * FROM public_accounts").fetchone())
    assert row[column] == owner
    assert row["record_id"] == "instagram:synthetic_van"
    assert db.execute("SELECT count(*) FROM public_accounts").fetchone()[0] == 1
    assert tuple(
        db.execute(
            "SELECT status,person_id,suggested_person_id FROM people_sync_records"
        ).fetchone()
    ) == ("public", None, None)


def test_dry_run_never_writes(db, capsys):
    classify("--organization", "org-1")
    assert db.execute("SELECT count(*) FROM public_accounts").fetchone()[0] == 0
    assert db.execute("SELECT status FROM people_sync_records").fetchone()[0] == "pending"
    assert "DRY-RUN" in capsys.readouterr().out


@pytest.mark.parametrize(
    "mutation,args",
    [
        ("UPDATE organizations SET deleted_at='gone'", ["--organization", "org-1"]),
        ("UPDATE people_sync_records SET deleted_at='gone'", ["--organization", "org-1"]),
        (
            "UPDATE people_sync_records SET status='matched',person_id='person-1'",
            ["--organization", "org-1"],
        ),
        ("UPDATE people_sync_records SET person_id='person-1'", ["--organization", "org-1"]),
        ("UPDATE people_sync_records SET source='apple_contacts'", ["--organization", "org-1"]),
        ("SELECT 1", ["--organization", "missing"]),
    ],
)
def test_invalid_classification_leaves_no_account(db, mutation, args):
    db.execute(mutation)
    with pytest.raises(SystemExit):
        classify(*args, "--apply")
    assert db.execute("SELECT count(*) FROM public_accounts").fetchone()[0] == 0


def test_refuse_reassigning_existing_account(db):
    classify("--organization", "org-1", "--apply")
    with pytest.raises(SystemExit):
        classify("--figure", "figure-1", "--apply")
    assert db.execute("SELECT organization_id FROM public_accounts").fetchone()[0] == "org-1"


def test_ignored_can_be_kept_but_public_cannot_be_ignored(db):
    db.execute("UPDATE people_sync_records SET status='ignored'")
    classify("--organization", "org-1", "--apply")
    reconcile.ignore(["instagram:synthetic_van"], reconcile.Ops(True))
    assert db.execute("SELECT status FROM people_sync_records").fetchone()[0] == "public"
    assert unfollow.pending() == []


def test_reingest_preserves_public_resolution(db):
    classify("--organization", "org-1", "--apply")
    result = ledger.upsert(
        [
            ledger.Record(
                source="instagram",
                source_id="synthetic_van",
                handle="synthetic_van",
                name="New public name",
                raw={},
                i_follow=1,
            )
        ]
    )
    assert result == {"new": 0, "updated": 1}
    assert db.execute("SELECT status FROM people_sync_records").fetchone()[0] == "public"
    assert unfollow.pending() == []
    assert (
        db.execute("SELECT count(*) FROM people_sync_records WHERE status='pending'").fetchone()[0]
        == 0
    )


def test_retry_after_account_insert_repairs_status(db):
    db.execute(
        "INSERT INTO public_accounts (id,record_id,organization_id) VALUES ('existing','instagram:synthetic_van','org-1')"
    )
    classify("--organization", "org-1", "--apply")
    assert db.execute("SELECT count(*) FROM public_accounts").fetchone()[0] == 1
    assert db.execute("SELECT status FROM people_sync_records").fetchone()[0] == "public"


def test_tombstone_not_resurrected(db):
    db.execute(
        "INSERT INTO public_accounts (id,record_id,organization_id,deleted_at) VALUES ('instagram:synthetic_van','instagram:synthetic_van','org-1','gone')"
    )
    with pytest.raises(SystemExit):
        classify("--organization", "org-1", "--apply")
    assert db.execute("SELECT status FROM people_sync_records").fetchone()[0] == "pending"


def test_public_record_never_matches_even_with_identical_person_name(db):
    db.execute(
        "INSERT INTO people VALUES ('person-1','Synthetic Coffee Van','Synthetic','Van',NULL,NULL)"
    )
    classify("--organization", "org-1", "--apply")
    assert somadata.sql(cli.QUEUE_QUERY) == []
    assert match.run_match() == {"auto": 0, "suggested": 0, "left_pending": 0}
    assert db.execute("SELECT count(*) FROM person_accounts").fetchone()[0] == 0


@pytest.mark.parametrize("operation", ["create", "link"])
def test_interrupted_public_classification_refuses_personal_writes(db, monkeypatch, operation):
    db.execute(
        "INSERT INTO public_accounts (id,record_id,organization_id) VALUES ('existing','instagram:synthetic_van','org-1')"
    )
    db.execute(
        "INSERT INTO people VALUES ('person-1','Synthetic Coffee Van','Synthetic','Van',NULL,NULL)"
    )
    db.execute("ALTER TABLE people ADD COLUMN notes TEXT")

    def forbidden(*args, **kwargs):
        pytest.fail("personal side effect before public ownership was checked")

    monkeypatch.setattr(reconcile.notion_people, "new_person_id", forbidden)
    monkeypatch.setattr(somadata, "insert", forbidden)
    if operation == "create":
        args = ["create", "instagram:synthetic_van", "--name", "Synthetic Coffee Van", "--apply"]
    else:
        args = ["link", "person-1", "instagram:synthetic_van", "--apply"]
    with pytest.raises(SystemExit, match="public account"):
        reconcile.main(args)
