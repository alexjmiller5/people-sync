import json
import subprocess
from people_sync import lifedata


def test_sql_parses_json(mocker):
    mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=[], returncode=0, stdout='[{"n": 1}]', stderr=""
        ),
    )
    assert lifedata.sql("SELECT 1 AS n") == [{"n": 1}]


def test_sql_raises_on_error(mocker):
    mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom"),
    )
    import pytest

    with pytest.raises(RuntimeError, match="boom"):
        lifedata.sql("SELECT 1")


def test_insert_pipes_rows(mocker):
    run = mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
    )
    lifedata.insert("people_sync_records", [{"id": "x:1"}])
    assert run.call_args.kwargs["input"] == json.dumps([{"id": "x:1"}])
    assert run.call_args.args[0][:3] == ["life", "insert", "people_sync_records"]


def test_insert_empty_is_noop(mocker):
    run = mocker.patch("subprocess.run")
    lifedata.insert("t", [])
    run.assert_not_called()


def test_sq_escapes():
    assert lifedata.sq("O'Brien") == "'O''Brien'"
    assert lifedata.sq(None) == "NULL"


def test_now_iso_shape():
    import re

    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", lifedata.now_iso())


def test_locked_database_is_retried_then_succeeds(mocker):
    import subprocess

    from people_sync import lifedata

    locked = subprocess.CompletedProcess([], 1, "", "sqlite3.OperationalError: database is locked")
    ok = subprocess.CompletedProcess([], 0, "[]", "")
    run = mocker.patch("people_sync.lifedata.subprocess.run", side_effect=[locked, locked, ok])
    mocker.patch("people_sync.lifedata.time.sleep")

    assert lifedata.sql("SELECT 1") == []
    assert run.call_count == 3


def test_locked_database_gives_up_after_the_retry_budget(mocker):
    import subprocess

    import pytest

    from people_sync import lifedata

    locked = subprocess.CompletedProcess([], 1, "", "database is locked")
    mocker.patch("people_sync.lifedata.subprocess.run", return_value=locked)
    sleep = mocker.patch("people_sync.lifedata.time.sleep")

    with pytest.raises(RuntimeError, match="database is locked"):
        lifedata.sql("SELECT 1")
    assert sleep.call_count == lifedata.LOCK_RETRIES - 1


def test_other_failures_are_not_retried(mocker):
    import subprocess

    import pytest

    from people_sync import lifedata

    boom = subprocess.CompletedProcess([], 1, "", "no such table")
    run = mocker.patch("people_sync.lifedata.subprocess.run", return_value=boom)

    with pytest.raises(RuntimeError, match="no such table"):
        lifedata.sql("SELECT 1")
    assert run.call_count == 1
