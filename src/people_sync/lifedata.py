"""Thin wrapper around the life CLI - the only write path to life-data."""

import json
import subprocess
from datetime import datetime, timezone


def _run(cmd: list[str], input: str | None = None) -> str:
    proc = subprocess.run(cmd, input=input, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {proc.stderr.strip()}")
    return proc.stdout


def sql(query: str) -> list[dict]:
    out = _run(["life", "sql", query]).strip()
    return json.loads(out) if out else []


def insert(table: str, rows: list[dict]) -> None:
    if not rows:
        return
    _run(["life", "insert", table], input=json.dumps(rows))


def sq(value: str | None) -> str:
    if value is None:
        return "NULL"
    return "'" + value.replace("'", "''") + "'"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
