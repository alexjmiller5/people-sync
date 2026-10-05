"""Exact-batch human approval and crash-safe, serial relationship removal.

Operational state is private local state, never a replacement for the ledger.
An uncertain action is a permanent no-retry barrier until verified absent.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import termios
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass

from people_sync import captures, lifedata
from people_sync.scrape import cdp, facebook_action, instagram_action, venmo_action
from people_sync.scrape.pace import Pacer

OPERATIONS = {
    "instagram": "unfollow",
    "facebook": "unfriend",
    "linkedin": "remove-connection",
    "venmo": "remove-friend",
}
ADAPTERS = {"instagram": instagram_action, "facebook": facebook_action, "venmo": venmo_action}
SUPPORTED = {("instagram", "unfollow"), ("venmo", "remove-friend")}
TTL = 3600
REMOTE_ID_SQL = "coalesce(json_extract(raw, '$.id'), json_extract(raw, '$.pk'), json_extract(raw, '$.graphql_user.id'), json_extract(raw, '$.web_profile_info.data.user.id'))"
COLUMNS = (
    "id",
    "source",
    "source_id",
    "handle",
    "status",
    "deleted_at",
    "i_follow",
    "updated_at",
    "url",
    "remote_id",
    "raw",
)
SELECT = (
    "SELECT id, source, source_id, handle, status, deleted_at, i_follow, updated_at, raw, "
    f"json_extract(raw, '$.url') AS url, {REMOTE_ID_SQL} AS remote_id FROM people_sync_records"
)
Refused = instagram_action.Refused


def now():
    return int(time.time())


def digest(value):
    return hashlib.sha256(captures.encode(value)).hexdigest()


def require(condition, reason="invalid plan"):
    if not condition:
        raise Refused(reason)


def adapter(platform):
    require(platform in ADAPTERS, "unsupported source/action")
    return ADAPTERS[platform]


@dataclass(frozen=True)
class Target:
    record_id: str
    source_id: str
    handle: str
    url: str
    ledger_sha256: str
    remote_id: str | None


@dataclass(frozen=True)
class Plan:
    version: int
    platform: str
    operation: str
    actor: str
    created_at: int
    expires_at: int
    targets: tuple[Target, ...]
    digest: str


def plan_dict(plan):
    value = asdict(plan)
    value["targets"] = list(value["targets"])
    return value


def canonical_url(platform, handle):
    require(isinstance(handle, str) and 0 < len(handle) <= 200, "invalid canonical handle")
    if platform == "facebook" and re.fullmatch(r"profile\.php\?id=[1-9][0-9]*", handle):
        return "https://www.facebook.com/" + handle
    require(
        re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", handle) is not None, "invalid canonical handle"
    )
    require(
        ".." not in handle
        and handle.lower()
        not in {
            "accounts",
            "explore",
            "direct",
            "reels",
            "stories",
            "p",
            "reel",
            "me",
            "login",
            "friends",
            "groups",
            "settings",
            "feed",
            "checkpoint",
            "challenge",
        },
        "invalid canonical handle",
    )
    if platform == "instagram":
        require(len(handle) <= 30 and "-" not in handle, "invalid Instagram handle")
    return {
        "instagram": f"https://www.instagram.com/{handle}/",
        "facebook": f"https://www.facebook.com/{handle}",
        "linkedin": f"https://www.linkedin.com/in/{handle}/",
        "venmo": f"https://account.venmo.com/u/{handle}",
    }[platform]


def parse_plan(value, *, expired_ok=False):
    try:
        require(type(value) is dict and set(value) == set(Plan.__dataclass_fields__))
        require(type(value["version"]) is int and value["version"] == 1)
        platform = value["platform"]
        require(platform in OPERATIONS and value["operation"] == OPERATIONS[platform])
        canonical_url(platform, value["actor"])
        created, expires = value["created_at"], value["expires_at"]
        require(type(created) is int and type(expires) is int)
        require(0 < created <= now() and 0 < expires - created <= TTL)
        require(expired_ok or now() < expires, "plan expired; create a new preview")
        require(type(value["targets"]) is list and 0 < len(value["targets"]) <= 100)
        targets = []
        for t in value["targets"]:
            require(type(t) is dict and set(t) == set(Target.__dataclass_fields__))
            strings = [v for k, v in t.items() if k != "remote_id"]
            require(all(isinstance(v, str) and 0 < len(v) <= 600 for v in strings))
            require(not any(unicodedata.category(c) in {"Cc", "Cf"} for v in strings for c in v))
            require(
                t["remote_id"] is None
                or (
                    isinstance(t["remote_id"], str)
                    and re.fullmatch(r"[1-9][0-9]{0,29}", t["remote_id"]) is not None
                )
            )
            require(t["record_id"] == f"{platform}:{t['source_id']}")
            require(t["url"] == canonical_url(platform, t["handle"]))
            require(t["handle"].casefold() != value["actor"].casefold(), "self action refused")
            require(re.fullmatch(r"[0-9a-f]{64}", t["ledger_sha256"]) is not None)
            targets.append(Target(**t))
        require(len({t.record_id for t in targets}) == len(targets), "duplicate target")
        require(len({t.url.casefold() for t in targets}) == len(targets), "duplicate identity")
        bound = [t.remote_id for t in targets if t.remote_id is not None]
        require(len(set(bound)) == len(bound), "duplicate remote identity")
        require(
            value["digest"] == digest({k: v for k, v in value.items() if k != "digest"}),
            "plan digest mismatch",
        )
        return Plan(**{**value, "targets": tuple(targets)})
    except (KeyError, TypeError, ValueError):
        raise Refused("invalid plan") from None


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def load_plan(path, *, expired_ok=False):
    try:
        path = captures.private_path(path)
        require(path.stat().st_size <= 1024 * 1024, "plan too large")
        return parse_plan(
            json.loads(path.read_text(), object_pairs_hook=_pairs), expired_ok=expired_ok
        )
    except (OSError, ValueError):
        raise Refused("cannot read private plan") from None


def _rows(ids):
    rows = lifedata.sql(f"{SELECT} WHERE id IN ({','.join(lifedata.sq(i) for i in ids)})")
    require(len(rows) == len(ids) and {r["id"] for r in rows} == set(ids), "missing ledger target")
    return {r["id"]: r for r in rows}


def _snapshot(row):
    require(all(k in row for k in COLUMNS), "incomplete ledger row")
    return {k: row[k] for k in COLUMNS}


def _target(row, platform, remote_id=None):
    require(
        row["source"] == platform and row["status"] == "ignored" and row["deleted_at"] is None,
        "target is not currently ignored",
    )
    url = canonical_url(platform, row["handle"])
    require(not row["url"] or row["url"].rstrip("/") == url.rstrip("/"), "conflicting profile URL")
    observed = row["remote_id"]
    if observed is not None:
        require(
            type(observed) in {int, str}
            and re.fullmatch(r"[1-9][0-9]{0,29}", str(observed)) is not None,
            "invalid retained numeric identity",
        )
        require(remote_id is None or remote_id == str(observed), "conflicting numeric identity")
        remote_id = str(observed)
    return Target(
        row["id"], row["source_id"], row["handle"], url, digest(_snapshot(row)), remote_id
    )


def make_plan(platform, operation, actor, ids, *, remote_ids=None):
    require(platform in OPERATIONS and operation == OPERATIONS[platform], "unsupported operation")
    require(0 < len(ids) <= 100 and len(ids) == len(set(ids)), "select 1-100 distinct record IDs")
    remote_ids = remote_ids or {}
    require(set(remote_ids) <= set(ids), "numeric identity supplied for unselected record")
    rows = _rows(ids)
    require(
        operation != "unfollow" or all(rows[i]["i_follow"] != 0 for i in ids),
        "target already absent in ledger",
    )
    value = dict(
        version=1,
        platform=platform,
        operation=operation,
        actor=actor,
        created_at=now(),
        expires_at=now() + TTL,
        targets=[asdict(_target(rows[i], platform, remote_ids.get(i))) for i in ids],
    )
    return parse_plan({**value, "digest": digest(value)})


def state_root():
    return captures.private_path(captures.state_directory() / "unfollow")


def _private_dir(path):
    path = captures.private_path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    require(stat.S_IMODE(path.stat().st_mode) == 0o700, "unfollow directory must be private (0700)")
    return path


def _sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save_plan(plan):
    return _save_json("plans", plan.digest, plan_dict(plan))


def save_observations(plan, observations):
    return _save_json("observations", plan.digest, observations)


def _save_json(kind, key, value):
    root = _private_dir(state_root())
    directory = _private_dir(root / kind)
    path = directory / f"{key}.json"
    body = captures.encode(value)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    except FileExistsError:
        require(path.read_bytes() == body, "immutable plan differs")
        return path
    with os.fdopen(fd, "wb") as f:
        f.write(body)
        f.flush()
        os.fsync(f.fileno())
    _sync_dir(directory)
    return path


def prepare_plan(platform, actor, ids, *, endpoint=None, target_id=None):
    """Read-only live identity/relationship observation; no approval or estate writes."""
    driver = adapter(platform)
    initial = make_plan(platform, OPERATIONS[platform], actor, ids)
    pacer = Pacer(platform, state_path=str(captures.state_directory() / "scrape-state.json"))
    remote_ids, observations = {}, []
    stop = threading.Event()
    with platform_lock(pacer):
        browser = cdp.Browser.connect(endpoint=endpoint, target_id=target_id)
        try:
            browser.watch_blocks(initial.targets[0].url, lambda reason: stop.set(), stop)
            for target in initial.targets:
                require(not stop.is_set(), "source warning; preparation stopped")
                validate_current(initial, {})
                require(pacer.reserve(), "pacing limit; preparation stopped")
                require(not stop.wait(pacer.next_gap()), "source warning; preparation stopped")
                observation = driver.observe(browser, actor, target.handle, target.url)
                require(not stop.is_set(), "source warning; preparation stopped")
                require(
                    isinstance(observation, dict)
                    and isinstance(observation.get("remote_id"), str)
                    and observation.get("state") in {"following", "absent"},
                    "live identity or relationship not verified",
                )
                remote_ids[target.record_id] = observation["remote_id"]
                observations.append(
                    {
                        "record_id": target.record_id,
                        **{k: observation[k] for k in ("remote_id", "state")},
                    }
                )
                pacer.record()
            validate_current(initial, {})
            value = plan_dict(initial)
            for target in value["targets"]:
                observed = remote_ids[target["record_id"]]
                require(
                    target["remote_id"] is None or target["remote_id"] == observed,
                    "conflicting stable identity",
                )
                target["remote_id"] = observed
            value["created_at"] = now()
            value["expires_at"] = now() + TTL
            value["digest"] = digest({k: v for k, v in value.items() if k != "digest"})
            return parse_plan(value), observations
        finally:
            browser.close()


def preview(plan, *, resume=False):
    mode = "VERIFY ONLY (no external actions)" if resume else plan.operation
    lines = [
        f"{mode}: {len(plan.targets)} exact targets on {plan.platform}",
        f"Signed-in account must be: {plan.actor}",
        f"Batch SHA256: {plan.digest}",
        f"Expires (Unix UTC): {plan.expires_at}",
    ]
    for target in plan.targets:
        lines.append(
            f"  {plan.operation} | {target.record_id} | remote ID: {target.remote_id or 'not observed (handle only)'} | {target.url}"
        )
    if (plan.platform, plan.operation) not in SUPPORTED:
        lines.append("UNSUPPORTED: no verified operation adapter; apply will refuse.")
    return "\n".join(lines)


def approve(plan, resume=False):
    """No flags, environment values, piped stdin or default answers authorize actions."""
    require(sys.stdin.isatty() and sys.stdout.isatty(), "approval requires a real foreground TTY")
    try:
        with open("/dev/tty", "r+b", buffering=0) as tty:
            fd = tty.fileno()
            require(
                os.isatty(fd) and os.tcgetpgrp(fd) == os.getpgrp(),
                "approval requires a real foreground TTY",
            )
            require(termios.tcgetattr(fd)[3] & termios.ICANON, "approval requires a canonical TTY")
            verb = "VERIFY" if resume else "REMOVE"
            phrase = f"{verb} {len(plan.targets)} {plan.platform} {plan.operation} {plan.digest}"
            tty.write((preview(plan, resume=resume) + "\n\n").encode())
            # Discard input queued before the exact challenge is shown.
            termios.tcflush(fd, termios.TCIFLUSH)
            tty.write(
                (
                    "Type this exact phrase yourself (anything else cancels):\n" + phrase + "\n> "
                ).encode()
            )
            require(tty.readline() == (phrase + "\n").encode(), "approval canceled")
    except (OSError, EOFError):
        raise Refused("approval requires a real foreground TTY") from None


def _key(plan, target):
    return digest([plan.platform, plan.actor.casefold(), target.source_id])


def _journal_path():
    return captures.private_path(state_root() / "journal.sqlite3")


def read_journal():
    path = _journal_path()
    if not path.exists():
        return []
    require(stat.S_IMODE(path.stat().st_mode) == 0o600, "journal must be private (0600)")
    with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute("SELECT * FROM actions ORDER BY rowid")]


@contextlib.contextmanager
def journal():
    root = _private_dir(state_root())
    lock_path = captures.private_path(root / "run.lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused("another unfollow run is active") from None
        path = _journal_path()
        # Create privately before SQLite opens it (also keeps rollback journals private).
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        require(stat.S_IMODE(path.stat().st_mode) == 0o600, "journal must be private (0600)")
        with contextlib.closing(sqlite3.connect(path)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA fullfsync=ON")
            db.execute(
                "CREATE TABLE IF NOT EXISTS actions (key TEXT PRIMARY KEY, "
                "digest TEXT NOT NULL, plan TEXT NOT NULL, record_id TEXT NOT NULL, "
                "url TEXT NOT NULL, state TEXT NOT NULL, result TEXT, "
                "verified_at TEXT, ledger_at TEXT)"
            )
            db.commit()
            _sync_dir(root)
            yield db


def _entries(plan):
    return {e["key"]: e for e in read_journal()}


def validate_current(plan, entries, *, resume=False):
    rows = _rows([t.record_id for t in plan.targets])
    for target in plan.targets:
        row = rows[target.record_id]
        current = _target(row, plan.platform, target.remote_id)
        require(
            (current.record_id, current.source_id, current.handle, current.url)
            == (target.record_id, target.source_id, target.handle, target.url),
            "ledger identity changed",
        )
        entry = entries.get(_key(plan, target))
        own = entry and entry["digest"] == plan.digest
        completed_write = (
            own
            and entry["state"] in {"verified", "done"}
            and _relationship_completed(plan, row)
            and row["updated_at"] == entry["ledger_at"]
        )
        require(
            current.ledger_sha256 == target.ledger_sha256 or completed_write,
            "ledger changed; replan",
        )
        if entry:
            require(own, "target has a prior journal entry; use its original plan and resume")
            require(resume, "batch already attempted; use resume (verification only)")
    if resume:
        require(
            any(e["digest"] == plan.digest for e in entries.values()),
            "no attempted batch to resume",
        )
    return rows


def _set(db, key, **values):
    db.execute(
        "UPDATE actions SET " + ",".join(f"{k}=?" for k in values) + " WHERE key=?",
        (*values.values(), key),
    )
    db.commit()


def _relationship_completed(plan, row):
    if plan.operation == "unfollow":
        return row["i_follow"] == 0
    try:
        marker = json.loads(row["raw"] or "{}").get("people_sync_relationship", {})
        return (
            marker.get("operation") == plan.operation
            and marker.get("state") == "absent"
            and marker.get("actor") == plan.actor
            and marker.get("plan_digest") == plan.digest
        )
    except (ValueError, TypeError, AttributeError):
        return False


def _finish(db, plan, target, row, key, result):
    timestamp = lifedata.now_iso()
    prior = db.execute("SELECT ledger_at FROM actions WHERE key=?", (key,)).fetchone()
    ledger_written = (
        prior["ledger_at"] is not None
        and _relationship_completed(plan, row)
        and row["updated_at"] == prior["ledger_at"]
    )
    # Keep the timestamp that the estate already acknowledges. In particular,
    # a failed provenance retry must not manufacture a new ledger timestamp.
    _set(db, key, state="verified", result=result, verified_at=timestamp)
    if ledger_written:
        require(
            _snapshot(_rows([target.record_id])[target.record_id]) == _snapshot(row),
            "ledger changed after live verification; journal retained",
        )
    else:
        _set(db, key, ledger_at=timestamp)
        # Compare-and-set closes a concurrent estate edit between validation and this write.
        guards = []
        for column in COLUMNS:
            expression = {"url": "json_extract(raw, '$.url')", "remote_id": REMOTE_ID_SQL}.get(
                column, column
            )
            value = row[column]
            literal = str(value) if type(value) is int else lifedata.sq(value)
            guards.append(f"{expression} IS {literal}")
        change = "i_follow=0"
        if plan.operation != "unfollow":
            marker = json.dumps(
                {
                    "operation": plan.operation,
                    "state": "absent",
                    "actor": plan.actor,
                    "plan_digest": plan.digest,
                    "verified_at": timestamp,
                }
            )
            change = (
                "raw=json_set(coalesce(raw, '{}'), '$.people_sync_relationship', json("
                + lifedata.sq(marker)
                + "))"
            )
        updated = lifedata.sql(
            "UPDATE people_sync_records SET "
            + change
            + ", updated_at="
            + lifedata.sq(timestamp)
            + " WHERE "
            + " AND ".join(guards)
            + " RETURNING id"
        )
        require(
            len(updated) == 1 and updated[0]["id"] == target.record_id,
            "ledger changed after live verification; journal retained",
        )
    edge_id = "unfollow:" + digest([plan.digest, target.record_id])
    evidence = lifedata.sql(
        f"SELECT id, deleted_at FROM provenance WHERE id={lifedata.sq(edge_id)}"
    )
    require(
        not evidence or (len(evidence) == 1 and evidence[0]["deleted_at"] is None),
        "existing evidence is deleted; journal retained",
    )
    if not evidence:
        lifedata.insert(
            "provenance",
            [
                {
                    "id": edge_id,
                    "from_kind": "manual",
                    "from_ref": "people-sync-unfollow:" + plan.digest,
                    "to_kind": "people_sync_records",
                    "to_ref": target.record_id,
                    "rel": "evidence_of",
                    "field": "i_follow" if plan.operation == "unfollow" else "raw",
                    "detail": json.dumps(
                        {
                            "cue": f"live {plan.platform} {plan.operation} relationship verified absent",
                            "confidence": "high",
                        }
                    ),
                    "asserted_by": "script:people-sync unfollow",
                }
            ],
        )
    _set(db, key, state="done")


@contextlib.contextmanager
def platform_lock(pacer):
    path = captures.private_path(f"{pacer.state_path}.{pacer.platform}.run.lock")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused("platform run already active") from None
        yield


def execute(plan, *, resume=False, endpoint=None, target_id=None):
    plan = parse_plan(plan_dict(plan), expired_ok=resume)
    require(
        (plan.platform, plan.operation) in SUPPORTED,
        "unsupported source/action; no actions performed",
    )
    driver = adapter(plan.platform)
    require(
        all(t.remote_id is not None for t in plan.targets),
        "stable identity required; run unfollow prepare",
    )
    entries = _entries(plan)
    validate_current(plan, entries, resume=resume)
    pacer = Pacer(plan.platform, state_path=str(captures.state_directory() / "scrape-state.json"))
    approve(plan, resume)
    # Human deliberation can take arbitrarily long. Recheck both time and ledger.
    parse_plan(plan_dict(plan), expired_ok=resume)
    validate_current(plan, _entries(plan), resume=resume)
    with journal() as db, platform_lock(pacer):
        entries = _entries(plan)
        validate_current(plan, entries, resume=resume)
        browser = cdp.Browser.connect(endpoint=endpoint, target_id=target_id)
        stop = threading.Event()
        try:
            browser.watch_blocks(plan.targets[0].url, lambda reason: stop.set(), stop)
            for target in plan.targets:
                key = _key(plan, target)
                entry = entries.get(key)
                if resume and (not entry or entry["state"] == "done"):
                    continue
                require(not stop.is_set(), "source warning; batch stopped")
                parse_plan(plan_dict(plan), expired_ok=resume)
                rows = validate_current(
                    plan,
                    _entries(plan),
                    resume=resume or any(e["digest"] == plan.digest for e in entries.values()),
                )
                require(pacer.reserve(), "pacing limit; batch stopped")
                require(not stop.wait(pacer.next_gap()), "source warning; batch stopped")
                state = driver.inspect(browser, plan, target)
                require(not stop.is_set(), "source warning; batch stopped")
                require(state in {"following", "absent"}, "unverified relationship state")
                if resume:
                    require(state == "absent", "uncertain action still present; no retry permitted")
                else:
                    # Approval and ledger must still be valid immediately before writing intent/clicks.
                    parse_plan(plan_dict(plan))
                    rows = validate_current(
                        plan,
                        _entries(plan),
                        resume=any(e["digest"] == plan.digest for e in entries.values()),
                    )
                    db.execute(
                        "INSERT INTO actions (key,digest,plan,record_id,url,state) VALUES (?,?,?,?,?,?)",
                        (
                            key,
                            plan.digest,
                            captures.encode(plan_dict(plan)).decode(),
                            target.record_id,
                            target.url,
                            "started",
                        ),
                    )
                    db.commit()
                try:
                    if state == "following":
                        require(not stop.is_set(), "source warning; batch stopped")
                        driver.perform(browser, plan, target)
                        # Fresh navigation, no optimistic in-page state or request-status success.
                        require(
                            driver.inspect(browser, plan, target) == "absent",
                            "postcondition failed; outcome unknown; resume verifies only",
                        )
                    require(not stop.is_set(), "source warning; outcome unknown")
                except BaseException:
                    _set(db, key, state="unknown")
                    raise
                _finish(
                    db,
                    plan,
                    target,
                    rows[target.record_id],
                    key,
                    "verified-absent" if resume or state == "following" else "already-absent",
                )
                pacer.record()
                entries = _entries(plan)
        finally:
            browser.close()
        own = [e for e in read_journal() if e["digest"] == plan.digest]
        return {
            "verified": sum(e["state"] == "done" for e in own),
            "unattempted": len(plan.targets) - len(own),
        }
