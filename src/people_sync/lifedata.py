"""Thin wrapper around the life CLI - the only write path to life-data."""

import json
import random
import subprocess
import time
from datetime import datetime, timezone

# Another writer (a second scrape, the sync daemon) holds SQLite briefly;
# a locked statement is retried with a short backoff before it is an error.
LOCK_RETRIES = 40
LOCK_BACKOFF_S = 0.5  # capped per attempt below; ~1.5 min of patience for a busy replica


def _run(cmd: list[str], input: str | None = None) -> str:
    for attempt in range(1, LOCK_RETRIES + 1):
        proc = subprocess.run(cmd, input=input, capture_output=True, text=True)
        if proc.returncode == 0:
            return proc.stdout
        if "database is locked" in proc.stderr and attempt < LOCK_RETRIES:
            time.sleep(min(LOCK_BACKOFF_S * attempt, 3.0) * (0.5 + random.random()))
            continue
        raise RuntimeError(f"{' '.join(cmd)} failed: {proc.stderr.strip()}")
    raise AssertionError("unreachable")


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
