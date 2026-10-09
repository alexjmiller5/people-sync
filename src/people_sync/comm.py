"""Communication history as metadata only: who, when, channel, direction, and a
call's outcome and duration, from Apple Messages, Apple call history and the
WhatsApp desktop stores.

Events land verbatim in one soma stream per source (`STREAM`) through `soma stream
import`; `refresh` folds them into one `communication_summaries` row per person
and channel, and every import run leaves one `communication_imports` row.

Nothing here can read a message body: every store connection carries a SQLite
authorizer that allows the metadata columns in `ALLOWED` and nothing else, so a
query touching a body, subject, preview, caption, name or attachment column fails
inside SQLite. Raw phone numbers and emails are read in memory only, to compute
`participant_ref` (sha256 of `tel:+E164` / `mailto:local@domain`, the findmy-cli
`source_handle_key` convention) and to resolve it to `people.id` through confirmed
`person_accounts` links.

Accepted events are written first to a private local mirror
(`<state>/comm/comm.sqlite`): the (stream, event_id) dedupe set, the outbox of
events the hub has not taken yet, the per-source checkpoint, and the source of
`refresh` when the hub is unreachable.
"""

import argparse
import gzip
import hashlib
import json
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from people_sync import captures, ledger, somadata, sources

SOURCES = ("apple_messages", "apple_calls", "whatsapp_messages", "whatsapp_calls")
STREAM = {source: f"comm_{source}" for source in SOURCES}
CALL_SOURCES = {"apple_calls", "whatsapp_calls"}
NAMESPACE = {
    "apple_messages": "chat.db message.guid",
    "apple_calls": "CallHistory.storedata ZCALLRECORD.ZUNIQUE_ID",
    "whatsapp_messages": "ChatStorage.sqlite ZWAMESSAGE.ZSTANZAID",
    "whatsapp_calls": "CallHistory.sqlite ZWACDCALLEVENT.ZCALLIDSTRING",
}
CHANNELS = ("imessage", "sms", "rcs", "phone", "facetime_audio", "facetime_video", "whatsapp")
COVERAGE_SOURCES = {
    "imessage": ("apple_messages",),
    "sms": ("apple_messages",),
    "rcs": ("apple_messages",),
    "phone": ("apple_calls",),
    "facetime_audio": ("apple_calls",),
    "facetime_video": ("apple_calls",),
    "whatsapp": ("whatsapp_messages", "whatsapp_calls"),
}
WHATSAPP_PARTIAL = (
    "the WhatsApp desktop store is a partial mirror of the phone: it holds only what the "
    "phone synced to it"
)
BATCH = 1000  # the hub's stream batch limit (soma worker: 1..1000 records per batch)
HOLD_BACK = timedelta(days=7)  # a transient exclusion younger than this is re-read next run
COCOA = 978307200  # 2001-01-01T00:00:00Z in unix seconds
BUSY_TIMEOUT_S = 60.0  # how long a write waits on another connection to comm.sqlite

_WA = Path.home() / "Library/Group Containers/group.net.whatsapp.WhatsApp.shared"
LIVE = {
    "apple_messages": Path.home() / "Library/Messages/chat.db",
    "apple_calls": Path.home() / "Library/Application Support/CallHistoryDB/CallHistory.storedata",
    "whatsapp_messages": _WA / "ChatStorage.sqlite",
    "whatsapp_calls": _WA / "CallHistory.sqlite",
    "whatsapp_lid": _WA / "LID.sqlite",
}
WHATSAPP_PREFERENCES = _WA / "Library/Preferences/group.net.whatsapp.WhatsApp.shared.plist"

# The only columns a store connection may read. Everything else - bodies, attributed
# bodies, subjects, payloads, chat and contact names, previews, captions, call names
# and locations, attachments - is refused by SQLite itself.
ALLOWED = {
    "apple_messages": {
        "message": {
            "ROWID",
            "guid",
            "handle_id",
            "service",
            "date",
            "is_from_me",
            "is_sent",
            "error",
            "item_type",
            "associated_message_type",
            "is_system_message",
            "is_service_message",
            "destination_caller_id",
        },
        "chat": {"ROWID", "guid", "style", "chat_identifier"},
        "handle": {"ROWID", "id", "country"},
        "chat_handle_join": {"chat_id", "handle_id"},
        "chat_message_join": {"chat_id", "message_id"},
    },
    "apple_calls": {
        "ZCALLRECORD": {
            "Z_PK",
            "ZUNIQUE_ID",
            "ZDATE",
            "ZDURATION",
            "ZANSWERED",
            "ZORIGINATED",
            "ZCALLTYPE",
            "ZADDRESS",
            "ZISO_COUNTRY_CODE",
            "ZDISCONNECTED_CAUSE",
        },
        "Z_2REMOTEPARTICIPANTHANDLES": {"Z_2REMOTEPARTICIPANTCALLS", "Z_4REMOTEPARTICIPANTHANDLES"},
    },
    "whatsapp": {
        "ZWAMESSAGE": {
            "Z_PK",
            "ZCHATSESSION",
            "ZISFROMME",
            "ZMESSAGESTATUS",
            "ZMESSAGEERRORSTATUS",
            "ZMESSAGETYPE",
            "ZMESSAGEDATE",
            "ZSTANZAID",
            "ZGROUPMEMBER",
        },
        "ZWACHATSESSION": {"Z_PK", "ZSESSIONTYPE", "ZCONTACTJID", "ZCONTACTIDENTIFIER"},
        "ZWAGROUPMEMBER": {"Z_PK", "ZMEMBERJID"},
        "ZWAZACCOUNT": {"ZIDENTIFIER", "ZPHONENUMBER"},
        "ZWAAGGREGATECALLEVENT": {"Z_PK", "ZINCOMING", "ZMISSED", "ZVIDEO"},
        "ZWACDCALLEVENT": {
            "Z_PK",
            "Z1CALLEVENTS",
            "ZOUTCOME",
            "ZDATE",
            "ZDURATION",
            "ZCALLIDSTRING",
            "ZGROUPJIDSTRING",
        },
        "ZWACDCALLEVENTPARTICIPANT": {"Z1PARTICIPANTS", "ZJIDSTRING"},
    },
}


COMM_TOKEN_ENV = "SOMA_COMM_HUB_TOKEN"


def _hub_env() -> dict:
    """The environment for `soma` hub calls made by comm: its own credential, when the
    operator supplies one, replaces SOMA_HUB_TOKEN for these calls only, so the rest of
    People Sync keeps its narrower token."""
    env = dict(os.environ)
    token = env.pop(COMM_TOKEN_ENV, None)
    if token:
        env["SOMA_HUB_TOKEN"] = token
    return env


class AppendError(RuntimeError):
    """The hub did not take a batch; the events stay in the local outbox."""


# --- identity ------------------------------------------------------------------------

_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_E164 = re.compile(r"\+[1-9]\d{7,14}")


def identity(handle: str, country: str | None = None) -> str:
    """A native handle as `tel:+E164`, `mailto:local@domain` (domain lowercased, local
    part kept) or `other:<handle>`. A number without `+` becomes E.164 only when the
    source itself names its country (Messages' handle.country, CallHistory's ISO code)."""
    value = handle.strip()
    if _EMAIL.fullmatch(value):
        local, domain = value.split("@")
        return f"mailto:{local}@{domain.lower()}"
    phone = re.sub(r"[ ().-]", "", value)
    if _E164.fullmatch(phone):
        return "tel:" + phone
    if (country or "").lower() == "us" and re.fullmatch(r"1?\d{10}", phone):
        return "tel:+1" + phone[-10:]
    return "other:" + value


def ref(identity_uri: str) -> str:
    return hashlib.sha256(identity_uri.encode()).hexdigest()


def _book_identity(number: str) -> str | None:
    """An address-book number, which carries no country of its own."""
    value = number.strip()
    if value.startswith("+"):
        found = identity(value)
        return found if found.startswith("tel:") else None
    digits = re.sub(r"\D", "", value)
    # ponytail: a contact number without a country code is read as US (+1); add a
    # default-region option if contacts from elsewhere go unresolved
    if len(digits) == 10:
        return "tel:+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "tel:+" + digits
    return None


class Resolver:
    """participant_ref -> people.id, only when every confirmed link names one person."""

    def __init__(self, people: dict[str, set[str]]):
        self.people = people

    def person(self, participant_ref: str | None) -> str | None:
        found = self.people.get(participant_ref) if participant_ref else None
        return next(iter(found)) if found and len(found) == 1 else None


def build_resolver(accounts, contacts, *, google_records=None, whatsapp_pairs=None) -> Resolver:
    """Index every identity reachable from a confirmed `person_accounts` link: an Apple
    contact by its id, a Google contact through the CardDAV external id of an Apple
    record (`google_records`: external id -> `google_contacts:<people/c...>` ledger id),
    a WhatsApp account by its LID and the number WhatsApp pairs with it."""
    linked: dict[tuple[str, str], set[str]] = {}
    for a in accounts:
        linked.setdefault((a["platform"], a["source_id"]), set()).add(a["person_id"])
    people: dict[str, set[str]] = {}

    def add(identity_uri, persons):
        if identity_uri and persons:
            people.setdefault(ref(identity_uri), set()).update(persons)

    for contact in contacts:
        persons = set(linked.get(("apple_contacts", contact["apple"]), ()))
        record = (google_records or {}).get(contact.get("external"))
        if record:
            persons |= linked.get(("google_contacts", record.split(":", 1)[1]), set())
        for number in contact.get("phones") or ():
            add(_book_identity(number), persons)
        for email in contact.get("emails") or ():
            found = identity(email)
            add(found if found.startswith("mailto:") else None, persons)
    for lid, phone in (whatsapp_pairs or {}).items():
        user = lid.split("@")[0]
        persons = linked.get(("whatsapp", "lid-" + user), set())
        add("whatsapp-lid:" + user, persons)
        add("tel:+" + phone, persons)
    return Resolver(people)


# --- store access --------------------------------------------------------------------


def _guard(allowed: dict[str, set[str]]):
    def authorize(action, table, column, _db, _trigger):
        if action == sqlite3.SQLITE_READ and column and column not in allowed.get(table, ()):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    return authorize


def _clone(src: Path, dst: Path) -> None:
    # An APFS clone is instant and costs no space; anything else gets a plain copy.
    if subprocess.run(["cp", "-c", str(src), str(dst)], capture_output=True).returncode != 0:
        shutil.copy2(src, dst)


def _snapshot(src: Path, directory: Path) -> str:
    directory.mkdir(mode=0o700)
    if src.suffix == ".gz":
        target = directory / src.stem
        with gzip.open(src) as f, open(target, "wb") as out:
            shutil.copyfileobj(f, out)
        return f"file:{target}?mode=ro&immutable=1"
    if not src.is_file():
        raise FileNotFoundError("store missing")
    target = directory / src.name
    _clone(src, target)
    wal = False
    for suffix in ("-wal", "-shm"):
        side = Path(f"{src}{suffix}")
        if side.exists():
            _clone(side, Path(f"{target}{suffix}"))
            wal = wal or suffix == "-wal"
    return f"file:{target}?mode=ro" + ("" if wal else "&immutable=1")


@contextmanager
def opened(paths: dict, allowed: dict[str, set[str]]):
    """A read-only connection over private snapshots of the stores (a live store is
    copied with its WAL, a .gz backup decompressed), so an app's own file is never
    opened. The first store is main; the rest attach under their keys."""
    with tempfile.TemporaryDirectory(prefix="people-sync-comm-") as tmp:
        uris = {name: _snapshot(Path(path), Path(tmp) / name) for name, path in paths.items()}
        names = list(uris)
        conn = sqlite3.connect(uris[names[0]], uri=True)
        try:
            for name in names[1:]:
                conn.execute(f"ATTACH DATABASE ? AS {name}", (uris[name],))
            conn.set_authorizer(_guard(allowed))
            yield conn
        finally:
            conn.close()


def _rows(conn, sql: str, params=()) -> list:
    """Rows of an optional table: a store that lacks it yields nothing."""
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return []
        raise


def _iso(unix: float) -> str:
    return (
        datetime.fromtimestamp(unix, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


# --- adapters: each yields (native pk, unix time, record | None, reason | None, transient) ---

_APPLE_MESSAGES = """
WITH chat_handle AS (
  SELECT j.chat_id, count(*) AS n, min(h.id) AS handle, min(h.country) AS country
  FROM chat_handle_join j JOIN handle h ON h.ROWID = j.handle_id GROUP BY j.chat_id)
SELECT m.ROWID, m.guid, m.date, m.service, m.is_from_me, m.is_sent, m.error, m.item_type,
       m.associated_message_type, m.is_system_message, m.is_service_message,
       c.guid, c.style, c.chat_identifier, ch.n, ch.handle, ch.country, s.id, s.country
FROM message m
LEFT JOIN chat_message_join cm ON cm.message_id = m.ROWID
LEFT JOIN chat c ON c.ROWID = cm.chat_id
LEFT JOIN chat_handle ch ON ch.chat_id = c.ROWID
LEFT JOIN handle s ON s.ROWID = m.handle_id
WHERE m.ROWID > ? ORDER BY m.ROWID
"""
_MESSAGE_CHANNELS = {"iMessage": "imessage", "iMessageLite": "imessage", "SMS": "sms", "RCS": "rcs"}
_CHAT_STYLES = {45: "direct", 43: "group"}


def _own_apple(conn) -> tuple[set[str], set[str]]:
    raw = {
        v
        for (v,) in conn.execute(
            "SELECT DISTINCT destination_caller_id FROM message "
            "WHERE is_from_me = 1 AND destination_caller_id IS NOT NULL"
        )
        if v
    }
    stripped = {re.sub(r"^(e:|p:|tel:|mailto:)", "", v, flags=re.I) for v in raw}
    return raw | stripped, {ref(identity(v)) for v in stripped}


def apple_messages(conn, after, resolver, instance, **_):
    own_raw, own_refs = _own_apple(conn)
    for row in conn.execute(_APPLE_MESSAGES, (after,)):
        (
            pk,
            guid,
            date,
            service,
            from_me,
            sent,
            error,
            item_type,
            assoc,
            system,
            service_row,
            chat_guid,
            style,
            chat_ident,
            n_handles,
            chat_handle,
            chat_country,
            sender,
            sender_country,
        ) = row
        if date is None or not guid:
            yield pk, 0, None, "no-id", False
            continue
        unix = COCOA + (date / 1e9 if date > 1e14 else date)
        reason, transient = None, False
        kind = _CHAT_STYLES.get(style)
        channel = _MESSAGE_CHANNELS.get(service)
        participant = None
        if chat_guid is None:
            reason = "no-chat"
        elif assoc:
            reason = "reaction"
        elif item_type or system or service_row:
            reason = "service"
        elif channel is None:
            reason = "other-service"
        elif kind is None or (kind == "direct" and n_handles != 1):
            reason = "other-chat"
        elif kind == "direct":
            participant = ref(identity(chat_handle, chat_country))
            if participant in own_refs or chat_ident in own_raw:
                reason = "self"
        elif not from_me and sender:
            participant = ref(identity(sender, sender_country))
        if reason is None and error:
            reason = "failed"
        elif reason is None and from_me and not sent:
            reason, transient = "pending", True
        if reason:
            yield pk, unix, None, reason, transient
            continue
        yield (
            pk,
            unix,
            {
                "v": 1,
                "event_id": guid,
                "at": _iso(unix),
                "channel": channel,
                "direction": "outbound" if from_me else "inbound",
                "conversation_kind": kind,
                "participant_ref": participant,
                "person_id": resolver.person(participant),
                "chat_ref": ref(chat_guid),
                "instance": instance,
            },
            None,
            False,
        )


_APPLE_CALLS = """
SELECT r.Z_PK, r.ZUNIQUE_ID, r.ZDATE, r.ZDURATION, r.ZANSWERED, r.ZORIGINATED, r.ZCALLTYPE,
       r.ZADDRESS, r.ZISO_COUNTRY_CODE, r.ZDISCONNECTED_CAUSE,
       (SELECT count(*) FROM Z_2REMOTEPARTICIPANTHANDLES j WHERE j.Z_2REMOTEPARTICIPANTCALLS = r.Z_PK)
FROM ZCALLRECORD r WHERE r.Z_PK > ? ORDER BY r.Z_PK
"""
_CALL_CHANNELS = {1: "phone", 8: "facetime_video", 16: "facetime_audio"}


def apple_calls(conn, after, resolver, instance, **_):
    for row in conn.execute(_APPLE_CALLS, (after,)):
        (
            pk,
            uid,
            date,
            duration,
            answered,
            originated,
            call_type,
            address,
            country,
            cause,
            remotes,
        ) = row
        if date is None or not uid:
            yield pk, 0, None, "no-id", False
            continue
        unix = COCOA + date
        channel = _CALL_CHANNELS.get(call_type)
        if channel is None:
            yield pk, unix, None, "unknown-type", False
            continue
        if not address:
            yield pk, unix, None, "no-participant", False
            continue
        group = remotes > 1
        participant = None if group else ref(identity(address, country))
        # Apple records an answer only on incoming calls: an outgoing call's duration is
        # not an outcome, so it stays unknown.
        outcome = "answered" if answered == 1 else ("unknown" if originated else "missed")
        yield (
            pk,
            unix,
            {
                "v": 1,
                "event_id": uid,
                "at": _iso(unix),
                "channel": channel,
                "direction": "outbound" if originated else "inbound",
                "conversation_kind": "group" if group else "direct",
                "participant_ref": participant,
                "person_id": resolver.person(participant),
                "outcome": outcome,
                "duration_seconds": round(duration or 0.0, 3),
                "source_outcome": {"answered": answered, "disconnected_cause": cause},
                "instance": instance,
            },
            None,
            False,
        )


def whatsapp_phones(conn) -> dict[str, str]:
    """`<id>@lid` -> phone digits, from the account table and the direct chats that
    carry both forms. Used in memory only."""
    phones = {}
    for jid, number in _rows(
        conn,
        "SELECT ZIDENTIFIER, ZPHONENUMBER FROM ZWAZACCOUNT "
        "WHERE ZIDENTIFIER LIKE '%@lid' AND ZPHONENUMBER IS NOT NULL",
    ):
        digits = re.sub(r"\D", "", number)
        if digits:
            phones[jid] = digits
    for a, b in _rows(
        conn, "SELECT ZCONTACTJID, ZCONTACTIDENTIFIER FROM ZWACHATSESSION WHERE ZSESSIONTYPE = 0"
    ):
        for lid, phone in ((a, b), (b, a)):
            if lid and phone and lid.endswith("@lid") and phone.endswith("@s.whatsapp.net"):
                phones[lid] = phone.split("@")[0]
    return phones


def _wa_identity(jid: str | None, phones: dict[str, str]) -> str | None:
    if not jid:
        return None
    user, _, server = jid.partition("@")
    if server == "s.whatsapp.net":
        return "tel:+" + user
    if server == "lid":
        return "tel:+" + phones[jid] if jid in phones else "whatsapp-lid:" + user
    return None


def _wa_peer(contact_jid, contact_identifier, phones):
    """A direct chat's other party, preferring the phone form."""
    jids = [j for j in (contact_jid, contact_identifier) if j]
    jids.sort(key=lambda j: not j.endswith("@s.whatsapp.net"))
    return _wa_identity(jids[0], phones) if jids else None


_WA_MESSAGES = """
SELECT m.Z_PK, m.ZSTANZAID, m.ZMESSAGEDATE, m.ZISFROMME, m.ZMESSAGESTATUS, m.ZMESSAGEERRORSTATUS,
       m.ZMESSAGETYPE, s.ZSESSIONTYPE, s.ZCONTACTJID, s.ZCONTACTIDENTIFIER, g.ZMEMBERJID
FROM ZWAMESSAGE m
LEFT JOIN ZWACHATSESSION s ON s.Z_PK = m.ZCHATSESSION
LEFT JOIN ZWAGROUPMEMBER g ON g.Z_PK = m.ZGROUPMEMBER
WHERE m.Z_PK > ? ORDER BY m.Z_PK
"""
# Proven on the real store: 0 text, 1 image, 2 video, 3 audio, 4 contact card, 5 location,
# 7 link, 8 document, 11 GIF, 14 revoked, 15 sticker are messages a person sent; 6 and 10
# are system rows; 59 is a call's row in the chat (calls come from the call store).
_WA_MESSAGE_TYPES = {0, 1, 2, 3, 4, 5, 7, 8, 11, 14, 15}
# An outgoing message settles at 8 or 6 once the server has acknowledged it; 1 is the
# in-flight state (observed moving to 6 within minutes).
_WA_SENT = {6, 8}


def whatsapp_messages(conn, after, resolver, instance, own_whatsapp=None, **_):
    phones = whatsapp_phones(conn)
    own = _wa_identity(own_whatsapp, phones)
    for row in conn.execute(_WA_MESSAGES, (after,)):
        (
            pk,
            stanza,
            date,
            from_me,
            status,
            error,
            msg_type,
            session_type,
            contact_jid,
            contact_ident,
            member,
        ) = row
        if date is None or not stanza:
            yield pk, 0, None, "no-id", False
            continue
        unix = COCOA + date
        reason, transient, participant = None, False, None
        if session_type is None:
            reason = "no-chat"
        elif session_type not in (0, 1):
            reason = "status-broadcast"
        elif msg_type in (6, 10):
            reason = "service"
        elif msg_type == 59:
            reason = "call-row"
        elif msg_type not in _WA_MESSAGE_TYPES:
            reason = "other-type"
        elif session_type == 0:
            peer = _wa_peer(contact_jid, contact_ident, phones)
            if peer is None:
                reason = "no-participant"
            elif peer == own:
                reason = "self"
            else:
                participant = ref(peer)
        elif not from_me:
            peer = _wa_identity(member, phones)
            participant = ref(peer) if peer else None
        if reason is None and error:
            reason = "failed"
        elif reason is None and from_me and status not in _WA_SENT:
            reason, transient = "pending", True
        if reason:
            yield pk, unix, None, reason, transient
            continue
        yield (
            pk,
            unix,
            {
                "v": 1,
                "event_id": stanza,
                "at": _iso(unix),
                "channel": "whatsapp",
                "direction": "outbound" if from_me else "inbound",
                "conversation_kind": "direct" if session_type == 0 else "group",
                "participant_ref": participant,
                "person_id": resolver.person(participant),
                "chat_ref": ref(contact_jid or contact_ident or ""),
                "source_status": status,
                "instance": instance,
            },
            None,
            False,
        )


_WA_CALLS = """
SELECT e.Z_PK, e.ZCALLIDSTRING, e.ZDATE, e.ZDURATION, e.ZOUTCOME, e.ZGROUPJIDSTRING,
       a.ZINCOMING, a.ZMISSED, a.ZVIDEO,
       (SELECT count(*) FROM ZWACDCALLEVENTPARTICIPANT p WHERE p.Z1PARTICIPANTS = e.Z_PK),
       (SELECT min(p.ZJIDSTRING) FROM ZWACDCALLEVENTPARTICIPANT p WHERE p.Z1PARTICIPANTS = e.Z_PK)
FROM ZWACDCALLEVENT e LEFT JOIN ZWAAGGREGATECALLEVENT a ON a.Z_PK = e.Z1CALLEVENTS
WHERE e.Z_PK > ? ORDER BY e.Z_PK
"""


def whatsapp_calls(conn, after, resolver, instance, **_):
    phones = whatsapp_phones(conn)
    # A call with no participant rows still has its own row in the chat it belongs to.
    rows = {
        stanza: (session_type, jid, ident)
        for stanza, session_type, jid, ident in _rows(
            conn,
            "SELECT m.ZSTANZAID, s.ZSESSIONTYPE, s.ZCONTACTJID, s.ZCONTACTIDENTIFIER "
            "FROM ZWAMESSAGE m JOIN ZWACHATSESSION s ON s.Z_PK = m.ZCHATSESSION "
            "WHERE m.ZMESSAGETYPE = 59",
        )
    }
    for row in conn.execute(_WA_CALLS, (after,)):
        (
            pk,
            call_id,
            date,
            duration,
            outcome_code,
            group_jid,
            incoming,
            missed,
            video,
            n_participants,
            participant_jid,
        ) = row
        if date is None or not call_id:
            yield pk, 0, None, "no-id", False
            continue
        unix = COCOA + date
        if incoming is None:
            yield pk, unix, None, "no-direction", False
            continue
        chat = rows.get(call_id)
        group = bool(group_jid) or n_participants > 1 or (chat is not None and chat[0] == 1)
        participant = None
        if not group:
            if n_participants == 1:
                peer = _wa_identity(participant_jid, phones)
            else:
                peer = _wa_peer(chat[1], chat[2], phones) if chat else None
            if peer is None:
                yield pk, unix, None, "no-participant", True
                continue
            participant = ref(peer)
        # Proven on the real store: outcome 0 is connected (every one has a positive
        # duration); 1 and 4 never connected (an incoming one is flagged missed); 5 is
        # left unknown.
        if outcome_code == 0:
            outcome = "answered"
        elif outcome_code in (1, 4):
            outcome = "missed" if incoming else "unanswered"
        else:
            outcome = "unknown"
        yield (
            pk,
            unix,
            {
                "v": 1,
                "event_id": call_id,
                "at": _iso(unix),
                "channel": "whatsapp",
                "direction": "inbound" if incoming else "outbound",
                "conversation_kind": "group" if group else "direct",
                "participant_ref": participant,
                "person_id": resolver.person(participant),
                "media": "video" if video else "audio",
                "outcome": outcome,
                "duration_seconds": round(duration or 0.0, 3),
                "source_outcome": {"outcome": outcome_code, "missed": missed},
                "instance": instance,
            },
            None,
            False,
        )


ADAPTERS = {
    "apple_messages": apple_messages,
    "apple_calls": apple_calls,
    "whatsapp_messages": whatsapp_messages,
    "whatsapp_calls": whatsapp_calls,
}


def _store_paths(source: str, stores: dict) -> tuple[dict, dict]:
    if source == "apple_messages":
        return {"main": stores["apple_messages"]}, ALLOWED["apple_messages"]
    if source == "apple_calls":
        return {"main": stores["apple_calls"]}, ALLOWED["apple_calls"]
    paths = {"main": stores[source]}
    # Calls find a participant-less call's chat through ChatStorage; both read the LID map.
    extra = (("chat", "whatsapp_messages"),) if source == "whatsapp_calls" else ()
    for name, key in (*extra, ("lid", "whatsapp_lid")):
        if stores.get(key) and Path(stores[key]).exists():
            paths[name] = stores[key]
    return paths, ALLOWED["whatsapp"]


# --- local mirror: dedupe set, outbox, checkpoints -------------------------------------


class State:
    """Private local state (`comm.sqlite`, 0600): every accepted record keyed by
    (stream, event_id), whether the hub has taken it, and each source's checkpoint."""

    def __init__(self, root):
        root = Path(root)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = root / "comm.sqlite"
        self.db = sqlite3.connect(path, timeout=BUSY_TIMEOUT_S)
        path.chmod(0o600)
        # WAL: a progress read (`comm coverage`, a shell) never blocks the importer's commits.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (stream TEXT NOT NULL, event_id TEXT NOT NULL,
                record TEXT NOT NULL, landed INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (stream, event_id)) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS outbox ON events (stream) WHERE landed = 0;
            CREATE TABLE IF NOT EXISTS checkpoints (source TEXT NOT NULL, instance TEXT NOT NULL,
                native_pk INTEGER NOT NULL, read_at TEXT, PRIMARY KEY (source, instance));
            """
        )

    def checkpoint(self, source: str, instance: str) -> tuple[int, str | None]:
        row = self.db.execute(
            "SELECT native_pk, read_at FROM checkpoints WHERE source = ? AND instance = ?",
            (source, instance),
        ).fetchone()
        return (row[0], row[1]) if row else (0, None)

    def set_checkpoint(self, source, instance, native_pk, read_at) -> None:
        self.db.execute(
            "INSERT INTO checkpoints VALUES (?,?,?,?) ON CONFLICT (source, instance) "
            "DO UPDATE SET native_pk = excluded.native_pk, read_at = excluded.read_at",
            (source, instance, native_pk, read_at),
        )

    def add(self, stream: str, record: dict) -> bool:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO events (stream, event_id, record) VALUES (?,?,?)",
            (stream, record["event_id"], json.dumps(record, sort_keys=True, separators=(",", ":"))),
        )
        return cur.rowcount == 1

    def unlanded(self, stream: str, limit: int) -> list[tuple[str, str]]:
        return self.db.execute(
            "SELECT event_id, record FROM events WHERE stream = ? AND landed = 0 "
            "ORDER BY event_id LIMIT ?",
            (stream, limit),
        ).fetchall()

    def mark_landed(self, stream: str, event_ids: list[str]) -> None:
        with self.db:
            self.db.executemany(
                "UPDATE events SET landed = 1 WHERE stream = ? AND event_id = ?",
                [(stream, e) for e in event_ids],
            )

    def waiting(self, stream: str | None = None) -> int:
        if stream:
            sql, params = "SELECT count(*) FROM events WHERE landed = 0 AND stream = ?", (stream,)
        else:
            sql, params = "SELECT count(*) FROM events WHERE landed = 0", ()
        return self.db.execute(sql, params).fetchone()[0]


def flush(state: State, stream: str, append) -> tuple[int, str | None]:
    """Hand the stream's outbox to the hub one batch at a time; stop at the first refusal."""
    landed = 0
    while batch := state.unlanded(stream, BATCH):
        try:
            append(stream, [json.loads(record) for _, record in batch])
        except AppendError as e:
            return landed, str(e)
        state.mark_landed(stream, [event_id for event_id, _ in batch])
        landed += len(batch)
    return landed, None


def soma_append(stream: str, records: list[dict]) -> None:
    """One `soma stream import` call per hub batch, so a refusal leaves nothing half-sent."""
    body = "\n".join(json.dumps(r, sort_keys=True, separators=(",", ":")) for r in records)
    proc = subprocess.run(
        ["soma", "stream", "import", stream, "--chunk", str(BATCH)],
        input=body,
        capture_output=True,
        text=True,
        env=_hub_env(),
    )
    if proc.returncode != 0:
        lines = [ln.strip() for ln in proc.stderr.splitlines() if ln.strip()]
        raise AppendError((lines[-1] if lines else f"exit {proc.returncode}")[:200])


# --- import --------------------------------------------------------------------------


def run_import(
    source,
    *,
    stores,
    state: State,
    resolver: Resolver,
    append,
    own_whatsapp=None,
    instance="live",
    full=False,
    now=None,
) -> dict:
    """Read one source past its checkpoint, keep every accepted record in the local
    mirror, then hand the outbox to the hub. The counts always sum:
    observed_rows = accepted + excluded + duplicates."""
    now = now or datetime.now(timezone.utc)
    stream = STREAM[source]
    after, previous_read = (0, None) if full else state.checkpoint(source, instance)
    report = {
        "source": source,
        "source_namespace": NAMESPACE[source],
        "source_instance": instance,
        "window_start": previous_read,
        "window_end": _iso(now.timestamp()),
        "observed_rows": 0,
        "accepted": 0,
        "excluded": 0,
        "duplicates": 0,
        "excluded_by_reason": {},
        "event_min_at": None,
        "event_max_at": None,
        "coverage": "complete",
        "reason": None,
        "stream": stream,
        "landed": 0,
        "resolution": {"resolved": 0, "unresolved": 0},
    }
    reasons: list[str] = []
    if source.startswith("whatsapp") and not own_whatsapp:
        report.update(coverage="unavailable", reason="own WhatsApp id unknown")
        return report
    excluded: Counter = Counter()
    refs: dict[str, bool] = {}
    try:
        paths, allowed = _store_paths(source, stores)
        with opened(paths, allowed) as conn, state.db:
            last, hold = after, None
            for pk, unix, record, reason, transient in ADAPTERS[source](
                conn, after, resolver, instance, own_whatsapp=own_whatsapp
            ):
                report["observed_rows"] += 1
                last = max(last, pk)
                if reason:
                    excluded[reason] += 1
                    if transient and now.timestamp() - unix < HOLD_BACK.total_seconds():
                        hold = pk if hold is None else min(hold, pk)
                    continue
                if not state.add(stream, record):
                    report["duplicates"] += 1
                    continue
                report["accepted"] += 1
                at = record["at"]
                report["event_min_at"] = min(filter(None, (report["event_min_at"], at)))
                report["event_max_at"] = max(filter(None, (report["event_max_at"], at)))
                if record["conversation_kind"] == "direct" and record["participant_ref"]:
                    refs[record["participant_ref"]] = record["person_id"] is not None
            state.set_checkpoint(
                source,
                instance,
                last if hold is None else min(last, hold - 1),
                report["window_end"],
            )
    except (OSError, sqlite3.Error) as e:
        if "not authorized" in str(e):
            raise  # a query reached past the metadata allowlist: a bug, never a coverage gap
        report.update(coverage="unavailable", reason="store not readable")
    report["excluded_by_reason"] = dict(excluded)
    report["excluded"] = sum(excluded.values())
    report["resolution"] = {
        "resolved": sum(refs.values()),
        "unresolved": len(refs) - sum(refs.values()),
    }
    landed, error = flush(state, stream, append)
    report["landed"] = landed
    waiting = state.waiting(stream)
    if report["coverage"] == "unavailable":
        reasons.append(report["reason"])
    elif waiting:
        report["coverage"] = "partial"
        reasons.append(
            f"{waiting} accepted events wait in the local outbox"
            + (f" (hub refused: {error})" if error else "")
        )
    if source.startswith("whatsapp") and report["coverage"] == "complete":
        report["coverage"] = "partial"
    if source.startswith("whatsapp") and report["coverage"] != "unavailable":
        reasons.append(WHATSAPP_PARTIAL)
    report["reason"] = "; ".join(reasons) or None
    return report


IMPORT_COLUMNS = (
    "source",
    "source_namespace",
    "source_instance",
    "window_start",
    "window_end",
    "observed_rows",
    "accepted",
    "excluded",
    "duplicates",
    "excluded_by_reason",
    "event_min_at",
    "event_max_at",
    "coverage",
    "reason",
)


def import_row(report: dict) -> dict:
    row = {c: report[c] for c in IMPORT_COLUMNS}
    return {"id": uuid.uuid4().hex, **row, "completed_at": somadata.now_iso()}


# --- refresh -------------------------------------------------------------------------

_LOCAL_AGGREGATES = """
SELECT json_extract(record, '$.participant_ref') AS participant_ref,
       json_extract(record, '$.person_id') AS person_id,
       json_extract(record, '$.channel') AS channel,
       CASE WHEN stream IN ('comm_apple_calls', 'comm_whatsapp_calls') THEN 'call' ELSE 'message' END
           AS kind,
       json_extract(record, '$.direction') AS direction,
       json_extract(record, '$.outcome') AS outcome,
       count(*) AS n,
       max(json_extract(record, '$.at')) AS last_at,
       json_extract(record, '$.duration_seconds') AS last_seconds
FROM events
WHERE json_extract(record, '$.conversation_kind') = 'direct'
  AND json_extract(record, '$.participant_ref') IS NOT NULL
GROUP BY 1, 2, 3, 4, 5, 6
"""


def local_aggregates(state: State) -> list[dict]:
    """Per (participant, import-time person, channel, kind, direction, outcome): event
    count, latest time and that event's duration (SQLite takes a bare column from the
    max() row). Direct events only."""
    cur = state.db.execute(_LOCAL_AGGREGATES)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def hub_aggregate_sql(source: str) -> str:
    """The same aggregate over the stream's landed records (DuckDB through `soma archive
    query --raw`), deduplicated on event_id."""
    if source in CALL_SOURCES:
        kind, outcome, seconds = "'call'", "outcome", 'arg_max(duration_seconds, "at")'
    else:
        kind, outcome, seconds = "'message'", "NULL", "NULL"
    return (
        f"SELECT participant_ref, person_id, channel, {kind} AS kind, direction, "
        f'{outcome} AS outcome, count(DISTINCT event_id) AS n, max("at") AS last_at, '
        f"{seconds} AS last_seconds FROM stream('{STREAM[source]}') "
        "WHERE conversation_kind = 'direct' AND participant_ref IS NOT NULL GROUP BY ALL"
    )


class HubError(RuntimeError):
    pass


def _json_values(text: str) -> list:
    decoder, values, i = json.JSONDecoder(), [], 0
    text = text.strip()
    while i < len(text):
        value, i = decoder.raw_decode(text, i)
        values.append(value)
        while i < len(text) and text[i].isspace():
            i += 1
    return values


def hub_aggregates() -> list[dict]:
    rows = []
    for source in SOURCES:
        proc = subprocess.run(
            ["soma", "archive", "query", "--raw", hub_aggregate_sql(source)],
            capture_output=True,
            text=True,
            env=_hub_env(),
        )
        if proc.returncode != 0:
            lines = [ln.strip() for ln in proc.stderr.splitlines() if ln.strip()]
            last = lines[-1] if lines else f"exit {proc.returncode}"
            if "is empty" in last:
                continue  # a stream nothing has landed in yet
            raise HubError(last[:200])
        for value in _json_values(proc.stdout):
            if isinstance(value, list):
                rows.extend(r for r in value if isinstance(r, dict) and "participant_ref" in r)
    return rows


def _later(a, b):
    return max(v for v in (a, b) if v) if (a or b) else None


def _earlier(a, b):
    return min(v for v in (a, b) if v) if (a or b) else None


def channel_coverage(imports: list[dict]) -> dict[str, tuple[str, str]]:
    """{channel: (coverage_start, coverage_end)}: a source spans its earliest landed
    event to its latest completed read; a channel fed by two sources (WhatsApp messages
    and calls) covers only where both do."""
    spans: dict[str, tuple] = {}
    for row in imports:
        if row["coverage"] == "unavailable":
            continue
        lo, hi = spans.get(row["source"], (None, None))
        spans[row["source"]] = (
            _earlier(lo, row.get("event_min_at")),
            _later(hi, row.get("window_end")),
        )
    out = {}
    for channel, feeding in COVERAGE_SOURCES.items():
        if all(spans.get(s, (None, None))[0] and spans[s][1] for s in feeding):
            out[channel] = (max(spans[s][0] for s in feeding), min(spans[s][1] for s in feeding))
    return out


SUMMARY_COLUMNS = (
    "person_id",
    "channel",
    "last_outbound_sent_at",
    "last_inbound_received_at",
    "last_answered_call_at",
    "last_answered_call_seconds",
    "outbound_count",
    "inbound_count",
    "calls_answered",
    "calls_missed",
    "calls_outbound",
    "calls_inbound",
    "last_call_at",
    "coverage_start",
    "coverage_end",
    "refreshed_at",
    "needs_review",
)
_LAST = (
    "last_outbound_sent_at",
    "last_inbound_received_at",
    "last_answered_call_at",
    "last_call_at",
)


def summarize(rows, resolver: Resolver, coverage: dict, now: datetime) -> list[dict]:
    """One summary per (person, channel) from direct events only. A participant resolves
    through today's confirmed links, else keeps the person it resolved to at import."""
    refreshed = _iso(now.timestamp())
    out: dict[tuple[str, str], dict] = {}
    moved: Counter = Counter()
    for r in rows:
        person = resolver.person(r["participant_ref"]) or r.get("person_id")
        if not person:
            continue
        key = (person, r["channel"])
        if key not in out:
            start, end = coverage.get(r["channel"], (None, None))
            out[key] = {
                "id": f"{person}:{r['channel']}",
                **dict.fromkeys(SUMMARY_COLUMNS),
                "person_id": person,
                "channel": r["channel"],
                **dict.fromkeys(
                    (
                        "outbound_count",
                        "inbound_count",
                        "calls_answered",
                        "calls_missed",
                        "calls_outbound",
                        "calls_inbound",
                    ),
                    0,
                ),
                "coverage_start": start,
                "coverage_end": end,
                "refreshed_at": refreshed,
            }
        s, n, at = out[key], int(r["n"]), r["last_at"]
        outbound = r["direction"] == "outbound"
        if r["kind"] == "message":
            if outbound:
                s["outbound_count"] += n
                s["last_outbound_sent_at"] = _later(s["last_outbound_sent_at"], at)
            else:
                s["inbound_count"] += n
                s["last_inbound_received_at"] = _later(s["last_inbound_received_at"], at)
        else:
            s["calls_outbound" if outbound else "calls_inbound"] += n
            s["last_call_at"] = _later(s["last_call_at"], at)
            if r["outcome"] == "answered":
                s["calls_answered"] += n
                if s["last_answered_call_at"] is None or at > s["last_answered_call_at"]:
                    s["last_answered_call_at"] = at
                    s["last_answered_call_seconds"] = r.get("last_seconds")
            elif r["outcome"] == "missed":
                s["calls_missed"] += n
        if r.get("person_id") and r["person_id"] != person:
            moved[key] += n
    for key, s in out.items():
        reasons = []
        if moved[key]:
            reasons.append(
                f"{moved[key]} events resolved to another person at import; "
                "check this participant's confirmed links"
            )
        if any(s[c] and s[c] > refreshed for c in _LAST):
            reasons.append("an event is dated after this refresh")
        s["needs_review"] = "; ".join(reasons) or None
    return sorted(out.values(), key=lambda s: s["id"])


def _literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return repr(value)
    return somadata.sq(str(value))


def write_summaries(summaries: list[dict]) -> dict:
    """Upsert the refreshed rows; a (person, channel) with no direct events left is
    soft-deleted."""
    existing = {
        r["id"]: r["deleted_at"]
        for r in somadata.sql("SELECT id, deleted_at FROM communication_summaries")
    }
    new = [s for s in summaries if s["id"] not in existing]
    somadata.insert("communication_summaries", new)
    updates = {
        s["id"]: {**{c: _literal(s[c]) for c in SUMMARY_COLUMNS}, "deleted_at": "NULL"}
        for s in summaries
        if s["id"] in existing
    }
    if updates:
        ledger.batch_update("communication_summaries", "id", updates)
    keep = {s["id"] for s in summaries}
    gone = [i for i, deleted in existing.items() if deleted is None and i not in keep]
    for start in range(0, len(gone), 200):
        chunk = ",".join(somadata.sq(i) for i in gone[start : start + 200])
        somadata.sql(
            f"UPDATE communication_summaries SET deleted_at = updated_at WHERE id IN ({chunk})"
        )
    return {"inserted": len(new), "updated": len(updates), "deleted": len(gone)}


# --- operator wiring (address books, WhatsApp self id, soma) ----------------------------

_BOOK_SQL = (
    "SELECT r.ZUNIQUEID AS apple, r.ZEXTERNALUUID AS external, "
    "(SELECT json_group_array(ZFULLNUMBER) FROM ZABCDPHONENUMBER WHERE ZOWNER = r.Z_PK) AS phones, "
    "(SELECT json_group_array(ZADDRESS) FROM ZABCDEMAILADDRESS WHERE ZOWNER = r.Z_PK) AS emails "
    "FROM ZABCDRECORD r WHERE r.ZUNIQUEID IS NOT NULL"
)
ADDRESSBOOK_HOST_ENV = "PEOPLE_SYNC_ADDRESSBOOK_HOST"


def _book_rows(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        out.append(
            {
                "apple": row["apple"],
                "external": row.get("external"),
                "phones": [p for p in json.loads(row.get("phones") or "[]") if p],
                "emails": [e for e in json.loads(row.get("emails") or "[]") if e],
            }
        )
    return out


def address_book() -> tuple[list[dict], list[str]]:
    """Contact ids with their numbers and emails, read only: this Mac's address book and,
    with PEOPLE_SYNC_ADDRESSBOOK_HOST set, another Mac's over ssh. Held in memory only."""
    contacts, read = [], []
    for path in sources._db_paths():
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            contacts += _book_rows([dict(r) for r in conn.execute(_BOOK_SQL)])
            conn.close()
            read.append("local")
        except sqlite3.Error:
            continue
    host = os.environ.get(ADDRESSBOOK_HOST_ENV)
    if host:
        script = (
            'for db in "$HOME"/Library/Application\\ Support/AddressBook/Sources/*/'
            "AddressBook-v22.abcddb; do\n"
            f"  sqlite3 -json \"file:$db?mode=ro\" <<'SQL'\n{_BOOK_SQL};\nSQL\ndone\n"
        )
        try:
            proc = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, "sh", "-s"],
                input=script,
                capture_output=True,
                text=True,
                timeout=180,
            )
            if proc.returncode == 0:
                for value in _json_values(proc.stdout):
                    contacts += _book_rows(value)
                read.append("remote")
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass
    return contacts, read


def live_whatsapp_pairs(stores: dict) -> dict[str, str]:
    paths = {
        name: stores[key]
        for name, key in (("main", "whatsapp_messages"), ("lid", "whatsapp_lid"))
        if stores.get(key) and Path(stores[key]).exists()
    }
    if "main" not in paths:
        return {}
    try:
        with opened(paths, ALLOWED["whatsapp"]) as conn:
            return whatsapp_phones(conn)
    except (OSError, sqlite3.OperationalError):
        return {}


def load_resolver(stores: dict) -> tuple[Resolver, dict]:
    accounts = somadata.sql(
        "SELECT platform, source_id, person_id FROM person_accounts WHERE deleted_at IS NULL "
        "AND source_id IS NOT NULL "
        "AND platform IN ('apple_contacts', 'google_contacts', 'whatsapp')"
    )
    contacts, books = address_book()
    pairs = live_whatsapp_pairs(stores)
    resolver = build_resolver(
        accounts, contacts, google_records=sources.google_contact_ids(), whatsapp_pairs=pairs
    )
    return resolver, {
        "address_books": books,
        "contacts": len(contacts),
        "whatsapp_pairs": len(pairs),
    }


def own_whatsapp_id() -> str | None:
    try:
        with open(WHATSAPP_PREFERENCES, "rb") as f:
            value = plistlib.load(f).get("OwnJabberID")
    except (OSError, plistlib.InvalidFileException):
        return None
    return value if isinstance(value, str) and value.endswith("@s.whatsapp.net") else None


def _state(args) -> State:
    return State(captures.private_path(captures.state_directory(args.state_dir) / "comm"))


def cmd_import(args) -> None:
    chosen = SOURCES if args.source == "all" else (args.source,)
    if args.store and len(chosen) != 1:
        sys.exit("--store reads one source's store; name the source")
    stores = dict(LIVE)
    if args.store:
        stores[chosen[0]] = args.store
    if args.lid_store:
        stores["whatsapp_lid"] = args.lid_store
    instance = args.instance or (
        f"backup:{args.store.parent.name}/{args.store.name}" if args.store else "live"
    )
    state = _state(args)
    resolver, inputs = load_resolver(dict(LIVE))
    own = args.whatsapp_self_id or own_whatsapp_id()
    reports = []
    for source in chosen:
        report = run_import(
            source,
            stores=stores,
            state=state,
            resolver=resolver,
            append=soma_append,
            own_whatsapp=own,
            instance=instance,
            full=args.full,
        )
        somadata.insert("communication_imports", [import_row(report)])
        reports.append(report)
    print(json.dumps({"resolver": inputs, "imports": reports}, indent=1))


def cmd_refresh(args) -> None:
    state = _state(args)
    waiting = state.waiting()
    origin, why = "hub", None
    rows = []
    if args.local:
        origin, why = "local", "asked for the local pass"
    elif waiting:
        origin, why = "local", f"{waiting} accepted events have not reached the hub yet"
    else:
        try:
            rows = hub_aggregates()
        except (HubError, OSError) as e:
            origin, why = "local", f"hub unreachable: {e}"
    if origin == "local":
        rows = local_aggregates(state)
    resolver, inputs = load_resolver(dict(LIVE))
    imports = somadata.sql(
        "SELECT source, coverage, event_min_at, window_end FROM communication_imports "
        "WHERE deleted_at IS NULL"
    )
    now = datetime.now(timezone.utc)
    summaries = summarize(rows, resolver, channel_coverage(imports), now)
    written = write_summaries(summaries)
    refs = {r["participant_ref"] for r in rows}
    resolved = sum(1 for r in refs if resolver.person(r))
    print(
        json.dumps(
            {
                "source": origin,
                "why": why,
                "resolver": inputs,
                "participants": {"total": len(refs), "resolved_now": resolved},
                "summaries": len(summaries),
                **written,
            },
            indent=1,
        )
    )


def cmd_coverage(args) -> None:
    rows = somadata.sql(
        "SELECT source, source_instance, coverage, observed_rows, accepted, excluded, duplicates, "
        "excluded_by_reason, event_min_at, event_max_at, window_start, window_end, completed_at, "
        "reason FROM communication_imports WHERE deleted_at IS NULL ORDER BY completed_at"
    )
    state = _state(args)
    outbox = {STREAM[s]: state.waiting(STREAM[s]) for s in SOURCES}
    print(json.dumps({"imports": rows, "outbox": outbox}, indent=1))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="people-sync comm",
        description="Communication history as metadata only (who, when, channel, direction, "
        "call outcome and duration): import into soma streams, refresh per-person summaries.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    imp = sub.add_parser("import", help="append a source's new events to its soma stream")
    imp.add_argument("source", choices=(*SOURCES, "all"))
    imp.add_argument(
        "--full",
        action="store_true",
        help="re-read the whole store; known events count as duplicates",
    )
    imp.add_argument(
        "--store",
        type=Path,
        help="read this store (a backup .db or .db.gz) instead of the live one",
    )
    imp.add_argument(
        "--lid-store",
        type=Path,
        help="WhatsApp LID.sqlite (or .gz) that pairs account ids with numbers",
    )
    imp.add_argument(
        "--instance",
        help="store label in records and import rows (default: live, or backup:<folder>/<file>)",
    )
    imp.add_argument(
        "--whatsapp-self-id",
        help="the account's own phone JID (default: WhatsApp's OwnJabberID preference)",
    )
    imp.add_argument("--state-dir", help="private local state root (default: XDG state)")
    imp.set_defaults(func=cmd_import)
    ref_p = sub.add_parser("refresh", help="recompute communication_summaries from the streams")
    ref_p.add_argument(
        "--local", action="store_true", help="use the local mirror instead of the hub"
    )
    ref_p.add_argument("--state-dir")
    ref_p.set_defaults(func=cmd_refresh)
    cov = sub.add_parser("coverage", help="print the import runs and the local outbox")
    cov.add_argument("--state-dir")
    cov.set_defaults(func=cmd_coverage)
    args = parser.parse_args(argv)
    args.func(args)
