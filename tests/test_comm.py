"""Communication-history import: synthetic stores only, never a native database.

Every fixture handle, body and name below is invented. The canary strings stand in
for message content: no record, local state file, import row or log line may ever
contain one.
"""

import gzip
import hashlib
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from people_sync import comm

COCOA = 978307200
CANARY = "CANARY-BODY-7f3a"
PHONE_A = "+1 (202) 555-0101"  # a person, in iMessage
EMAIL_B = "Person.B@Example.INVALID"
PHONE_C_LOCAL = "2025550103"  # SMS handle without a country code; handle.country = us
SHORT = "55555"  # a business short code
OWN = "+12025550199"
T0 = 800_000_000  # seconds since 2001 (2026-05-08)


def ref(identity: str) -> str:
    return hashlib.sha256(identity.encode()).hexdigest()


REF_A = ref("tel:+12025550101")
REF_B = ref("mailto:Person.B@example.invalid")
REF_C = ref("tel:+12025550103")


def iso(cocoa_seconds: float) -> str:
    return (
        datetime.fromtimestamp(cocoa_seconds + COCOA, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


# --- fixture stores ----------------------------------------------------------


def apple_messages_db(path):
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT, country TEXT, service TEXT);
        CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, guid TEXT, style INT, chat_identifier TEXT,
                           display_name TEXT);
        CREATE TABLE chat_handle_join (chat_id INT, handle_id INT);
        CREATE TABLE chat_message_join (chat_id INT, message_id INT);
        CREATE TABLE message (ROWID INTEGER PRIMARY KEY, guid TEXT, text TEXT, attributedBody BLOB,
            subject TEXT, payload_data BLOB, handle_id INT, service TEXT, date INT,
            is_from_me INT, is_sent INT, is_delivered INT, error INT, item_type INT,
            associated_message_type INT, is_system_message INT, is_service_message INT,
            destination_caller_id TEXT);
        CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY, filename TEXT, transfer_name TEXT);
        """
    )
    db.executemany(
        "INSERT INTO handle VALUES (?,?,?,?)",
        [
            (
                1,
                PHONE_A.replace(" ", "").replace("(", "").replace(")", "").replace("-", ""),
                "us",
                "iMessage",
            ),
            (2, EMAIL_B, None, "iMessage"),
            (3, PHONE_C_LOCAL, "us", "SMS"),
            (4, SHORT, "us", "SMS"),
            (5, OWN, "us", "iMessage"),
        ],
    )
    db.executemany(
        "INSERT INTO chat VALUES (?,?,?,?,?)",
        [
            (1, "iMessage;-;a", 45, "+12025550101", CANARY),
            (2, "iMessage;-;b", 45, EMAIL_B, None),
            (3, "SMS;-;c", 45, PHONE_C_LOCAL, None),
            (4, "iMessage;+;chat9", 43, "chat9", CANARY),  # a group, even with one member
            (5, "SMS;-;short", 45, SHORT, None),
            (6, "iMessage;-;self", 45, OWN, None),
        ],
    )
    db.executemany(
        "INSERT INTO chat_handle_join VALUES (?,?)",
        [(1, 1), (2, 2), (3, 3), (4, 1), (5, 4), (6, 5)],
    )
    ns = 1_000_000_000
    rows = [
        # rowid, guid, handle, service, date, from_me, sent, delivered, error, item, assoc, sys, svc, chat
        (1, "G-1", 1, "iMessage", T0, 1, 1, 1, 0, 0, 0, 0, 0, 1),  # A outbound sent
        (2, "G-2", 1, "iMessage", T0 + 10, 0, 0, 1, 0, 0, 0, 0, 0, 1),  # A inbound
        (3, "G-3", 1, "iMessage", T0 + 20, 0, 0, 1, 0, 0, 2000, 0, 0, 1),  # reaction
        (4, "G-4", 2, "iMessage", T0 + 30, 1, 0, 0, 7, 0, 0, 0, 0, 2),  # failed send to B
        (5, "G-5", 0, "iMessage", T0 + 40, 0, 0, 1, 0, 0, 0, 0, 0, 2),  # B inbound, no handle_id
        (6, "G-6", 3, "SMS", T0 + 50, 1, 1, 0, 0, 0, 0, 0, 0, 3),  # C outbound SMS, sent
        (7, "G-7", 1, "iMessage", T0 + 60, 0, 0, 1, 0, 0, 0, 0, 0, 4),  # group inbound from A
        (8, "G-8", 0, "iMessage", T0 + 70, 1, 1, 1, 0, 0, 0, 0, 0, 4),  # group outbound
        (9, "G-9", 0, "iMessage", T0 + 80, 0, 0, 1, 0, 1, 0, 0, 0, 4),  # group service row
        (10, "G-10", 4, "SMS", T0 + 90, 0, 0, 1, 0, 0, 0, 0, 0, 5),  # short code inbound
        (11, "G-11", 5, "iMessage", T0 + 100, 1, 1, 1, 0, 0, 0, 0, 0, 6),  # self chat
        (12, "G-12", 1, "iMessage", T0 + 110, 0, 0, 1, 0, 0, 0, 0, 0, None),  # no chat
        (13, "G-13", 2, "RCS", T0 + 120, 1, 1, 1, 0, 0, 0, 0, 0, 2),  # B outbound RCS
    ]
    for r in rows:
        rowid, guid, handle, service, date, me, sent, dlv, err, item, assoc, sys_, svc, chat = r
        db.execute(
            "INSERT INTO message VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rowid,
                guid,
                CANARY,
                CANARY.encode(),
                CANARY,
                CANARY.encode(),
                handle,
                service,
                date * ns,
                me,
                sent,
                dlv,
                err,
                item,
                assoc,
                sys_,
                svc,
                OWN if me else None,
            ),
        )
        if chat is not None:
            db.execute("INSERT INTO chat_message_join VALUES (?,?)", (chat, rowid))
    db.execute("INSERT INTO attachment VALUES (1, ?, ?)", (CANARY, CANARY))
    db.commit()
    db.close()


def apple_calls_db(path, extra=()):
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE ZCALLRECORD (Z_PK INTEGER PRIMARY KEY, ZUNIQUE_ID TEXT, ZDATE REAL,
            ZDURATION REAL, ZANSWERED INT, ZORIGINATED INT, ZCALLTYPE INT, ZADDRESS TEXT,
            ZISO_COUNTRY_CODE TEXT, ZDISCONNECTED_CAUSE INT, ZNAME TEXT, ZLOCATION TEXT);
        CREATE TABLE ZHANDLE (Z_PK INTEGER PRIMARY KEY, ZTYPE INT, ZNORMALIZEDVALUE TEXT, ZVALUE TEXT);
        CREATE TABLE Z_2REMOTEPARTICIPANTHANDLES (Z_2REMOTEPARTICIPANTCALLS INT,
            Z_4REMOTEPARTICIPANTHANDLES INT);
        """
    )
    rows = [
        # pk, uid, date, duration, answered, originated, type, address, country, cause, remotes
        (1, "C-1", T0, 61.5, 1, 0, 1, "+12025550101", "us", None, 1),  # A inbound answered
        (2, "C-2", T0 + 100, 300.25, 0, 1, 1, "+12025550101", "us", None, 1),  # A outbound, long
        (3, "C-3", T0 + 200, 0, 0, 0, 16, EMAIL_B, None, 6, 1),  # B facetime audio missed
        (4, "C-4", T0 + 300, 40, 1, 0, 8, EMAIL_B, None, 1, 4),  # group facetime video
        (5, "C-5", T0 + 400, 5, 0, 0, 1, "2025550103", "us", None, 1),  # C missed, ringing time
        (6, "C-6", T0 + 500, 10, 1, 0, 99, "+12025550101", "us", None, 1),  # unknown type
        (7, "C-7", T0 + 600, 10, 1, 0, 1, None, None, None, 0),  # no address
        *extra,
    ]
    for pk, uid, date, dur, ans, orig, typ, addr, country, cause, remotes in rows:
        db.execute(
            "INSERT INTO ZCALLRECORD VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (pk, uid, date, dur, ans, orig, typ, addr, country, cause, CANARY, CANARY),
        )
        for i in range(remotes):
            db.execute("INSERT INTO Z_2REMOTEPARTICIPANTHANDLES VALUES (?,?)", (pk, pk * 10 + i))
    db.commit()
    db.close()


WA_A = "12025550101@s.whatsapp.net"
WA_A_LID = "900000000001@lid"
WA_D_LID = "900000000004@lid"  # phone known only through the account table
WA_E_LID = "900000000005@lid"  # no phone anywhere
WA_SELF = "12025550199@s.whatsapp.net"


def whatsapp_dbs(tmp_path):
    chat = tmp_path / "ChatStorage.sqlite"
    db = sqlite3.connect(chat)
    db.executescript(
        """
        CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZSESSIONTYPE INT, ZCONTACTJID TEXT,
            ZCONTACTIDENTIFIER TEXT, ZPARTNERNAME TEXT, ZLASTMESSAGETEXT TEXT, ZREMOVED INT);
        CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT, ZCONTACTNAME TEXT);
        CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INT, ZISFROMME INT,
            ZMESSAGESTATUS INT, ZMESSAGEERRORSTATUS INT, ZMESSAGETYPE INT, ZMESSAGEDATE REAL,
            ZSTANZAID TEXT, ZGROUPMEMBER INT, ZFROMJID TEXT, ZTEXT TEXT, ZPUSHNAME TEXT);
        """
    )
    db.executemany(
        "INSERT INTO ZWACHATSESSION VALUES (?,?,?,?,?,?,?)",
        [
            (1, 0, WA_A_LID, WA_A, CANARY, CANARY, 0),  # A, lid-form session
            (2, 0, WA_D_LID, None, CANARY, CANARY, 0),  # D, phone via account table
            (3, 1, "120363000000000001@g.us", None, CANARY, CANARY, 0),  # group
            (4, 3, "status@broadcast", None, CANARY, CANARY, 0),  # status
            (5, 0, WA_SELF, None, CANARY, CANARY, 0),  # message yourself
            (6, 0, WA_E_LID, None, CANARY, CANARY, 1),  # removed chat, lid only
        ],
    )
    db.executemany("INSERT INTO ZWAGROUPMEMBER VALUES (?,?,?)", [(1, WA_D_LID, CANARY)])
    msgs = [
        # pk, session, me, status, err, type, date, stanza, member
        (1, 1, 1, 6, 0, 0, T0, "W-1", None),  # A outbound settled
        (2, 1, 0, 6, 0, 1, T0 + 10, "W-2", None),  # A inbound image
        (3, 1, 1, 1, 0, 0, T0 + 20, "W-3", None),  # A outbound in flight
        (4, 2, 1, 8, 0, 0, T0 + 30, "W-4", None),  # D outbound settled
        (5, 2, 1, 6, 3, 0, T0 + 40, "W-5", None),  # D outbound error
        (6, 3, 0, 6, 0, 0, T0 + 50, "W-6", 1),  # group inbound from D
        (7, 3, 0, 0, 0, 6, T0 + 60, "W-7", None),  # group system row
        (8, 4, 0, 6, 0, 0, T0 + 70, "W-8", None),  # status post
        (9, 5, 1, 6, 0, 0, T0 + 80, "W-9", None),  # self
        (10, 1, 0, 0, 0, 59, T0 + 90, "CALL-2", None),  # call row for call CALL-2
        (11, 1, 0, 6, 0, 66, T0 + 100, "W-11", None),  # unknown type
        (12, 6, 0, 6, 0, 0, T0 + 110, "W-12", None),  # removed chat, lid-only peer
        (13, None, 0, 6, 0, 0, T0 + 120, "W-13", None),  # no session
    ]
    for pk, session, me, status, err, typ, date, stanza, member in msgs:
        db.execute(
            "INSERT INTO ZWAMESSAGE VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (pk, session, me, status, err, typ, date, stanza, member, CANARY, CANARY, CANARY),
        )
    db.commit()
    db.close()

    calls = tmp_path / "CallHistory.sqlite"
    db = sqlite3.connect(calls)
    db.executescript(
        """
        CREATE TABLE ZWAAGGREGATECALLEVENT (Z_PK INTEGER PRIMARY KEY, ZINCOMING INT, ZMISSED INT,
            ZMISSEDREASON INT, ZVIDEO INT, ZFIRSTDATE REAL);
        CREATE TABLE ZWACDCALLEVENT (Z_PK INTEGER PRIMARY KEY, Z1CALLEVENTS INT, ZOUTCOME INT,
            ZDATE REAL, ZDURATION REAL, ZCALLIDSTRING TEXT, ZGROUPJIDSTRING TEXT);
        CREATE TABLE ZWACDCALLEVENTPARTICIPANT (Z_PK INTEGER PRIMARY KEY, Z1PARTICIPANTS INT,
            ZOUTCOME INT, ZJIDSTRING TEXT);
        """
    )
    db.executemany(
        "INSERT INTO ZWAAGGREGATECALLEVENT VALUES (?,?,?,?,?,?)",
        [
            (1, 0, 0, 1, 0, T0),
            (2, 1, 1, 1, 0, T0),
            (3, 1, 0, 1, 1, T0),
            (4, 0, 0, 1, 0, T0),
            (5, 1, 0, 1, 0, T0),
            (6, 1, 0, 1, 0, T0),
        ],
    )
    events = [
        # pk, agg, outcome, date, duration, call id, group jid, participants
        (1, 1, 0, T0 + 1000, 120.0, "CALL-1", None, [WA_A_LID]),  # A outbound connected
        (2, 2, 1, T0 + 1100, 3.0, "CALL-2", None, []),  # inbound missed, peer via call row
        (3, 3, 0, T0 + 1200, 50.0, "CALL-3", "120363000000000001@g.us", [WA_D_LID]),  # group
        (4, 4, 1, T0 + 1300, 0.0, "CALL-4", None, [WA_D_LID]),  # outbound not connected
        (5, 5, 5, T0 + 1400, 0.0, "CALL-5", None, [WA_D_LID]),  # inbound, outcome unknown
        (6, 6, 0, T0 + 1500, 9.0, "CALL-6", None, []),  # no peer anywhere
    ]
    for pk, agg, outcome, date, dur, cid, group, parts in events:
        db.execute(
            "INSERT INTO ZWACDCALLEVENT VALUES (?,?,?,?,?,?,?)",
            (pk, agg, outcome, date, dur, cid, group),
        )
        for jid in parts:
            db.execute(
                "INSERT INTO ZWACDCALLEVENTPARTICIPANT (Z1PARTICIPANTS, ZOUTCOME, ZJIDSTRING) "
                "VALUES (?,?,?)",
                (pk, outcome, jid),
            )
    db.commit()
    db.close()

    lid = tmp_path / "LID.sqlite"
    db = sqlite3.connect(lid)
    db.execute(
        "CREATE TABLE ZWAZACCOUNT (Z_PK INTEGER PRIMARY KEY, ZIDENTIFIER TEXT, ZPHONENUMBER TEXT, "
        "ZDISPLAYNAME TEXT)"
    )
    db.executemany(
        "INSERT INTO ZWAZACCOUNT VALUES (?,?,?,?)",
        [(1, WA_D_LID, "12025550104", CANARY), (2, WA_A_LID, "12025550101", CANARY)],
    )
    db.commit()
    db.close()
    return {"whatsapp_messages": chat, "whatsapp_calls": calls, "whatsapp_lid": lid}


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(comm, "_sleep", lambda seconds: None)


@pytest.fixture
def stores(tmp_path):
    apple_messages_db(tmp_path / "chat.db")
    apple_calls_db(tmp_path / "CallHistory.storedata")
    return {
        "apple_messages": tmp_path / "chat.db",
        "apple_calls": tmp_path / "CallHistory.storedata",
        **whatsapp_dbs(tmp_path),
    }


class FakeHub:
    def __init__(self, fail_after=None):
        self.streams: dict[str, list[dict]] = {}
        self.calls = 0
        self.fail_after = fail_after

    def __call__(self, stream, records):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise comm.AppendError("HTTP Error 403: Forbidden")
        assert 0 < len(records) <= comm.BATCH
        self.streams.setdefault(stream, []).extend(json.loads(json.dumps(records)))


PERSON_A, PERSON_B, PERSON_D = "a" * 32, "b" * 32, "d" * 32
RESOLVER = comm.Resolver(
    {REF_A: {PERSON_A}, REF_B: {PERSON_B}, ref("tel:+12025550104"): {PERSON_D}}
)


def run(source, stores, state, hub, **kw):
    return comm.run_import(
        source,
        stores=stores,
        state=comm.State(state),
        resolver=kw.pop("resolver", RESOLVER),
        append=hub,
        own_whatsapp=kw.pop("own_whatsapp", WA_SELF),
        now=kw.pop("now", datetime(2026, 10, 1, tzinfo=timezone.utc)),
        **kw,
    )


def by_id(hub, source):
    return {r["event_id"]: r for r in hub.streams.get(comm.STREAM[source], [])}


# --- identity ------------------------------------------------------------------


def test_identity_matches_the_findmy_source_handle_key_convention():
    assert comm.identity("+1 (202) 555-0101") == "tel:+12025550101"
    assert comm.identity("Person.B@Example.INVALID") == "mailto:Person.B@example.invalid"
    assert comm.identity("2025550103", "us") == "tel:+12025550103"
    assert comm.identity("2025550103") == "other:2025550103"  # no country, no guess
    assert comm.identity("55555", "us") == "other:55555"
    assert comm.ref("tel:+12025550101") == REF_A
    assert len(comm.ref("x")) == 64


def test_resolver_returns_only_unique_people():
    r = comm.Resolver({"x": {"p1"}, "y": {"p1", "p2"}})
    assert r.person("x") == "p1"
    assert r.person("y") is None  # ambiguous
    assert r.person("z") is None
    assert r.person(None) is None


def test_build_resolver_goes_through_confirmed_account_links_only():
    accounts = [
        {"platform": "apple_contacts", "source_id": "U1:ABPerson", "person_id": PERSON_A},
        {"platform": "google_contacts", "source_id": "people/c9", "person_id": PERSON_B},
        {"platform": "whatsapp", "source_id": "lid-900000000004", "person_id": PERSON_D},
    ]
    contacts = [
        {"apple": "U1:ABPerson", "external": None, "phones": ["(202) 555-0101"], "emails": []},
        {"apple": "U2:ABPerson", "external": "g9", "phones": [], "emails": [EMAIL_B]},
        {"apple": "U3:ABPerson", "external": None, "phones": ["+1 202 555 0177"], "emails": []},
    ]
    r = comm.build_resolver(
        accounts,
        contacts,
        google_records={"g9": "google_contacts:people/c9"},
        whatsapp_pairs={WA_D_LID: "12025550104"},
    )
    assert r.person(REF_A) == PERSON_A  # US default for a bare 10-digit address-book number
    assert r.person(REF_B) == PERSON_B  # through the CardDAV external id to Google
    assert r.person(ref("tel:+12025550104")) == PERSON_D  # through the WhatsApp account
    assert r.person(ref("whatsapp-lid:900000000004")) == PERSON_D
    assert r.person(ref("tel:+12025550177")) is None  # a contact with no confirmed link


# --- metadata-only guard -----------------------------------------------------------


def test_store_guard_refuses_body_columns(stores):
    with comm.opened({"main": stores["apple_messages"]}, comm.ALLOWED["apple_messages"]) as conn:
        assert conn.execute("SELECT count(*) FROM message").fetchone()[0] == 13
        for column in ("text", "attributedBody", "subject", "payload_data"):
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute(f"SELECT {column} FROM message").fetchall()
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("SELECT guid FROM message WHERE text LIKE '%'").fetchall()
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("SELECT filename FROM attachment").fetchall()
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("SELECT display_name FROM chat").fetchall()


def test_store_guard_refuses_whatsapp_and_call_content(stores):
    paths = {"main": stores["whatsapp_messages"], "lid": stores["whatsapp_lid"]}
    with comm.opened(paths, comm.ALLOWED["whatsapp"]) as conn:
        for sql in (
            "SELECT ZTEXT FROM ZWAMESSAGE",
            "SELECT ZPUSHNAME FROM ZWAMESSAGE",
            "SELECT ZPARTNERNAME FROM ZWACHATSESSION",
            "SELECT ZLASTMESSAGETEXT FROM ZWACHATSESSION",
            "SELECT ZCONTACTNAME FROM ZWAGROUPMEMBER",
            "SELECT ZDISPLAYNAME FROM ZWAZACCOUNT",
        ):
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute(sql).fetchall()
    with comm.opened({"main": stores["apple_calls"]}, comm.ALLOWED["apple_calls"]) as conn:
        for sql in ("SELECT ZNAME FROM ZCALLRECORD", "SELECT ZLOCATION FROM ZCALLRECORD"):
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute(sql).fetchall()


def test_opened_reads_a_private_snapshot_never_the_live_file(stores, tmp_path):
    live = stores["apple_calls"]
    before = live.read_bytes()
    with comm.opened({"main": live}, comm.ALLOWED["apple_calls"]) as conn:
        path = conn.execute("PRAGMA database_list").fetchall()[0][2]
        assert path and path != str(live)
    assert live.read_bytes() == before


def test_opened_reads_a_gzipped_backup(tmp_path):
    raw = tmp_path / "raw.db"
    apple_calls_db(raw)
    gz = tmp_path / "callhistory.db.gz"
    gz.write_bytes(gzip.compress(raw.read_bytes()))
    with comm.opened({"main": gz}, comm.ALLOWED["apple_calls"]) as conn:
        assert conn.execute("SELECT count(*) FROM ZCALLRECORD").fetchone()[0] == 7


# --- the body canary -----------------------------------------------------------------


def test_no_body_text_or_raw_handle_reaches_any_output(stores, tmp_path, capsys):
    hub = FakeHub()
    state = tmp_path / "state"
    reports = [run(s, stores, state, hub) for s in comm.SOURCES]
    rows = comm.local_aggregates(comm.State(state))
    out = capsys.readouterr()
    blobs = [
        json.dumps(hub.streams),
        json.dumps(reports),
        json.dumps(rows),
        out.out + out.err,
        *(p.read_bytes().decode("latin-1") for p in state.rglob("*") if p.is_file()),
    ]
    raw_handles = [
        CANARY,
        "202555",
        "5550101",
        "Person.B",
        "example.invalid",
        "55555",
        "900000000",
        "s.whatsapp.net",
        "@lid",
        "@g.us",
    ]
    for blob in blobs:
        for needle in raw_handles:
            assert needle not in blob, needle


# --- Apple Messages --------------------------------------------------------------------


def test_apple_messages_rules(stores, tmp_path):
    hub = FakeHub()
    report = run("apple_messages", stores, tmp_path / "state", hub)
    got = by_id(hub, "apple_messages")
    assert set(got) == {"G-1", "G-2", "G-5", "G-6", "G-7", "G-8", "G-10", "G-13"}
    a_out = got["G-1"]
    assert a_out == {
        "v": 1,
        "event_id": "G-1",
        "at": iso(T0),
        "channel": "imessage",
        "direction": "outbound",
        "conversation_kind": "direct",
        "participant_ref": REF_A,
        "person_id": PERSON_A,
        "chat_ref": comm.ref("iMessage;-;a"),
        "instance": "live",
    }
    assert got["G-2"]["direction"] == "inbound"
    assert got["G-5"]["participant_ref"] == REF_B  # direct chat's own handle, not handle_id 0
    assert got["G-6"]["channel"] == "sms" and got["G-6"]["participant_ref"] == REF_C
    assert got["G-6"]["person_id"] is None  # C is not linked to anyone
    assert got["G-7"]["conversation_kind"] == "group"  # chat style, not member count
    assert got["G-7"]["participant_ref"] == REF_A
    assert got["G-8"]["participant_ref"] is None  # outbound to a group names no one
    assert got["G-10"]["participant_ref"] == comm.ref("other:55555")
    assert got["G-13"]["channel"] == "rcs"
    assert report["observed_rows"] == 13
    assert report["excluded_by_reason"] == {
        "reaction": 1,
        "failed": 1,
        "service": 1,
        "self": 1,
        "no-chat": 1,
    }
    assert report["observed_rows"] == report["accepted"] + report["excluded"] + report["duplicates"]
    assert report["event_min_at"] == iso(T0) and report["event_max_at"] == iso(T0 + 120)
    assert report["coverage"] == "complete"
    assert report["resolution"] == {"resolved": 2, "unresolved": 2}


def test_a_direct_chat_without_exactly_one_handle_is_not_counted(stores, tmp_path):
    db = sqlite3.connect(stores["apple_messages"])
    db.execute("INSERT INTO chat (ROWID, guid, style) VALUES (7, 'iMessage;-;odd', 45)")
    db.executemany("INSERT INTO chat_handle_join VALUES (?,?)", [(7, 1), (7, 2)])
    db.execute(
        "INSERT INTO message (ROWID, guid, handle_id, service, date, is_from_me, is_sent, "
        "is_delivered, error, item_type, associated_message_type, is_system_message, "
        "is_service_message) VALUES (14, 'G-14', 1, 'iMessage', ?, 0, 0, 1, 0, 0, 0, 0, 0)",
        ((T0 + 200) * 1_000_000_000,),
    )
    db.execute("INSERT INTO chat_message_join VALUES (7, 14)")
    db.commit()
    hub = FakeHub()
    report = run("apple_messages", stores, tmp_path / "state", hub)
    assert "G-14" not in by_id(hub, "apple_messages")
    assert report["excluded_by_reason"]["other-chat"] == 1


def test_pending_apple_send_is_held_back_until_it_settles(stores, tmp_path):
    db = sqlite3.connect(stores["apple_messages"])
    db.execute(
        "INSERT INTO message (ROWID, guid, handle_id, service, date, is_from_me, is_sent, "
        "is_delivered, error, item_type, associated_message_type, is_system_message, "
        "is_service_message) VALUES (14, 'G-14', 1, 'iMessage', ?, 1, 0, 0, 0, 0, 0, 0, 0)",
        ((T0 + 200) * 1_000_000_000,),
    )
    db.execute("INSERT INTO chat_message_join VALUES (1, 14)")
    db.commit()
    hub, state = FakeHub(), tmp_path / "state"
    now = datetime.fromtimestamp(T0 + 300 + COCOA, tz=timezone.utc)
    first = run("apple_messages", stores, state, hub, now=now)
    assert first["excluded_by_reason"]["pending"] == 1
    db.execute("UPDATE message SET is_sent = 1 WHERE ROWID = 14")
    db.commit()
    second = run("apple_messages", stores, state, hub, now=now)
    assert second["observed_rows"] == 1 and second["accepted"] == 1
    assert "G-14" in by_id(hub, "apple_messages")


# --- Apple calls ---------------------------------------------------------------------------


def test_apple_call_rules(stores, tmp_path):
    hub = FakeHub()
    report = run("apple_calls", stores, tmp_path / "state", hub)
    got = by_id(hub, "apple_calls")
    assert set(got) == {"C-1", "C-2", "C-3", "C-4", "C-5"}
    assert got["C-1"] == {
        "v": 1,
        "event_id": "C-1",
        "at": iso(T0),
        "channel": "phone",
        "direction": "inbound",
        "conversation_kind": "direct",
        "participant_ref": REF_A,
        "person_id": PERSON_A,
        "outcome": "answered",
        "duration_seconds": 61.5,
        "source_outcome": {"answered": 1, "disconnected_cause": None},
        "instance": "live",
    }
    # Apple never marks an outgoing call answered: a long duration is not an outcome.
    assert got["C-2"]["outcome"] == "unknown" and got["C-2"]["duration_seconds"] == 300.25
    assert got["C-3"]["channel"] == "facetime_audio" and got["C-3"]["outcome"] == "missed"
    assert got["C-4"]["channel"] == "facetime_video"
    assert got["C-4"]["conversation_kind"] == "group"
    assert got["C-5"]["outcome"] == "missed" and got["C-5"]["participant_ref"] == REF_C
    assert report["excluded_by_reason"] == {"unknown-type": 1, "no-participant": 1}


# --- WhatsApp -----------------------------------------------------------------------------


def test_whatsapp_message_rules(stores, tmp_path):
    hub = FakeHub()
    report = run("whatsapp_messages", stores, tmp_path / "state", hub)
    got = by_id(hub, "whatsapp_messages")
    assert set(got) == {"W-1", "W-2", "W-4", "W-6", "W-12"}
    assert got["W-1"] == {
        "v": 1,
        "event_id": "W-1",
        "at": iso(T0),
        "channel": "whatsapp",
        "direction": "outbound",
        "conversation_kind": "direct",
        "participant_ref": REF_A,  # the phone form, shared with iMessage and calls
        "person_id": PERSON_A,
        "chat_ref": comm.ref(WA_A_LID),
        "source_status": 6,
        "instance": "live",
    }
    assert got["W-4"]["participant_ref"] == ref("tel:+12025550104")  # via the account table
    assert got["W-4"]["person_id"] == PERSON_D
    assert got["W-6"]["conversation_kind"] == "group"
    assert got["W-6"]["participant_ref"] == ref("tel:+12025550104")
    assert got["W-12"]["participant_ref"] == ref("whatsapp-lid:900000000005")
    assert report["excluded_by_reason"] == {
        "pending": 1,
        "failed": 1,
        "service": 1,
        "status-broadcast": 1,
        "self": 1,
        "call-row": 1,
        "other-type": 1,
        "no-chat": 1,
    }
    assert report["coverage"] == "partial"
    assert "partial mirror" in report["reason"]


def test_whatsapp_call_rules(stores, tmp_path):
    hub = FakeHub()
    report = run("whatsapp_calls", stores, tmp_path / "state", hub)
    got = by_id(hub, "whatsapp_calls")
    assert set(got) == {"CALL-1", "CALL-2", "CALL-3", "CALL-4", "CALL-5"}
    assert got["CALL-1"] == {
        "v": 1,
        "event_id": "CALL-1",
        "at": iso(T0 + 1000),
        "channel": "whatsapp",
        "direction": "outbound",
        "conversation_kind": "direct",
        "participant_ref": REF_A,
        "person_id": PERSON_A,
        "media": "audio",
        "outcome": "answered",
        "duration_seconds": 120.0,
        "source_outcome": {"outcome": 0, "missed": 0},
        "instance": "live",
    }
    # no participant rows: the peer comes from the call's own row in the chat
    assert got["CALL-2"]["participant_ref"] == REF_A
    assert got["CALL-2"]["direction"] == "inbound" and got["CALL-2"]["outcome"] == "missed"
    assert got["CALL-3"]["conversation_kind"] == "group" and got["CALL-3"]["media"] == "video"
    assert got["CALL-4"]["outcome"] == "unanswered"
    assert got["CALL-5"]["outcome"] == "unknown"
    assert report["excluded_by_reason"] == {"no-participant": 1}


# --- idempotency, checkpoint, outbox --------------------------------------------------------


def test_rerun_appends_nothing_and_full_rerun_counts_duplicates(stores, tmp_path):
    hub, state = FakeHub(), tmp_path / "state"
    first = run("apple_messages", stores, state, hub)
    again = run("apple_messages", stores, state, hub)
    assert again["observed_rows"] == 0 and again["accepted"] == 0
    full = run("apple_messages", stores, state, hub, full=True)
    assert full["accepted"] == 0 and full["duplicates"] == first["accepted"]
    assert len(hub.streams[comm.STREAM["apple_messages"]]) == first["accepted"]


def test_incremental_run_reads_only_new_rows(stores, tmp_path):
    hub, state = FakeHub(), tmp_path / "state"
    run("apple_calls", stores, state, hub)
    db = sqlite3.connect(stores["apple_calls"])
    db.execute(
        "INSERT INTO ZCALLRECORD (Z_PK, ZUNIQUE_ID, ZDATE, ZDURATION, ZANSWERED, ZORIGINATED, "
        "ZCALLTYPE, ZADDRESS, ZISO_COUNTRY_CODE) VALUES (8, 'C-8', ?, 12, 1, 0, 1, ?, 'us')",
        (T0 + 700, "+12025550101"),
    )
    db.execute("INSERT INTO Z_2REMOTEPARTICIPANTHANDLES VALUES (8, 80)")
    db.commit()
    second = run("apple_calls", stores, state, hub)
    assert second["observed_rows"] == 1 and second["accepted"] == 1


def test_backup_store_dedupes_against_live_events(stores, tmp_path):
    hub, state = FakeHub(), tmp_path / "state"
    run("apple_calls", stores, state, hub)
    backup = tmp_path / "backup.db"
    apple_calls_db(
        backup, extra=[(9, "C-OLD", T0 - 86400, 30, 1, 0, 1, "+12025550101", "us", None, 1)]
    )
    gz = tmp_path / "callhistory.db.gz"
    gz.write_bytes(gzip.compress(backup.read_bytes()))
    report = run("apple_calls", {**stores, "apple_calls": gz}, state, hub, instance="backup:x")
    assert report["accepted"] == 1 and report["duplicates"] == 5
    assert by_id(hub, "apple_calls")["C-OLD"]["instance"] == "backup:x"


def test_refused_append_keeps_events_in_the_outbox_until_a_later_run(stores, tmp_path):
    state = tmp_path / "state"
    refused = FakeHub(fail_after=0)
    first = run("apple_messages", stores, state, refused)
    assert first["accepted"] == 8 and first["landed"] == 0
    assert first["coverage"] == "partial" and "403" in first["reason"]
    hub = FakeHub()
    second = run("apple_messages", stores, state, hub)
    assert second["observed_rows"] == 0 and second["landed"] == 8
    assert second["coverage"] == "complete"
    assert len(by_id(hub, "apple_messages")) == 8


def test_outbox_flushes_in_hub_sized_batches(stores, tmp_path, monkeypatch):
    monkeypatch.setattr(comm, "BATCH", 3)
    hub = FakeHub()
    report = run("apple_messages", stores, tmp_path / "state", hub)
    assert report["landed"] == 8 and hub.calls == 3


def test_unavailable_store_is_reported_not_raised(stores, tmp_path):
    hub = FakeHub()
    report = run(
        "apple_calls", {**stores, "apple_calls": tmp_path / "missing.db"}, tmp_path / "s", hub
    )
    assert report["coverage"] == "unavailable" and report["observed_rows"] == 0
    assert report["reason"] == "store not readable"


# --- refresh ----------------------------------------------------------------------------------


def test_summaries_follow_direct_contact_semantics(stores, tmp_path):
    hub, state = FakeHub(), tmp_path / "state"
    for s in comm.SOURCES:
        run(s, stores, state, hub)
    rows = comm.local_aggregates(comm.State(state))
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    coverage = {c: ("2026-01-01T00:00:00.000Z", "2026-10-01T00:00:00.000Z") for c in comm.CHANNELS}
    out = {s["id"]: s for s in comm.summarize(rows, RESOLVER, coverage, now)}
    a_im = out[f"{PERSON_A}:imessage"]
    assert a_im["last_outbound_sent_at"] == iso(T0)
    assert a_im["last_inbound_received_at"] == iso(T0 + 10)  # the group message does not count
    assert (a_im["outbound_count"], a_im["inbound_count"]) == (1, 1)
    assert a_im["calls_answered"] == 0 and a_im["last_answered_call_at"] is None
    a_phone = out[f"{PERSON_A}:phone"]
    assert a_phone["last_answered_call_at"] == iso(T0)
    assert a_phone["last_answered_call_seconds"] == 61.5
    assert (a_phone["calls_answered"], a_phone["calls_missed"]) == (1, 0)
    assert (a_phone["calls_outbound"], a_phone["calls_inbound"]) == (1, 1)
    assert a_phone["last_call_at"] == iso(T0 + 100)
    assert a_phone["outbound_count"] == 0 and a_phone["last_outbound_sent_at"] is None
    a_wa = out[f"{PERSON_A}:whatsapp"]
    assert a_wa["last_outbound_sent_at"] == iso(T0)  # the in-flight message never counted
    assert a_wa["last_answered_call_at"] == iso(T0 + 1000)
    assert (a_wa["calls_answered"], a_wa["calls_missed"]) == (1, 1)
    b = out[f"{PERSON_B}:facetime_audio"]
    assert b["calls_missed"] == 1 and b["calls_answered"] == 0
    assert f"{PERSON_B}:facetime_video" not in out  # only a group call
    d = out[f"{PERSON_D}:whatsapp"]
    assert d["calls_answered"] == 0 and d["outbound_count"] == 1  # group call/message ignored
    assert a_im["coverage_start"] == "2026-01-01T00:00:00.000Z"
    assert a_im["refreshed_at"] == "2026-10-01T00:00:00.000Z"
    assert a_im["needs_review"] is None
    assert all(s["person_id"] in {PERSON_A, PERSON_B, PERSON_D} for s in out.values())


def test_a_ref_that_moved_to_another_person_needs_review():
    rows = [
        {
            "participant_ref": REF_A,
            "person_id": PERSON_B,
            "channel": "sms",
            "kind": "message",
            "direction": "inbound",
            "outcome": None,
            "n": 2,
            "last_at": iso(T0),
        },
    ]
    out = comm.summarize(rows, RESOLVER, {}, datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert out[0]["person_id"] == PERSON_A
    assert "another person" in out[0]["needs_review"]


def test_an_unresolvable_ref_keeps_its_import_time_person():
    rows = [
        {
            "participant_ref": "f" * 64,
            "person_id": PERSON_B,
            "channel": "sms",
            "kind": "message",
            "direction": "inbound",
            "outcome": None,
            "n": 1,
            "last_at": iso(T0),
        },
    ]
    out = comm.summarize(rows, comm.Resolver({}), {}, datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert out[0]["person_id"] == PERSON_B and out[0]["needs_review"] is None


def test_channel_coverage_intersects_whatsapp_messages_and_calls():
    imports = [
        {
            "source": "whatsapp_messages",
            "coverage": "partial",
            "event_min_at": "2018-01-01T00:00:00.000Z",
            "window_end": "2026-10-01T00:00:00.000Z",
        },
        {
            "source": "whatsapp_calls",
            "coverage": "partial",
            "event_min_at": "2026-03-16T00:00:00.000Z",
            "window_end": "2026-09-30T00:00:00.000Z",
        },
        {
            "source": "apple_calls",
            "coverage": "unavailable",
            "event_min_at": None,
            "window_end": None,
        },
        {
            "source": "apple_calls",
            "coverage": "complete",
            "event_min_at": "2025-07-16T00:00:00.000Z",
            "window_end": "2026-10-01T00:00:00.000Z",
        },
    ]
    cov = comm.channel_coverage(imports)
    assert cov["whatsapp"] == ("2026-03-16T00:00:00.000Z", "2026-09-30T00:00:00.000Z")
    assert cov["phone"] == ("2025-07-16T00:00:00.000Z", "2026-10-01T00:00:00.000Z")
    assert "imessage" not in cov


def test_hub_aggregate_sql_dedupes_on_event_id():
    sql = comm.hub_aggregate_sql("apple_calls")
    assert "stream('comm_apple_calls')" in sql and "DISTINCT event_id" in sql
    assert "conversation_kind = 'direct'" in sql


# --- the comm credential ------------------------------------------------------------------


class Recorder:
    def __init__(self, stdout=""):
        self.calls, self.stdout = [], stdout

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        return type("P", (), {"returncode": 0, "stdout": self.stdout, "stderr": ""})()


def test_comm_hub_calls_use_the_comm_token_when_set(monkeypatch):
    monkeypatch.setenv("SOMA_HUB_TOKEN", "files-token")
    monkeypatch.setenv("SOMA_COMM_HUB_TOKEN", "comm-token")
    rec = Recorder(stdout="[]")
    monkeypatch.setattr(comm.subprocess, "run", rec)
    comm.soma_append("comm_apple_calls", [{"event_id": "x"}])
    comm.hub_aggregates()
    assert rec.calls and all(kw["env"]["SOMA_HUB_TOKEN"] == "comm-token" for _, kw in rec.calls)
    assert "SOMA_COMM_HUB_TOKEN" not in rec.calls[0][1]["env"]


def test_comm_hub_calls_keep_the_ambient_token_without_one(monkeypatch):
    monkeypatch.setenv("SOMA_HUB_TOKEN", "files-token")
    monkeypatch.delenv("SOMA_COMM_HUB_TOKEN", raising=False)
    rec = Recorder()
    monkeypatch.setattr(comm.subprocess, "run", rec)
    comm.soma_append("comm_apple_calls", [{"event_id": "x"}])
    assert rec.calls[0][1]["env"]["SOMA_HUB_TOKEN"] == "files-token"


def test_a_reader_never_blocks_the_importer(tmp_path, monkeypatch):
    monkeypatch.setattr(comm, "BUSY_TIMEOUT_S", 0.2)
    state = comm.State(tmp_path / "state")
    with state.db:
        state.add("comm_apple_calls", {"event_id": "e1"})
    reader = sqlite3.connect(tmp_path / "state" / "comm.sqlite")
    reader.execute("BEGIN")
    assert reader.execute("SELECT count(*) FROM events").fetchone()[0] == 1  # holds a read
    state.mark_landed("comm_apple_calls", ["e1"])
    reader.rollback()
    assert state.waiting() == 0


class FlakyHub(FakeHub):
    def __init__(self, failures, message="RuntimeError: hub unreachable: [Errno 32] Broken pipe"):
        super().__init__()
        self.failures, self.message = failures, message

    def __call__(self, stream, records):
        if self.failures:
            self.failures -= 1
            raise comm.AppendError(self.message)
        super().__call__(stream, records)


def test_a_transient_hub_failure_is_retried(stores, tmp_path, monkeypatch):
    waits = []
    monkeypatch.setattr(comm, "_sleep", waits.append)
    hub = FlakyHub(failures=2)
    report = run("apple_messages", stores, tmp_path / "state", hub)
    assert report["landed"] == 8 and report["coverage"] == "complete"
    assert len(waits) == 2


def test_a_lasting_or_refused_failure_stops_the_flush(stores, tmp_path, monkeypatch):
    waits = []
    monkeypatch.setattr(comm, "_sleep", waits.append)
    report = run("apple_messages", stores, tmp_path / "s1", FlakyHub(failures=99))
    assert report["landed"] == 0 and len(waits) == len(comm.RETRY_WAITS_S)
    waits.clear()
    refused = FlakyHub(
        failures=99, message='RuntimeError: hub HTTP 403: {"error":"insufficient scope"}'
    )
    report = run("apple_messages", stores, tmp_path / "s2", refused)
    assert report["landed"] == 0 and waits == []  # a refusal is not retried
