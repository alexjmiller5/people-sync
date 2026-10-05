"""Approval and recovery contracts, using only invented ledger/browser state."""

from pathlib import Path

import json
import sqlite3

import pytest

from people_sync import cli


def row(handle="example_target", source="instagram"):
    return {
        "id": f"{source}:{handle}",
        "source": source,
        "source_id": handle,
        "handle": handle,
        "status": "ignored",
        "deleted_at": None,
        "i_follow": 1,
        "updated_at": "2026-01-01T00:00:00.000Z",
        "url": None,
        "remote_id": None,
        "raw": "{}",
    }


def test_cli_plan_is_private_exact_and_does_not_write_estate_or_open_browser(
    tmp_path, monkeypatch, mocker, capsys
):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    sql = mocker.patch("people_sync.lifedata.sql", return_value=[row()])
    connect = mocker.patch("people_sync.scrape.cdp.Browser.connect")
    cli.main(
        [
            "unfollow",
            "plan",
            "--platform",
            "instagram",
            "--operation",
            "unfollow",
            "--actor",
            "example_operator",
            "--record-id",
            "instagram:example_target",
        ]
    )
    plans = list((tmp_path / "people-sync" / "unfollow" / "plans").glob("*.json"))
    assert len(plans) == 1
    plan = json.loads(plans[0].read_text())
    assert plan["targets"][0]["record_id"] == "instagram:example_target"
    assert plan["targets"][0]["url"] == "https://www.instagram.com/example_target/"
    assert plan["operation"] == "unfollow"
    assert plans[0].stat().st_mode & 0o777 == 0o400
    assert all(call.args[0].startswith("SELECT ") for call in sql.call_args_list)
    connect.assert_not_called()
    assert plan["digest"] in capsys.readouterr().out


def test_cli_apply_requires_real_tty_before_any_effect(tmp_path, monkeypatch, mocker, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    mocker.patch("people_sync.lifedata.sql", return_value=[row()])
    cli.main(
        [
            "unfollow",
            "plan",
            "--platform",
            "instagram",
            "--operation",
            "unfollow",
            "--actor",
            "example_operator",
            "--record-id",
            "instagram:example_target",
            "--remote-id",
            "instagram:example_target=900001",
        ]
    )
    path = next((tmp_path / "people-sync" / "unfollow" / "plans").glob("*.json"))
    connect = mocker.patch("people_sync.scrape.cdp.Browser.connect")
    insert = mocker.patch("people_sync.lifedata.insert")
    with pytest.raises(SystemExit) as exc:
        cli.main(["unfollow", "apply", str(path)])
    assert exc.value.code != 0
    assert "TTY" in capsys.readouterr().err
    connect.assert_not_called()
    insert.assert_not_called()
    assert not (tmp_path / "people-sync" / "unfollow" / "journal.sqlite3").exists()


@pytest.fixture
def estate(monkeypatch, tmp_path):
    from people_sync import lifedata

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE people_sync_records (id TEXT PRIMARY KEY, source TEXT, "
        "source_id TEXT, handle TEXT, status TEXT, deleted_at TEXT, i_follow INTEGER, "
        "updated_at TEXT, raw TEXT)"
    )
    for handle in ("example_target", "example_second"):
        r = row(handle)
        r.pop("url")
        r.pop("remote_id")
        r["raw"] = "{}"
        db.execute("INSERT INTO people_sync_records VALUES (?,?,?,?,?,?,?,?,?)", tuple(r.values()))
    db.execute("ALTER TABLE people_sync_records ADD COLUMN name TEXT")
    db.execute("CREATE TABLE provenance (id TEXT PRIMARY KEY, deleted_at TEXT)")
    writes, edges = [], []

    def sql(query):
        if not query.startswith("SELECT "):
            writes.append(query)
        return [dict(r) for r in db.execute(query).fetchall()]

    monkeypatch.setattr(lifedata, "sql", sql)

    def insert(table, rows):
        for r in rows:
            db.execute("INSERT INTO provenance(id) VALUES (?)", (r["id"],))
        edges.extend(rows)

    monkeypatch.setattr(lifedata, "insert", insert)
    yield db, writes, edges
    db.close()


def make_plan(handles=("example_target",)):
    from people_sync import unfollow_actions as actions

    return actions.make_plan(
        "instagram",
        "unfollow",
        "example_operator",
        [f"instagram:{h}" for h in handles],
        remote_ids={f"instagram:{h}": str(900001 + i) for i, h in enumerate(handles)},
    )


@pytest.mark.parametrize(
    "change",
    [
        {"operation": "unfriend"},
        {"actor": "example_target"},
        {"expires_at": 0},
        {"version": True},
        {"extra": "field"},
        {"created_at": 9999999999},
    ],
)
def test_malformed_or_expired_plan_fails_even_with_recomputed_digest(estate, change):
    from people_sync import unfollow_actions as actions

    value = actions.plan_dict(make_plan())
    value.update(change)
    value["digest"] = actions.digest({k: v for k, v in value.items() if k != "digest"})
    with pytest.raises(actions.Refused):
        actions.parse_plan(value)


def test_plan_digest_identity_and_duplicates_are_validated(estate):
    from people_sync import unfollow_actions as actions

    value = actions.plan_dict(make_plan())
    value["targets"][0]["url"] = "https://www.instagram.com/other_target/"
    with pytest.raises(actions.Refused):
        actions.parse_plan(value)
    value["digest"] = actions.digest({k: v for k, v in value.items() if k != "digest"})
    with pytest.raises(actions.Refused):
        actions.parse_plan(value)
    with pytest.raises(actions.Refused):
        make_plan(("example_target", "example_target"))


@pytest.mark.parametrize(
    "change",
    [
        "status='matched'",
        "deleted_at='2026-01-02'",
        "handle='other_target'",
        "source_id='other_id'",
        "i_follow=0",
        "updated_at='2026-01-03'",
    ],
)
def test_ledger_drift_refuses_before_approval_or_browser(estate, mocker, change):
    from people_sync import unfollow_actions as actions

    plan = make_plan()
    estate[0].execute(f"UPDATE people_sync_records SET {change}")
    approve = mocker.patch.object(
        actions, "approve", side_effect=AssertionError("approval reached")
    )
    browser = mocker.patch("people_sync.scrape.cdp.Browser.connect")
    with pytest.raises(actions.Refused):
        actions.execute(plan)
    approve.assert_not_called()
    browser.assert_not_called()
    assert not estate[1]


@pytest.mark.parametrize("flag", ["--yes", "--force", "--approve", "--operation", "--json"])
def test_unknown_apply_flags_cannot_cause_any_effect(estate, mocker, flag):
    connect = mocker.patch("people_sync.scrape.cdp.Browser.connect")
    with pytest.raises(SystemExit):
        cli.main(["unfollow", "apply", "/no/plan.json", flag])
    assert not estate[1]
    connect.assert_not_called()


class FakeBrowser:
    def __init__(self):
        self.closed = False
        self.visits = []

    def watch_blocks(self, url, callback, stop):
        self.stop = stop

    def close(self):
        self.closed = True


@pytest.fixture
def engine(estate, mocker):
    from people_sync import unfollow_actions as actions

    browser = FakeBrowser()
    mocker.patch("people_sync.scrape.cdp.Browser.connect", return_value=browser)
    # Only the human boundary and real remote effects are replaced. Plans,
    # journal, SQL guards, recovery and control flow stay real.
    mocker.patch.object(actions, "approve")
    mocker.patch.object(actions.Pacer, "reserve", return_value=True)
    mocker.patch.object(actions.Pacer, "next_gap", return_value=0)
    mocker.patch.object(actions.Pacer, "record")
    remote = {"example_target": "following", "example_second": "following"}
    clicks = []

    def inspect(browser, plan, target):
        browser.visits.append(target.handle)
        return remote[target.handle]

    def perform(browser, plan, target):
        clicks.append(target.handle)
        remote[target.handle] = "absent"

    mocker.patch.object(actions.instagram_action, "inspect", side_effect=inspect)
    mocker.patch.object(actions.instagram_action, "perform", side_effect=perform)
    return actions, browser, remote, clicks


def test_engine_verified_completion_preserves_url_and_writes_provenance(estate, engine):
    actions, browser, remote, clicks = engine
    plan = make_plan()
    actions.execute(plan)
    assert clicks == ["example_target"]
    assert browser.visits == ["example_target", "example_target"]
    assert browser.closed
    stored = (
        estate[0]
        .execute("SELECT * FROM people_sync_records WHERE id=?", (plan.targets[0].record_id,))
        .fetchone()
    )
    assert stored["i_follow"] == 0 and stored["status"] == "ignored"
    assert plan.targets[0].url in json.dumps(actions.read_journal())
    assert estate[2][0]["field"] == "i_follow"
    assert estate[2][0]["rel"] == "evidence_of"
    assert plan.digest in estate[2][0]["from_ref"]
    assert actions.read_journal()[0]["state"] == "done"


def test_already_unfollowed_never_clicks(estate, engine):
    actions, browser, remote, clicks = engine
    remote["example_target"] = "absent"
    actions.execute(make_plan())
    assert clicks == []
    assert actions.read_journal()[0]["result"] == "already-absent"
    assert estate[1] and estate[2]


@pytest.mark.parametrize("failure", ["warning", "wrong-identity", "ambiguous-dom"])
def test_inspection_failure_stops_entire_batch_without_mutation(estate, engine, mocker, failure):
    actions, browser, remote, clicks = engine
    mocker.patch.object(actions.instagram_action, "inspect", side_effect=actions.Refused(failure))
    with pytest.raises(actions.Refused):
        actions.execute(make_plan(("example_target", "example_second")))
    assert clicks == []
    assert not estate[1] and not estate[2]
    assert browser.closed


def test_failed_postcondition_stays_unknown_and_cannot_be_retried_by_new_plan(
    estate, engine, mocker
):
    actions, browser, remote, clicks = engine
    mocker.patch.object(
        actions.instagram_action, "perform", side_effect=lambda *args: clicks.append("attempt")
    )
    plan = make_plan()
    with pytest.raises(actions.Refused):
        actions.execute(plan)
    assert actions.read_journal()[0]["state"] == "unknown"
    assert not estate[1] and not estate[2]
    with pytest.raises(actions.Refused):
        actions.execute(make_plan())
    with pytest.raises(actions.Refused):
        actions.execute(plan, resume=True)
    assert clicks == ["attempt"]


def test_interruption_after_click_recovers_by_read_only_verification(estate, engine, mocker):
    actions, browser, remote, clicks = engine
    plan = make_plan()

    def lost_reply(*args):
        clicks.append("attempt")
        remote["example_target"] = "absent"
        raise KeyboardInterrupt

    mocker.patch.object(actions.instagram_action, "perform", side_effect=lost_reply)
    with pytest.raises(KeyboardInterrupt):
        actions.execute(plan)
    assert actions.read_journal()[0]["state"] in {"started", "unknown"}
    # Recovery may be needed after the original authorization expires.
    mocker.patch.object(actions, "now", return_value=plan.expires_at + 10)
    actions.execute(plan, resume=True)
    assert clicks == ["attempt"]
    assert estate[1] and estate[2]
    assert actions.read_journal()[0]["state"] == "done"


def test_resume_does_not_execute_unstarted_targets(estate, engine, mocker):
    actions, browser, remote, clicks = engine
    plan = make_plan(("example_target", "example_second"))
    mocker.patch.object(actions.instagram_action, "perform", side_effect=RuntimeError("secret URL"))
    with pytest.raises(RuntimeError):
        actions.execute(plan)
    remote["example_target"] = "absent"
    actions.execute(plan, resume=True)
    assert clicks == []
    assert remote["example_second"] == "following"
    assert (
        estate[0]
        .execute("SELECT i_follow FROM people_sync_records WHERE handle='example_second'")
        .fetchone()[0]
        == 1
    )


def test_unsupported_action_refuses_before_approval(estate, mocker):
    from people_sync import unfollow_actions as actions

    estate[0].execute("UPDATE people_sync_records SET source='linkedin', id='linkedin:'||source_id")
    plan = actions.make_plan(
        "linkedin",
        "remove-connection",
        "example_operator",
        ["linkedin:example_target"],
        remote_ids={"linkedin:example_target": "900001"},
    )
    approve = mocker.patch.object(actions, "approve")
    with pytest.raises(actions.Refused, match="unsupported"):
        actions.execute(plan)
    approve.assert_not_called()
    assert not estate[1]


def test_approval_rechecks_ledger_after_human_wait(estate, engine, mocker):
    actions, browser, remote, clicks = engine
    mocker.patch.object(
        actions,
        "approve",
        side_effect=lambda *args: estate[0].execute(
            "UPDATE people_sync_records SET status='matched'"
        ),
    )
    with pytest.raises(actions.Refused):
        actions.execute(make_plan())
    assert not clicks and not estate[1]


def test_remote_numeric_identity_is_bound_into_plan_and_rechecked(estate, mocker):
    from people_sync import unfollow_actions as actions

    estate[0].execute(
        "UPDATE people_sync_records SET raw=? WHERE handle='example_target'",
        (json.dumps({"id": "900001"}),),
    )
    plan = make_plan()
    assert plan.targets[0].remote_id == "900001"
    assert "900001" in actions.preview(plan)
    estate[0].execute(
        "UPDATE people_sync_records SET raw=? WHERE handle='example_target'",
        (json.dumps({"id": "900002"}),),
    )
    with pytest.raises(actions.Refused):
        actions.execute(plan)


def test_explicit_observed_numeric_identity_survives_plan_roundtrip(estate):
    from people_sync import unfollow_actions as actions

    plan = actions.make_plan(
        "instagram",
        "unfollow",
        "example_operator",
        ["instagram:example_target"],
        remote_ids={"instagram:example_target": "900001"},
    )
    assert actions.load_plan(actions.save_plan(plan)) == plan
    assert plan.targets[0].remote_id == "900001"
    with pytest.raises(actions.Refused):
        actions.make_plan(
            "instagram",
            "unfollow",
            "example_operator",
            ["instagram:example_target"],
            remote_ids={"instagram:wrong": "900001"},
        )


def test_successful_multi_target_batch(estate, engine):
    actions, browser, remote, clicks = engine
    actions.execute(make_plan(("example_target", "example_second")))
    assert clicks == ["example_target", "example_second"]
    assert len(estate[1]) == len(estate[2]) == 2
    assert all(e["state"] == "done" for e in actions.read_journal())


def test_resume_reports_partial_when_batch_has_unattempted_targets(estate, engine, mocker):
    actions, browser, remote, clicks = engine
    plan = make_plan(("example_target", "example_second"))
    mocker.patch.object(actions.instagram_action, "perform", side_effect=RuntimeError)
    with pytest.raises(RuntimeError):
        actions.execute(plan)
    remote["example_target"] = "absent"
    result = actions.execute(plan, resume=True)
    assert result == {"verified": 1, "unattempted": 1}


def test_no_completion_text_on_unsupported_or_partial_cli(estate, engine, mocker, capsys):
    actions, browser, remote, clicks = engine
    path = actions.save_plan(make_plan())
    mocker.patch.object(actions, "execute", return_value={"verified": 0, "unattempted": 1})
    with pytest.raises(SystemExit) as exc:
        cli.main(["unfollow", "resume", str(path)])
    assert exc.value.code == 2
    out = capsys.readouterr()
    assert "finished" not in out.out and "unattempted" in out.out


def test_provenance_failure_can_be_repaired_without_repeating_action(estate, engine, mocker):
    actions, browser, remote, clicks = engine
    plan = make_plan()
    insert = mocker.patch("people_sync.lifedata.insert", side_effect=RuntimeError)
    with pytest.raises(RuntimeError):
        actions.execute(plan)
    assert actions.read_journal()[0]["state"] == "verified"
    insert.side_effect = None
    actions.execute(plan, resume=True)
    assert clicks == ["example_target"]
    assert actions.read_journal()[0]["state"] == "done"


@pytest.mark.parametrize("reply", ["yes", "REMOVE", "correct", "wrong-digest"])
def test_real_foreground_tty_requires_exact_digest_phrase(estate, reply):
    import os
    import pty
    import select
    import subprocess
    import sys
    import time
    from people_sync import unfollow_actions as actions

    plan = make_plan()
    phrase = f"REMOVE 1 instagram unfollow {plan.digest}"
    program = """
import fcntl, json, sys, termios
from people_sync import unfollow_actions as actions
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
try:
    actions.approve(actions.parse_plan(json.loads(sys.argv[1])))
except actions.Refused:
    sys.exit(7)
"""
    master, slave = pty.openpty()
    child = subprocess.Popen(
        [sys.executable, "-c", program, json.dumps(actions.plan_dict(plan))],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
        env={**os.environ, "PYTHONPATH": str(Path("src").resolve())},
    )
    os.close(slave)
    try:
        output = b""
        deadline = time.monotonic() + 5
        while b"\r\n> " not in output and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                chunk = os.read(master, 65536)
                if not chunk:
                    break
                output += chunk
        assert b"\r\n> " in output, output.decode(errors="replace")
        assert plan.targets[0].url.encode() in output and plan.digest.encode() in output
        supplied = (
            phrase
            if reply == "correct"
            else (phrase[:-1] + "x" if reply == "wrong-digest" else reply)
        )
        os.write(master, (supplied + "\n").encode())
        # Drain terminal echo before waiting: macOS waits for slave output to
        # drain during process exit, even after SIGKILL. Never block in waitpid.
        while child.poll() is None and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    if not os.read(master, 65536):
                        break
                except OSError:
                    break
        assert child.wait(timeout=1) == (0 if reply == "correct" else 7)
    finally:
        os.close(master)
        if child.poll() is None:
            child.kill()
        child.wait(timeout=2)


def test_environment_cannot_approve(estate, monkeypatch):
    from people_sync import unfollow_actions as actions

    monkeypatch.setenv("PEOPLE_SYNC_UNFOLLOW_APPROVED", "yes")
    monkeypatch.setenv("CI", "true")
    with pytest.raises(actions.Refused, match="TTY"):
        actions.approve(make_plan())


def test_unsafe_plan_paths_and_duplicate_json_fail_before_actions(estate, tmp_path):
    from people_sync import unfollow_actions as actions

    plan = make_plan()
    path = actions.save_plan(plan)
    link = tmp_path / "linked-plan.json"
    link.symlink_to(path)
    with pytest.raises(actions.Refused):
        actions.load_plan(link)
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(json.dumps(actions.plan_dict(plan))[:-1] + ',"version":1}')
    with pytest.raises(actions.Refused, match="duplicate JSON key"):
        actions.load_plan(duplicate)


def test_warning_after_action_prevents_ledger_success_and_next_action(estate, engine, mocker):
    actions, browser, remote, clicks = engine

    def warned(*args):
        remote["example_target"] = "absent"
        browser.stop.set()

    mocker.patch.object(actions.instagram_action, "perform", side_effect=warned)
    with pytest.raises(actions.Refused):
        actions.execute(make_plan(("example_target", "example_second")))
    assert not estate[1] and not estate[2]
    assert remote["example_second"] == "following"
    assert actions.read_journal()[0]["state"] == "unknown"


def test_expiry_during_browser_inspection_stops_before_action(estate, engine, mocker):
    actions, browser, remote, clicks = engine
    plan = make_plan()

    def expired(*args):
        mocker.patch.object(actions, "now", return_value=plan.expires_at)
        return "following"

    mocker.patch.object(actions.instagram_action, "inspect", side_effect=expired)
    with pytest.raises(actions.Refused):
        actions.execute(plan)
    assert not clicks and not estate[1]
    assert actions.read_journal() == []


def test_compare_and_set_refuses_concurrent_reclassification(estate, engine, mocker):
    actions, browser, remote, clicks = engine
    plan = make_plan()
    count = 0

    def inspect(*args):
        nonlocal count
        count += 1
        if count == 2:
            estate[0].execute("UPDATE people_sync_records SET status='matched'")
            return "absent"
        return "following"

    mocker.patch.object(actions.instagram_action, "inspect", side_effect=inspect)
    with pytest.raises(actions.Refused, match="ledger changed"):
        actions.execute(plan)
    assert not estate[2]
    assert estate[0].execute("SELECT i_follow FROM people_sync_records").fetchone()[0] == 1
    assert actions.read_journal()[0]["state"] == "verified"


def test_second_run_cannot_pass_existing_process_lock(estate, engine):
    actions, browser, remote, clicks = engine
    with actions.journal():
        with pytest.raises(actions.Refused, match="another unfollow run"):
            actions.execute(make_plan())
    assert not clicks and not estate[1]


def test_previous_unrelated_plan_does_not_block_new_batch(estate, engine):
    actions, browser, remote, clicks = engine
    actions.execute(make_plan())
    actions.execute(make_plan(("example_second",)))
    assert clicks == ["example_target", "example_second"]


def test_cli_unexpected_error_has_no_sensitive_exception_text(estate, engine, mocker, capsys):
    actions, browser, remote, clicks = engine
    path = actions.save_plan(make_plan())
    mocker.patch.object(actions, "execute", side_effect=RuntimeError("SECRET_IN_BROWSER_ERROR"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["unfollow", "apply", str(path)])
    assert exc.value.code == 1
    output = capsys.readouterr()
    assert "SECRET_IN_BROWSER_ERROR" not in output.err
    assert "finished" not in output.out


def test_plan_objects_are_immutable(estate):
    from dataclasses import FrozenInstanceError

    plan = make_plan()
    with pytest.raises(FrozenInstanceError):
        plan.targets[0].url = "https://www.instagram.com/other_target/"


def test_write_intent_is_durable_before_first_click(estate, engine, mocker):
    actions, browser, remote, clicks = engine

    def perform(*args):
        assert actions.read_journal()[0]["state"] == "started"
        remote["example_target"] = "absent"

    mocker.patch.object(actions.instagram_action, "perform", side_effect=perform)
    actions.execute(make_plan())


def test_provenance_reply_loss_does_not_duplicate_evidence_on_resume(estate, engine, mocker):
    from people_sync import lifedata

    actions, browser, remote, clicks = engine
    original = lifedata.insert

    def stored_but_reply_lost(table, values):
        original(table, values)
        raise RuntimeError("reply lost")

    mocker.patch.object(lifedata, "insert", side_effect=stored_but_reply_lost)
    plan = make_plan()
    with pytest.raises(RuntimeError):
        actions.execute(plan)
    mocker.patch.object(lifedata, "insert", side_effect=original)
    actions.execute(plan, resume=True)
    assert len(estate[2]) == 1
    assert clicks == ["example_target"]


def test_platform_scrape_lock_stops_action_engine(estate, engine, tmp_path, mocker):
    import fcntl
    from people_sync.scrape import pace

    actions, browser, remote, clicks = engine
    state_path = tmp_path / "scrape-state.json"
    original = pace.Pacer
    mocker.patch.object(
        actions, "Pacer", side_effect=lambda platform, **kwargs: original(platform, str(state_path))
    )
    with open(f"{state_path}.instagram.run.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(actions.Refused, match="platform run"):
            actions.execute(make_plan())
    assert not clicks and not estate[1]


def test_duplicate_journal_evidence_is_not_treated_as_completion(estate, engine):
    actions, browser, remote, clicks = engine
    plan = make_plan()
    actions.execute(plan)
    estate[0].execute("UPDATE people_sync_records SET i_follow=1")
    with pytest.raises(actions.Refused):
        actions.execute(plan, resume=True)


def test_journal_command_reports_recovery_state_without_browser(estate, engine, mocker, capsys):
    actions, browser, remote, clicks = engine
    plan = make_plan()
    actions.execute(plan)
    connect = mocker.patch("people_sync.scrape.cdp.Browser.connect")
    cli.main(["unfollow", "journal"])
    report = json.loads(capsys.readouterr().out)
    assert report[0]["digest"] == plan.digest
    assert report[0]["state"] == "done"
    connect.assert_not_called()


def test_prepare_observes_stable_ids_without_approval_or_mutation(estate, engine, mocker):
    from types import SimpleNamespace

    actions, browser, remote, clicks = engine
    observer = SimpleNamespace(observe=lambda *_: {"remote_id": "900001", "state": "following"})
    mocker.patch.object(actions, "adapter", return_value=observer, create=True)
    plan, observations = actions.prepare_plan(
        "instagram", "example_operator", ["instagram:example_target"]
    )
    assert plan.targets[0].remote_id == "900001"
    assert observations == [
        {"record_id": "instagram:example_target", "remote_id": "900001", "state": "following"}
    ]
    actions.approve.assert_not_called()
    assert not clicks and not estate[1] and not estate[2]
    assert browser.closed
    assert actions.read_journal() == []


@pytest.mark.parametrize("change", ["status='matched'", "updated_at='2026-01-02T00:00:00.000Z'"])
def test_prepare_refuses_reclassification_during_observation(estate, engine, mocker, change):
    from types import SimpleNamespace

    actions, browser, remote, clicks = engine

    def observe(*_):
        estate[0].execute("UPDATE people_sync_records SET " + change)
        return {"remote_id": "900001", "state": "following"}

    mocker.patch.object(
        actions, "adapter", return_value=SimpleNamespace(observe=observe), create=True
    )
    with pytest.raises(actions.Refused):
        actions.prepare_plan("instagram", "example_operator", ["instagram:example_target"])
    assert browser.closed and not clicks and not estate[1]


@pytest.mark.parametrize(
    "observation",
    [{"remote_id": None, "state": "following"}, {"remote_id": "900001", "state": "unknown"}],
)
def test_prepare_refuses_incomplete_live_identity(estate, engine, mocker, observation):
    from types import SimpleNamespace

    actions, browser, remote, clicks = engine
    mocker.patch.object(
        actions,
        "adapter",
        return_value=SimpleNamespace(observe=lambda *_: observation),
        create=True,
    )
    with pytest.raises(actions.Refused):
        actions.prepare_plan("instagram", "example_operator", ["instagram:example_target"])
    assert not clicks and not estate[1]


def test_prepare_cli_saves_exact_observations_privately(estate, engine, mocker, capsys):
    from types import SimpleNamespace

    actions, browser, remote, clicks = engine
    mocker.patch.object(
        actions,
        "adapter",
        return_value=SimpleNamespace(observe=lambda *_: {"remote_id": "900001", "state": "absent"}),
        create=True,
    )
    cli.main(
        [
            "unfollow",
            "prepare",
            "--platform",
            "instagram",
            "--actor",
            "example_operator",
            "--record-id",
            "instagram:example_target",
        ]
    )
    plan_path = next((actions.state_root() / "plans").glob("*.json"))
    observed_path = next((actions.state_root() / "observations").glob("*.json"))
    assert observed_path.stat().st_mode & 0o777 == 0o400
    assert json.loads(observed_path.read_text())[0]["state"] == "absent"
    assert actions.load_plan(plan_path).targets[0].remote_id == "900001"
    assert "absent" in capsys.readouterr().out
    assert not clicks and not estate[1]


def test_apply_refuses_unbound_identity_before_prompt_or_browser(estate, engine, mocker):
    actions, browser, remote, clicks = engine
    plan = actions.make_plan(
        "instagram", "unfollow", "example_operator", ["instagram:example_target"]
    )
    connect = mocker.patch.object(actions.cdp.Browser, "connect")
    with pytest.raises(actions.Refused, match="prepare"):
        actions.execute(plan)
    actions.approve.assert_not_called()
    connect.assert_not_called()
    assert actions.read_journal() == []


def test_plan_refuses_two_handles_bound_to_same_remote_id(estate):
    from people_sync import unfollow_actions as actions

    with pytest.raises(actions.Refused, match="duplicate remote"):
        actions.make_plan(
            "instagram",
            "unfollow",
            "example_operator",
            ["instagram:example_target", "instagram:example_second"],
            remote_ids={"instagram:example_target": "900001", "instagram:example_second": "900001"},
        )


def test_prepare_final_plan_keeps_observed_snapshot(estate, engine, mocker):
    from types import SimpleNamespace

    actions, browser, remote, clicks = engine
    expected = make_plan().targets[0].ledger_sha256
    original = actions.make_plan

    def replan(*args, **kwargs):
        if kwargs.get("remote_ids"):
            estate[0].execute(
                "UPDATE people_sync_records SET updated_at='changed-after-observation'"
            )
        return original(*args, **kwargs)

    mocker.patch.object(actions, "make_plan", side_effect=replan)
    mocker.patch.object(
        actions,
        "adapter",
        return_value=SimpleNamespace(
            observe=lambda *_: {"remote_id": "900001", "state": "following"}
        ),
    )
    plan, _ = actions.prepare_plan("instagram", "example_operator", ["instagram:example_target"])
    assert plan.targets[0].ledger_sha256 == expected


def test_unfollowed_friend_is_still_eligible_for_unfriend(estate):
    from people_sync import unfollow_actions as actions

    estate[0].execute(
        "UPDATE people_sync_records SET source='facebook',id='facebook:'||source_id,i_follow=0"
    )
    plan = actions.make_plan("facebook", "unfriend", "900009", ["facebook:example_target"])
    assert plan.operation == "unfriend"


@pytest.mark.parametrize(
    ("platform", "operation"), [("facebook", "unfriend"), ("venmo", "remove-friend")]
)
def test_engine_dispatches_friend_adapters(estate, engine, mocker, platform, operation):
    from importlib import import_module

    actions, browser, remote, clicks = engine
    estate[0].execute(
        "UPDATE people_sync_records SET source=?,id=?||source_id", (platform, platform + ":")
    )
    driver = import_module("people_sync.scrape." + platform + "_action")
    mocker.patch.object(driver, "inspect", side_effect=["following", "absent"])
    perform = mocker.patch.object(driver, "perform")
    plan = actions.make_plan(
        platform,
        operation,
        "example_operator",
        [platform + ":example_target"],
        remote_ids={platform + ":example_target": "900001"},
    )
    actions.execute(plan)
    perform.assert_called_once()
    assert not clicks
    assert actions.read_journal()[0]["state"] == "done"


def test_friend_removal_preserves_unknown_follow_state_and_leaves_queue(estate, engine, mocker):
    from people_sync import unfollow

    actions, browser, remote, clicks = engine
    estate[0].execute(
        "UPDATE people_sync_records SET source='venmo', id='venmo:'||source_id, i_follow=NULL"
    )
    mocker.patch.object(actions, "SUPPORTED", actions.SUPPORTED | {("venmo", "remove-friend")})
    mocker.patch.object(actions, "adapter", return_value=actions.instagram_action)
    plan = actions.make_plan(
        "venmo",
        "remove-friend",
        "example_operator",
        ["venmo:example_target"],
        remote_ids={"venmo:example_target": "900001"},
    )
    actions.execute(plan)
    stored = dict(
        estate[0]
        .execute("SELECT * FROM people_sync_records WHERE id='venmo:example_target'")
        .fetchone()
    )
    assert stored["i_follow"] is None
    marker = json.loads(stored["raw"])["people_sync_relationship"]
    assert marker["operation"] == "remove-friend" and marker["state"] == "absent"
    assert marker["actor"] == "example_operator"
    assert estate[2][0]["field"] == "raw"
    assert [r["id"] for r in unfollow.pending()] == ["venmo:example_second"]
    actions.execute(plan, resume=True)
    assert clicks == ["example_target"]


def test_friend_provenance_retry_keeps_follow_state_and_completed_write(estate, engine, mocker):
    actions, browser, remote, clicks = engine
    estate[0].execute(
        "UPDATE people_sync_records SET source='venmo', id='venmo:'||source_id, i_follow=1"
    )
    mocker.patch.object(actions, "SUPPORTED", actions.SUPPORTED | {("venmo", "remove-friend")})
    mocker.patch.object(actions, "adapter", return_value=actions.instagram_action)
    plan = actions.make_plan(
        "venmo",
        "remove-friend",
        "example_operator",
        ["venmo:example_target"],
        remote_ids={"venmo:example_target": "900001"},
    )
    original = actions.lifedata.insert
    insert = mocker.patch.object(actions.lifedata, "insert", side_effect=RuntimeError)
    with pytest.raises(RuntimeError):
        actions.execute(plan)
    before = list(estate[1])
    insert.side_effect = original
    actions.execute(plan, resume=True)
    assert estate[1] == before
    assert (
        estate[0]
        .execute("SELECT i_follow FROM people_sync_records WHERE id='venmo:example_target'")
        .fetchone()[0]
        == 1
    )
    assert clicks == ["example_target"]


def test_repeated_provenance_interruptions_preserve_acknowledged_ledger_write(
    estate, engine, mocker
):
    from people_sync import lifedata

    actions, browser, remote, clicks = engine
    plan = make_plan()
    original = lifedata.insert
    attempts = 0

    def flaky_provenance(table, rows):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise RuntimeError("interrupted evidence write")
        return original(table, rows)

    mocker.patch.object(lifedata, "insert", side_effect=flaky_provenance)
    mocker.patch.object(
        lifedata,
        "now_iso",
        side_effect=[
            "2026-01-02T00:00:00.000Z",
            "2026-01-02T00:01:00.000Z",
            "2026-01-02T00:02:00.000Z",
        ],
    )
    with pytest.raises(RuntimeError):
        actions.execute(plan)
    acknowledged_at = actions.read_journal()[0]["ledger_at"]
    with pytest.raises(RuntimeError):
        actions.execute(plan, resume=True)
    assert actions.read_journal()[0]["ledger_at"] == acknowledged_at
    assert len(estate[1]) == 1
    actions.execute(plan, resume=True)
    assert len(estate[1]) == 1
    assert actions.read_journal()[0]["state"] == "done"
    assert actions.read_journal()[0]["ledger_at"] == acknowledged_at
    assert len(estate[2]) == 1 and clicks == ["example_target"]


def test_resume_skips_completed_ledger_write_even_if_a_second_write_would_fail(
    estate, engine, mocker
):
    from people_sync import lifedata

    actions, browser, remote, clicks = engine
    plan = make_plan()
    original_insert, original_sql = lifedata.insert, lifedata.sql
    mocker.patch.object(lifedata, "insert", side_effect=RuntimeError)
    with pytest.raises(RuntimeError):
        actions.execute(plan)
    acknowledged_at = actions.read_journal()[0]["ledger_at"]
    mocker.patch.object(lifedata, "insert", side_effect=original_insert)

    def no_second_write(query):
        if query.startswith("UPDATE "):
            raise RuntimeError("second interruption")
        return original_sql(query)

    mocker.patch.object(lifedata, "sql", side_effect=no_second_write)
    actions.execute(plan, resume=True)
    assert actions.read_journal()[0]["ledger_at"] == acknowledged_at
    assert actions.read_journal()[0]["state"] == "done"
    assert len(estate[1]) == 1 and len(estate[2]) == 1
