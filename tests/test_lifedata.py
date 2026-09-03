import json
import subprocess
from contact_sync import lifedata


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
    lifedata.insert("contact_records", [{"id": "x:1"}])
    assert run.call_args.kwargs["input"] == json.dumps([{"id": "x:1"}])
    assert run.call_args.args[0][:3] == ["life", "insert", "contact_records"]


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
