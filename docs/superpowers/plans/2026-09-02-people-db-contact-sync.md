# People DB & Contact Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Tasks marked INTERACTIVE require Alex live in the loop** (per-value
> migration decisions). They cannot be completed by an unattended subagent -
> the executor runs their mechanical steps, then pauses for Alex.

**Goal:** Normalize the life-data people estate (accounts, ledger, locations, employments, photos, circles) and build the ad-hoc monthly contacts-review workflow that feeds it from every contact source.

**Architecture:** Plain-Python scripts in this repo (renamed `contact-sync`) write to life-data exclusively through the `life` CLI; a resolution-ledger table makes ingests idempotent and incremental; Claude drives triage via a `contacts-review` skill. No daemons, no cron.

**Tech Stack:** uv, Python 3.13, pytest + pytest-mock, ruff, httpx (Notion raw HTTP), boto3 (R2 only), `life` CLI (subprocess), `gog` CLI (subprocess).

**Spec:** `docs/superpowers/specs/2026-09-02-people-db-contact-sync-design.md` (read it first; the preservation invariant and Google-boundary rules there are binding).

## Global Constraints

- Writes to life-data go through the `life` CLI (`life sql` / `life insert`), never raw sqlite3 against `life.db`.
- Soft deletes only; every read filters `deleted_at IS NULL`.
- Timestamps are ISO-8601 UTC with milliseconds: `2026-09-02T14:33:13.538Z`.
- **Personal data never enters the repo**: no real names, handles, emails, or circle values in code, tests, fixtures, or commit messages. Fixtures are fully synthetic. Migration worksheets/decision files live in `data/` (gitignored).
- **Preservation invariant (spec §Migration step 5)**: no source value is dropped, merged, renamed, split, or reclassified except by Alex's explicit per-value decision.
- Notion writes >2KB use raw curl/httpx, never `ntn` (known CLI hang).
- Notion reads/writes use `NOTION_API_TOKEN` from the environment (standard export per the notion-workspace skill); scripts never read 1Password themselves.
- Commits: plain messages, no attribution or session trailers.
- TDD: failing test first for every module. Fixtures sanitized. Run `just test` and `just check` before every commit.
- This repo is under iCloud Drive: if any git command hangs, run `find .git -type f -print0 | xargs -0 cat > /dev/null` to re-materialize evicted files, then retry.

---

### Task 1: Commit spec; strip the repo to its new shape; rename to contact-sync

**Files:**
- Delete: `src/notion_contact_sync/` (entire package), `tests/` contents, `nix/darwin.nix`, `scripts/run.sh`
- Modify: `flake.nix`, `justfile`, `pyproject.toml`, `.env.tpl`, `AGENTS.md`, `README.md`
- Create: `src/contact_sync/__init__.py` (empty), `tests/__init__.py` (empty)

**Interfaces:**
- Produces: package `contact_sync` importable via `uv run python -c "import contact_sync"`; `just test` / `just check` green on an empty suite; repo named `contact-sync` on GitHub and on disk.

- [ ] **Step 1: Commit the spec** (it is written but uncommitted)

```bash
cd ~/Desktop/coding/active-projects/notion-contact-sync
git add docs/superpowers/specs/2026-09-02-people-db-contact-sync-design.md docs/superpowers/plans/2026-09-02-people-db-contact-sync.md
git commit -m "Add people db & contact sync design spec and plan"
```

- [ ] **Step 2: Delete the presumed-wrong code and the scheduled-job machinery**

```bash
git rm -r src/notion_contact_sync tests nix scripts/run.sh
mkdir -p src/contact_sync tests scripts
touch src/contact_sync/__init__.py tests/__init__.py
```

- [ ] **Step 3: Rewrite `pyproject.toml` package name/deps** - name `contact-sync`, package `contact_sync`, dependencies exactly: `httpx`, `structlog`, `boto3`; dev group: `pytest`, `pytest-mock`, `ruff`. Remove `pydantic-settings` (no long-lived config object; scripts read env directly). Run `uv sync`.

- [ ] **Step 4: Slim `flake.nix` and `justfile`** - flake: remove the `darwinModules` export (no launchd job anymore), keep the dev shell. justfile: keep `test` (`uv run pytest`), `check` (`uv run ruff check . && uv run ruff format --check .`), `fmt` (`uv run ruff format . && uv run ruff check --fix .`); delete `run`/`dev`/`logs`/`store-op-token`.

- [ ] **Step 5: Stub README.md and AGENTS.md** - three lines each stating: contact-sync consolidates contact sources into life-data via ad-hoc runs driven by the contacts-review skill; spec under `docs/superpowers/specs/`; full docs land in Task 14. (Current-state only; no history notes.)

- [ ] **Step 6: Verify empty suite + lint pass**

Run: `just test; just check`
Expected: pytest exits 5 (no tests collected) or 0; ruff clean.

- [ ] **Step 7: Commit, then rename repo and directory**

```bash
git add -A && git commit -m "Strip to contact-sync skeleton: delete legacy parsers, Notion writers, launchd machinery"
gh repo rename contact-sync --repo alexjmiller5/notion-contact-sync --yes
cd ~/Desktop/coding/active-projects && mv notion-contact-sync contact-sync && cd contact-sync
git remote set-url origin https://github.com/alexjmiller5/contact-sync.git
gh repo edit alexjmiller5/contact-sync --description "Consolidates every contact source (Apple, Google, Instagram, Snapchat, LinkedIn, Facebook) into the life-data people estate via ad-hoc agent-driven review runs"
```

Then follow the `repo-metadata` skill for topics, and update the `projects` skill row for this repo (path + name + description).

- [ ] **Step 8: Push and verify** - `git push && gh run list --limit 1` (no deploy workflow should fire; if a legacy workflow exists under `.github/workflows/`, delete it in this commit).

### Task 2: `lifedata.py` - the `life` CLI wrapper every writer uses

**Files:**
- Create: `src/contact_sync/lifedata.py`
- Test: `tests/test_lifedata.py`

**Interfaces:**
- Produces:
  - `sql(query: str) -> list[dict]` - runs `life sql <query>`, parses JSON stdout (empty list on empty output), raises `RuntimeError` with stderr on nonzero exit.
  - `insert(table: str, rows: list[dict]) -> None` - pipes JSON to `life insert <table>`; no-op on empty list.
  - `sq(value: str | None) -> str` - SQL string literal: `None` → `NULL`, else `'...'` with internal `'` doubled.
  - `now_iso() -> str` - ISO-8601 UTC milliseconds, e.g. `2026-09-02T14:33:13.538Z`.

- [ ] **Step 1: Write failing tests** (mock `subprocess.run` with pytest-mock)

```python
import json
import subprocess
from contact_sync import lifedata

def test_sql_parses_json(mocker):
    mocker.patch("subprocess.run", return_value=subprocess.CompletedProcess(
        args=[], returncode=0, stdout='[{"n": 1}]', stderr=""))
    assert lifedata.sql("SELECT 1 AS n") == [{"n": 1}]

def test_sql_raises_on_error(mocker):
    mocker.patch("subprocess.run", return_value=subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="boom"))
    import pytest
    with pytest.raises(RuntimeError, match="boom"):
        lifedata.sql("SELECT 1")

def test_insert_pipes_rows(mocker):
    run = mocker.patch("subprocess.run", return_value=subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr=""))
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
```

- [ ] **Step 2: Run to verify failure** - `uv run pytest tests/test_lifedata.py -v` → FAIL (module missing).

- [ ] **Step 3: Implement**

```python
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
```

- [ ] **Step 4: Verify pass** - `uv run pytest tests/test_lifedata.py -v` → all PASS.

- [ ] **Step 5: Mutation-check** - temporarily break `sq` (drop the doubling), confirm `test_sq_escapes` fails, restore.

- [ ] **Step 6: Commit** - `git add src/contact_sync/lifedata.py tests/test_lifedata.py && git commit -m "feat: life CLI wrapper (sql/insert/escape/timestamps)"`

### Task 3: Backup + schema creation (direct ops, no repo code)

**Files:** none committed (user-op DDL via life-cli, per the life-map convention; record lands in `_schema_log` and syncs to every replica).

**Interfaces:**
- Produces: tables `person_accounts`, `contact_records`, `person_locations`, `person_employments`, `person_photos`; people columns `circles`, `notes`, `notify_birthday`. Backup file for rollback.

- [ ] **Step 1: Backup** - `life export > ~/Documents/manual-backups/life-data-pre-people-rework-$(date +%Y%m%d).sql` and verify the file is non-trivial (`wc -c` > 100KB).

- [ ] **Step 2: Create tables**

```bash
life table create person_accounts person_id:text platform:text handle:text url:text source_id:text display_name:text active:integer notes:text
life table create contact_records source:text source_id:text handle:text name:text raw:text follows_me:integer i_follow:integer status:text person_id:text suggested_person_id:text first_seen:text last_seen:text
life table create person_locations person_id:text city:text country:text start:text end:text source:text notes:text
life table create person_employments person_id:text company:text title:text start:text end:text source:text notes:text
life table create person_photos person_id:text platform:text r2_key:text sha256:text fetched_at:text notes:text
```

(If `life table create --help` shows a different column syntax, follow the CLI; the column names/types above are the contract.)

- [ ] **Step 3: Add people columns**

```bash
life sql "ALTER TABLE people ADD COLUMN circles TEXT"
life sql "ALTER TABLE people ADD COLUMN notes TEXT"
life sql "ALTER TABLE people ADD COLUMN notify_birthday INTEGER"
```

- [ ] **Step 4: Verify + sync** - `life sql "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'person_%' OR name='contact_records'"` lists all five; `life sync` reports ddl_applied pushed. Column drops happen later (Tasks 5-6), only after their migrations verify.

### Task 4: Notion drift reconcile + notify_birthday migration — INTERACTIVE

**Files:**
- Create: `scripts/notion_people_pull.py` (one-off, run directly)

**Interfaces:**
- Consumes: `lifedata.sql/sq/now_iso`; `NOTION_API_TOKEN` env; People data_source `1a803953-a8af-80ab-824d-000bfe407316`.
- Produces: life-data reconciled with any Notion edits since the 2026-09-01 import; `people.notify_birthday` populated; `data/notion_people_snapshot.json` (gitignored) for later migration tasks.

- [ ] **Step 1: Write the pull script** - paginated raw-HTTP query (httpx) of ALL People pages, saving every page's properties to `data/notion_people_snapshot.json` keyed by dash-stripped page id. Include for each: all property values (flattened: title/rich_text → plain text, select → name, multi_select → list of names, date → start, checkbox → bool, url → url) plus `last_edited_time`.

```python
"""One-off: snapshot the Notion People DB to data/notion_people_snapshot.json."""
import json
import os
import pathlib
import httpx

DS = "1a803953-a8af-80ab-824d-000bfe407316"
H = {"Authorization": f"Bearer {os.environ['NOTION_API_TOKEN']}",
     "Notion-Version": "2026-03-11", "Content-Type": "application/json"}

def flatten(props: dict) -> dict:
    out = {}
    for key, p in props.items():
        t = p["type"]
        if t in ("title", "rich_text"):
            out[key] = "".join(x["plain_text"] for x in p[t]) or None
        elif t == "select":
            out[key] = p[t]["name"] if p[t] else None
        elif t == "multi_select":
            out[key] = [x["name"] for x in p[t]]
        elif t == "date":
            out[key] = p[t]["start"] if p[t] else None
        elif t == "checkbox":
            out[key] = p[t]
        elif t == "url":
            out[key] = p[t]
    return out

pages, cursor = {}, None
with httpx.Client(timeout=30) as c:
    while True:
        body = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
        r = c.post(f"https://api.notion.com/v1/data_sources/{DS}/query", headers=H, json=body)
        r.raise_for_status()
        d = r.json()
        for pg in d["results"]:
            pages[pg["id"].replace("-", "")] = {
                "last_edited_time": pg["last_edited_time"], **flatten(pg["properties"])}
        if not d.get("has_more"):
            break
        cursor = d["next_cursor"]
pathlib.Path("data/notion_people_snapshot.json").write_text(json.dumps(pages, indent=1))
print(f"{len(pages)} pages")
```

- [ ] **Step 2: Run it** - `export NOTION_API_TOKEN=$(op read "op://4eeyrkqibibn7k4j6rz2fbzvxm/nhsh73sfidj4cdowvbaayaq7tq/credential") && uv run python scripts/notion_people_pull.py` → expect ~662 pages.

- [ ] **Step 3: Diff drift (INTERACTIVE)** - for pages with `last_edited_time` > `2026-09-01T00:00:00Z`, compare each mapped field (Name→name, Birthday→birthday, Tags→tags, Company→company, etc. - the import mapping from the life-map people schema) against the life-data row (`lifedata.sql`). Present differences to Alex in chat; apply the ones he confirms via `life sql UPDATE` using `sq()`. Notion-side label-fix work in flight is the expected source.

- [ ] **Step 4: Migrate notify_birthday** - from the snapshot, for every page where `Birthday Notifications` is true: `life sql "UPDATE people SET notify_birthday = 1 WHERE id = '<rowid>' AND deleted_at IS NULL"`. Then set the rest to 0. Verify: count of `notify_birthday = 1` equals the snapshot's true-count; report both numbers to Alex.

- [ ] **Step 5: Sync + commit script** - `life sync`; `git add scripts/notion_people_pull.py && git commit -m "feat: one-off Notion People snapshot script"`.

### Task 5: Handles → person_accounts migration

**Files:**
- Create: `scripts/migrate_accounts.py` (one-off)

**Interfaces:**
- Consumes: `lifedata`; people flat columns.
- Produces: person_accounts rows for every non-empty handle; flat columns dropped from people.

- [ ] **Step 1: Write the migration script**

```python
"""One-off: move flat handle columns into person_accounts, keeping source values verbatim."""
from contact_sync import lifedata

COLS = {  # people column -> (platform, which field the value fills)
    "instagram": ("instagram", "url"),
    "facebook": ("facebook", "url"),
    "snapchat": ("snapchat", "handle"),
    "linkedin_url": ("linkedin", "url"),
    "google_contacts_url": ("google_contacts", "url"),
}

rows = lifedata.sql(
    "SELECT id, instagram, facebook, snapchat, linkedin_url, google_contacts_url "
    "FROM people WHERE deleted_at IS NULL")
out, expected = [], 0
for r in rows:
    for col, (platform, field) in COLS.items():
        val = (r.get(col) or "").strip()
        if not val:
            continue
        expected += 1
        acct = {"id": f"{platform}:{r['id']}", "person_id": r["id"],
                "platform": platform, "handle": None, "url": None,
                "source_id": None, "display_name": None, "active": 1, "notes": None}
        acct[field] = val
        if field == "url" and platform == "instagram":
            acct["handle"] = val.rstrip("/").rsplit("/", 1)[-1] or None
        out.append(acct)
lifedata.insert("person_accounts", out)
got = lifedata.sql("SELECT count(*) AS n FROM person_accounts WHERE deleted_at IS NULL")[0]["n"]
print(f"expected {expected}, inserted total now {got}")
assert got == expected, "count mismatch - do NOT drop columns"
```

- [ ] **Step 2: Rehearse against a copy** - `cp` the live db dir to a scratch dir, run with `LIFE_DATA_DIR=<scratch>` (the CLI honors it per life-cli), confirm the printed counts reconcile (from current data: expect single digits - 0 ig, 1 snap, 1 li, 0 fb, 5 gc as of 2026-09-02, plus any Task 4 drift).

- [ ] **Step 3: Run live** - `uv run python scripts/migrate_accounts.py` → assert passes.

- [ ] **Step 4: Drop the flat columns** (only after Step 3's assert)

```bash
for c in instagram facebook snapchat linkedin_url google_contacts_url; do life sql "ALTER TABLE people DROP COLUMN $c"; done
life sync
```

- [ ] **Step 5: Commit** - `git add scripts/migrate_accounts.py && git commit -m "feat: one-off flat-handle to person_accounts migration"`

### Task 6: Circles union + employments migration — INTERACTIVE

**Files:**
- Create: `scripts/migrate_circles.py` (one-off, two subcommands: `worksheet` and `apply`)

**Interfaces:**
- Consumes: `lifedata`; people `tags`/`company`/`when_we_met`/`employer`/`position_title` columns; `data/circles_decisions.json` (gitignored, produced with Alex).
- Produces: `people.circles` populated; person_employments/person_locations rows; `met_through` person_relations edges; old columns dropped; zero-loss reconciliation report.

- [ ] **Step 1: Write `worksheet` mode** - emits `data/circles_worksheet.json`: every distinct value of `tags` (JSON array elements) ∪ `company` ∪ `when_we_met` with its person-count and source column. Default decision pre-filled for every value: `{"action": "circle", "circle": "<value verbatim>"}`.

```python
"""One-off circles migration. Modes: worksheet | apply.

PRESERVATION INVARIANT: the default for every value is a verbatim circle.
Any other action (merge/rename/split/location/employment/met_through/retire)
exists in the decisions file ONLY because Alex chose it for that value.
"""
import json
import pathlib
import sys
from collections import defaultdict
from contact_sync import lifedata

WS = pathlib.Path("data/circles_worksheet.json")
DEC = pathlib.Path("data/circles_decisions.json")


def load_people():
    return lifedata.sql(
        "SELECT id, tags, company, when_we_met, employer, position_title "
        "FROM people WHERE deleted_at IS NULL")


def values_of(row):
    vals = []
    for t in json.loads(row.get("tags") or "[]"):
        vals.append(("tags", t))
    for col in ("company", "when_we_met"):
        v = (row.get(col) or "").strip()
        if v:
            vals.append((col, v))
    return vals


def worksheet():
    counts = defaultdict(lambda: {"count": 0, "sources": set()})
    for row in load_people():
        for src, v in values_of(row):
            counts[v]["count"] += 1
            counts[v]["sources"].add(src)
    WS.write_text(json.dumps({
        v: {"count": d["count"], "sources": sorted(d["sources"]),
            "decision": {"action": "circle", "circle": v}}
        for v, d in sorted(counts.items(), key=lambda kv: -kv[1]["count"])}, indent=1))
    print(f"{len(counts)} distinct values -> {WS}")


def apply():
    dec = json.loads(DEC.read_text())
    people = load_people()
    unaccounted = set()
    for row in people:
        circles, changed = [], False
        for _, v in values_of(row):
            d = dec.get(v)
            if d is None:
                unaccounted.add(v)
                continue
            a = d["decision"]
            for c in a.get("circles", [a.get("circle")] if a.get("circle") else []):
                if c not in circles:
                    circles.append(c)
            changed = True
        # employments from the employer multi-select + position_title
        emps = json.loads(row.get("employer") or "[]")
        title = (row.get("position_title") or "").strip() or None
        emp_rows = [{"id": f"emp:{row['id']}:{i}", "person_id": row["id"],
                     "company": e, "title": title if i == 0 else None,
                     "start": None, "end": None, "source": "notion_import",
                     "notes": None} for i, e in enumerate(emps)]
        lifedata.insert("person_employments", emp_rows)
        if changed or circles:
            lifedata.sql(
                f"UPDATE people SET circles = {lifedata.sq(json.dumps(circles))} "
                f"WHERE id = '{row['id']}'")
    assert not unaccounted, f"values with NO decision (nothing may be dropped): {sorted(unaccounted)}"
    print("applied; run reconcile checks next")


if __name__ == "__main__":
    {"worksheet": worksheet, "apply": apply}[sys.argv[1]]()
```

- [ ] **Step 2: Generate the worksheet** - `uv run python scripts/migrate_circles.py worksheet` → ~150 company values + ~20 era labels + 8 tags.

- [ ] **Step 3: Mapping session with Alex (INTERACTIVE)** - walk the worksheet in chat, value by value (highest count first, batching only where Alex says "defaults fine for these"). Legal decisions per value, all requiring his explicit word except the default: `circle` (default, verbatim), `circles: [..]` (split/merge - his call), plus **side effects recorded as extra keys** the executor applies manually after `apply`: `met_through: <person name>` (create the person_relations edge; the value still becomes a circle unless Alex says retire), `location: {city, country}` (person_locations row), `employment: {company}` (person_employments row). Save the completed file as `data/circles_decisions.json`. Also settled here: the Boston/London seed rows (Alex Corallo, Marco - find their person ids by name query) and whether `NYC` moves to person_locations.

- [ ] **Step 4: Rehearse `apply` on a db copy** (`LIFE_DATA_DIR` scratch), then reconcile: for every worksheet value, `SELECT count(*) FROM people, json_each(circles) WHERE json_each.value = '<circle it maps to>'` ≥ the old count. Print a per-value table; any shortfall = stop.

- [ ] **Step 5: Run live + apply side effects** - `apply` on the real db; create the `met_through` edges (`life insert person_relations` with `{"person_id": ..., "related_id": ..., "relation_type": "met_through"}`), location rows, employment extras from the decisions file; re-run the reconcile queries live and paste the table to Alex.

- [ ] **Step 6: Drop the absorbed columns** (only after Alex confirms the reconcile table)

```bash
for c in tags company when_we_met employer position_title; do life sql "ALTER TABLE people DROP COLUMN $c"; done
life sync
```

- [ ] **Step 7: Commit** - `git add scripts/migrate_circles.py && git commit -m "feat: one-off circles union + employments migration"` (worksheet/decisions stay gitignored in `data/`).

### Task 7: Ledger upsert core

**Files:**
- Create: `src/contact_sync/ledger.py`
- Test: `tests/test_ledger.py`

**Interfaces:**
- Consumes: `lifedata.sql/insert/sq/now_iso`.
- Produces: `Record` dataclass `(source: str, source_id: str, handle: str|None, name: str|None, raw: dict, follows_me: int|None, i_follow: int|None)` and `upsert(records: list[Record]) -> dict` returning `{"new": int, "updated": int}`. Row id = `f"{source}:{source_id}"`. New rows: `status="pending"`, `first_seen=last_seen=now`. Existing rows: update `handle,name,raw,follows_me,i_follow,last_seen` only - **never** `status`/`person_id`/`suggested_person_id`/`first_seen`.

- [ ] **Step 1: Failing tests** (mock `lifedata`)

```python
import json
from contact_sync.ledger import Record, upsert

def rec(sid="alice123"):
    return Record(source="instagram", source_id=sid, handle=sid, name=None,
                  raw={"value": sid}, follows_me=1, i_follow=None)

def test_new_record_inserted_pending(mocker):
    mocker.patch("contact_sync.lifedata.sql", return_value=[])  # nothing exists
    ins = mocker.patch("contact_sync.lifedata.insert")
    mocker.patch("contact_sync.lifedata.now_iso", return_value="2026-09-02T00:00:00.000Z")
    out = upsert([rec()])
    row = ins.call_args.args[1][0]
    assert row["id"] == "instagram:alice123"
    assert row["status"] == "pending"
    assert row["first_seen"] == row["last_seen"] == "2026-09-02T00:00:00.000Z"
    assert json.loads(row["raw"]) == {"value": "alice123"}
    assert out == {"new": 1, "updated": 0}

def test_existing_record_updates_not_status(mocker):
    sql = mocker.patch("contact_sync.lifedata.sql",
                       return_value=[{"id": "instagram:alice123"}])
    ins = mocker.patch("contact_sync.lifedata.insert")
    upsert([rec()])
    ins.assert_not_called()
    update = sql.call_args.args[0]
    assert "last_seen" in update and "status" not in update and "first_seen" not in update

def test_double_upsert_idempotent_counts(mocker):
    mocker.patch("contact_sync.lifedata.insert")
    mocker.patch("contact_sync.lifedata.sql", side_effect=[
        [], [{"id": "instagram:alice123"}], []])
    assert upsert([rec()]) == {"new": 1, "updated": 0}
    assert upsert([rec()]) == {"new": 0, "updated": 1}
```

- [ ] **Step 2: Verify fail** - `uv run pytest tests/test_ledger.py -v` → FAIL.

- [ ] **Step 3: Implement**

```python
"""contact_records resolution ledger - idempotent upserts."""
import json
from dataclasses import dataclass
from contact_sync import lifedata


@dataclass
class Record:
    source: str
    source_id: str
    handle: str | None
    name: str | None
    raw: dict
    follows_me: int | None = None
    i_follow: int | None = None

    @property
    def row_id(self) -> str:
        return f"{self.source}:{self.source_id}"


def _int_sql(v: int | None) -> str:
    return "NULL" if v is None else str(v)


def upsert(records: list[Record]) -> dict:
    if not records:
        return {"new": 0, "updated": 0}
    ids = ",".join(lifedata.sq(r.row_id) for r in records)
    existing = {row["id"] for row in lifedata.sql(
        f"SELECT id FROM contact_records WHERE id IN ({ids})")}
    now = lifedata.now_iso()
    new_rows = []
    updated = 0
    for r in records:
        if r.row_id in existing:
            lifedata.sql(
                "UPDATE contact_records SET "
                f"handle = {lifedata.sq(r.handle)}, name = {lifedata.sq(r.name)}, "
                f"raw = {lifedata.sq(json.dumps(r.raw))}, "
                f"follows_me = {_int_sql(r.follows_me)}, i_follow = {_int_sql(r.i_follow)}, "
                f"last_seen = {lifedata.sq(now)} "
                f"WHERE id = {lifedata.sq(r.row_id)}")
            updated += 1
        else:
            new_rows.append({
                "id": r.row_id, "source": r.source, "source_id": r.source_id,
                "handle": r.handle, "name": r.name, "raw": json.dumps(r.raw),
                "follows_me": r.follows_me, "i_follow": r.i_follow,
                "status": "pending", "person_id": None, "suggested_person_id": None,
                "first_seen": now, "last_seen": now})
    lifedata.insert("contact_records", new_rows)
    return {"new": len(new_rows), "updated": updated}
```

- [ ] **Step 4: Verify pass**, mutation-check (make `upsert` always insert; `test_existing_record_updates_not_status` must fail; restore).

- [ ] **Step 5: Commit** - `git commit -m "feat: contact_records ledger upsert"` (with both files added).

### Task 8: Export parsers - Instagram, Facebook, Snapchat, LinkedIn

**Files:**
- Create: `src/contact_sync/parsers.py`
- Test: `tests/test_parsers.py`, `tests/fixtures/` (synthetic files: `ig_followers.json`, `ig_following.json`, `fb_friends.json`, `snap_friends.json`, `linkedin_connections.csv`)

**Interfaces:**
- Consumes: `ledger.Record`.
- Produces: `parse_instagram(followers_path, following_path) -> list[Record]` (source `instagram`, source_id = lowercase username, one Record per username with `follows_me`/`i_follow` set from list membership); `parse_facebook(path) -> list[Record]` (source `facebook`, source_id = lowercase name with spaces→`_` - FB exports carry names only; `follows_me=i_follow=1`); `parse_snapchat(path) -> list[Record]` (source `snapchat`, source_id = lowercase username, name = display name); `parse_linkedin(path) -> list[Record]` (source `linkedin`, source_id = profile-URL slug, name = "First Last", raw includes company/position for the matcher and employments).

- [ ] **Step 1: Inspect the REAL exports first** (formats may differ from any assumption - the old parsers are presumed wrong):

```bash
jq '.[0]' data/instagram/followers.json; jq 'keys, .relationships_following[0]' data/instagram/following.json
jq 'keys, (.friends_v2 // .)[0]' data/facebook/your_friends.json
head -5 data/linkedin/Complete_LinkedInDataExport_06-23-2025/Connections.csv
jq 'keys' data/snapchat/friends.json  # MISSING as of 2026-09-02 - if absent, write the parser from Snapchat's documented shape {"friends": [{"Username", "Display Name", ...}]} and mark it needs-real-file validation in the run report
```

Record the actual shapes in a comment atop `parsers.py`. Adjust the interfaces above ONLY if a real file contradicts them (e.g. LinkedIn's first 3 lines are a notes preamble - skip until the header row `First Name,...`).

- [ ] **Step 2: Write synthetic fixtures** mirroring the real shapes exactly, with fake data only (e.g. username `testuser_a`, name `Test Person`). One entry per edge case seen in the real file: IG entry with `string_list_data`, FB `friends_v2` entry with `name` + `timestamp`, LinkedIn row with empty Email, Snapchat entry with display name differing from username.

- [ ] **Step 3: Failing tests** - one per parser asserting: record count, source/source_id/handle/name mapping, follows flags (IG user present in both files → `follows_me=1, i_follow=1`; only followers → `i_follow=None` or 0 per your Step 1 finding - pick one and test it), LinkedIn preamble skipped, raw preserves the source dict verbatim.

```python
from contact_sync import parsers

FIX = "tests/fixtures"

def test_instagram_merges_lists():
    recs = {r.source_id: r for r in parsers.parse_instagram(
        f"{FIX}/ig_followers.json", f"{FIX}/ig_following.json")}
    both = recs["testuser_a"]          # in both fixture files
    assert both.follows_me == 1 and both.i_follow == 1
    only_follower = recs["testuser_b"] # only in followers fixture
    assert only_follower.follows_me == 1 and only_follower.i_follow == 0

def test_linkedin_skips_preamble_and_slugs():
    recs = parsers.parse_linkedin(f"{FIX}/linkedin_connections.csv")
    assert recs[0].source == "linkedin"
    assert recs[0].source_id == "test-person-123"   # from fixture URL slug
    assert recs[0].name == "Test Person"
    assert recs[0].raw["Company"] == "TestCo"
```

(plus equivalent tests for facebook and snapchat - written against the fixture contents from Step 2.)

- [ ] **Step 4: Verify fail, implement, verify pass** - implementation reads with `json`/`csv` stdlib only; every parser lowercases source_ids; malformed entries (missing username) are skipped with a `structlog` warning, never crash.

- [ ] **Step 5: Smoke against the REAL files** - `uv run python -c` one-liner per parser printing `len()` on the real June-2025 exports: expect IG ≈ 1162 followers ∪ 1584 following merged, FB ≈ 375, LinkedIn ≈ 690. Mismatch by >5% = investigate before continuing.

- [ ] **Step 6: Commit** - `git add src/contact_sync/parsers.py tests/test_parsers.py tests/fixtures && git commit -m "feat: export parsers (instagram, facebook, snapchat, linkedin)"`

### Task 9: Google + Apple source ingests

**Files:**
- Create: `src/contact_sync/sources.py`
- Test: `tests/test_sources.py`

**Interfaces:**
- Consumes: `ledger.Record`; the `gog` CLI; local Contacts (per the apple-contacts skill's export mechanics - READ THAT SKILL before this task).
- Produces: `fetch_google() -> list[Record]` (source `google_contacts`, source_id = People API `resourceName` like `people/c123`, raw includes names, labels/memberships, org, birthday, photo url); `fetch_apple() -> list[Record]` (source `apple_contacts`, source_id = the stable contact identifier from the export, raw includes name, org, birthday, phone/email presence booleans - NOT the values; the spec's boundary keeps them out of life-data).

- [ ] **Step 1: Discover the real commands** - run `gog contacts list --help` (and the gog skill) to get the JSON listing invocation incl. label/membership fields; consult the apple-contacts skill for the bulk-export command. Record both exact commands in a comment atop `sources.py`. **Known hazard: a first gog contacts call hung for 2min on 2026-09-02** - if it hangs, check `gog auth` state per the gog skill before assuming the API shape is wrong.

- [ ] **Step 2: Failing tests** - mock `subprocess.run` with captured-shape sample JSON (synthetic values), assert Record mapping: resourceName → source_id, display name → name, memberships list lands in raw as `labels`, org fields land in raw as `org`, apple phone/email become `has_phone`/`has_email` booleans in raw with no values copied.

- [ ] **Step 3: Implement** - both functions shell out (same `_run` pattern as lifedata), parse JSON, map to Records. `follows_me`/`i_follow` stay None (not meaningful).

- [ ] **Step 4: Verify pass; live smoke** - `fetch_google()` on the real account: expect hundreds of records, spot-check one known contact's labels with Alex. `fetch_apple()` likewise (may require a TCC grant - if Contacts access prompts, that's the documented manual step; add it to README in Task 14).

- [ ] **Step 5: Commit** - `git commit -m "feat: google + apple contact ingests"` (files added).

### Task 10: Matcher

**Files:**
- Create: `src/contact_sync/match.py`
- Test: `tests/test_match.py`

**Interfaces:**
- Consumes: `lifedata`, ledger rows (`status='pending'`), people rows.
- Produces: `normalize(s: str) -> str` (casefold, NFKD-strip accents, drop non-letters, collapse spaces); `letters(s: str) -> str` (normalize then remove spaces); `run_match() -> dict` returning `{"auto": int, "suggested": int, "left_pending": int}`. Auto-link rule (all must hold): exactly one person's variant set matches the record exactly, exactly one pending record matches that person, the matched person name has ≥2 words. Auto-link writes: `contact_records.status='matched'`, `person_id`; plus a `person_accounts` row (`id=f"{platform}:{person_id}:{handle or source_id}"`, active=1, url from raw where present). Handle-only sources (instagram) compare `letters(handle)` against `letters(variant)`. Single fuzzy candidate → `suggested_person_id` only, stays pending.

- [ ] **Step 1: Failing tests**

```python
from contact_sync.match import normalize, letters

def test_normalize():
    assert normalize("  José  O'Brien-2 ") == "jose obrien"
    assert letters("José O'Brien") == "joseobrien"

def test_single_word_never_automatches(mocker):
    # person "Madonna" + record name "Madonna" -> suggested, not matched
    ...

def test_ambiguous_two_people_stays_pending(mocker):
    # two people normalize to "test person" -> no auto, no suggestion
    ...

def test_exact_unique_automatch_writes_account(mocker):
    # one person "Test Person", one linkedin record "Test Person"
    # -> status matched + person_accounts insert with url from raw
    ...
```

(Write the three `...` bodies out fully with mocked `lifedata.sql`/`insert` - the mock returns fixed people/pending lists and the assertions inspect the UPDATE strings and insert payloads, same style as Task 7's tests.)

- [ ] **Step 2: Verify fail, implement, verify pass.** Implementation pulls people variants once (`name`, `first_name + last_name`, `nickname + last_name` - each normalized), builds dicts both directions, applies the rules. No fuzzy libraries - exact normalized equality and letters-equality only (YAGNI; Claude handles the fuzzy tail in triage).

- [ ] **Step 3: Mutation-check** - disable the ≥2-words guard, confirm `test_single_word_never_automatches` fails, restore.

- [ ] **Step 4: Commit** - `git commit -m "feat: exact-match auto-linker with suggestions"` (files added).

### Task 11: Photos

**Files:**
- Create: `src/contact_sync/photos.py`
- Test: `tests/test_photos.py`
- Modify: `.env.tpl` (add R2 credential refs - names, not IDs: it's a bootstrap manifest)

**Interfaces:**
- Consumes: `lifedata`; env `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`; boto3 S3 client against `https://<account>.r2.cloudflarestorage.com`, bucket `life-data-archive`.
- Produces: `store_photo(person_id: str, platform: str, image: bytes, ext: str) -> str | None` - sha256 the bytes; if a person_photos row with that sha exists, return None (dedupe); else upload to `photos/people/<person_id>/<platform>-<sha[:8]>.<ext>`, insert the row, return the key. `fetch_google_photo(resource_name) -> bytes | None` and `fetch_apple_photo(source_id) -> bytes | None` using the same commands discovered in Task 9.

- [ ] **Step 1: Locate the R2 credential** - `op item list --vault 4eeyrkqibibn7k4j6rz2fbzvxm --format json | jq -r '.[].title'` and find the life-data R2 item (the places/archive imports used one). If none exists, STOP and ask Alex to mint one scoped to `life-data-archive`. Add the three refs to `.env.tpl` by item NAME.

- [ ] **Step 2: Failing tests** - mock boto3 client + lifedata: duplicate sha → no upload, no insert, returns None; new sha → `put_object` called with exact key `photos/people/p1/instagram-<sha8>.jpg`, row inserted with `fetched_at` = now_iso.

- [ ] **Step 3: Implement, verify pass.**

- [ ] **Step 4: Live smoke** - `op run --env-file=.env.tpl -- uv run python -c "..."` storing one real Google contact photo for a person Alex names; verify with a `life sql` select and an R2 HEAD via the same client. Then delete nothing - it's the first real row.

- [ ] **Step 5: Commit** - `git commit -m "feat: R2-backed person photo store with sha dedupe"` (files added).

### Task 12: CLI entrypoint + new-person helper

**Files:**
- Create: `src/contact_sync/cli.py`, `src/contact_sync/notion_people.py`, `src/contact_sync/__main__.py`
- Test: `tests/test_cli.py`, `tests/test_notion_people.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `uv run python -m contact_sync <cmd>`:
  - `ingest instagram|facebook|snapchat|linkedin --path <file-or-dir>` / `ingest google` / `ingest apple` → parser/source → `ledger.upsert`, prints the `{"new","updated"}` JSON
  - `match` → `run_match()`, prints its JSON
  - `queue` → prints pending records as JSON (id, source, handle, name, suggested_person_id, suggested person's name) for Claude's triage
  - `new-person --name "<Name>"` → `notion_people.create_stub(name) -> page_id` (raw httpx POST to `/v1/pages`, parent = People data source, title only), then `life insert people` one row with `id` = dash-stripped page id and `name`; prints the id. **This is the row-id invariant made executable.**
- Tests: argparse dispatch (mock the underlying functions, assert called with parsed args); `create_stub` mocked-httpx test asserting the POST body and the dash-strip.

- [ ] **Step 1: Failing tests → Step 2: implement (argparse, ~60 lines) → Step 3: pass → Step 4: commit** `git commit -m "feat: contact_sync CLI (ingest/match/queue/new-person)"` (files added). Follow the exact TDD loop of Tasks 7-10; every subcommand's happy path has a test.

### Task 13: contacts-review skill + Google write-back — INTERACTIVE finish

**Files:**
- Create: `~/.config/agent-config/skills/contacts-review/SKILL.md` (private - it references Alex's estate)
- Create: `scripts/google_cleanup.py` (in this repo)

**Interfaces:**
- Consumes: the CLI from Task 12; gog write commands (`gog contacts update` - verify exact syntax against the gog skill first).
- Produces: the runbook skill; a verify-then-clear cleanup script.

- [ ] **Step 1: Write `google_cleanup.py`** - for each google_contacts contact_record with status `matched`: read the matched person's life-data row; **only if** every Google label already appears in `circles` AND the org (if any) appears in `person_employments` for that person, clear the label memberships and org fields on the Google contact via gog; otherwise print the gap and skip. `--dry-run` first is mandatory and the default; `--apply` flips it. No test file (one-off script), but the verify-before-clear branch gets an inline `assert`-based self-check with stub data at the bottom under `if __name__ == "__main__" and "--selftest" in sys.argv`.

- [ ] **Step 2: Write the skill.** SKILL.md frontmatter description triggers on "contacts review", "triage my contacts/followers", "sync my people". Body sections, in run order:
  1. Per-platform export refresh click-ops (moved from this repo's old README, verified current during Task 8's real-file inspection); stale/missing export = skip source, never block.
  2. The exact ingest/match/queue commands.
  3. Triage conventions: work `queue` output grouped suggested-first; per item the three verbs (match → `UPDATE contact_records SET status='matched', person_id=... ` + `person_accounts` row; new → `new-person` then match; ignore → `status='ignored'`); circles vocabulary is governed here - new circle names need Alex's word, renames are estate-wide UPDATEs.
  4. Apple→Google port procedure (create in Google via gog from the Apple record, link both accounts).
  5. Google boundary + `google_cleanup.py --dry-run` then `--apply` with Alex watching.
  6. Photos step, quality sweep queries (circle-less people, `notify_birthday=1` missing birthday, close-circle members missing birthdays), `life sync`, run report format.
- [ ] **Step 3: Dry-run the skill top-to-bottom yourself** (no Alex): every command in it must execute against the real estate without error (using `--dry-run`/read-only paths). Fix drift.

- [ ] **Step 4: Commit** both repos - this repo: `git commit -m "feat: google write-back cleanup script"`; agent-config gets the skill committed per its own conventions.

### Task 14: First real contacts-review run (E2E) — INTERACTIVE

**Files:** none (this is the E2E test); README/AGENTS updated after.

- [ ] **Step 1: Alex refreshes exports** - fresh IG/FB/Snap/LinkedIn downloads per the skill's procedures (Snapchat especially - none exists on disk). Whatever isn't refreshed gets skipped, per the skill.
- [ ] **Step 2: Run the full skill flow live** - ingests (all sources), match, triage session, Apple→Google port, cleanup dry-run→apply, photos, sweep, sync. This is the acceptance test for the whole project; every defect found is fixed with a test before the run is called done.
- [ ] **Step 3: Verify the birthday payoff** - `life sql "SELECT count(*) FROM people WHERE deleted_at IS NULL AND birthday IS NOT NULL"` - report before (24) vs after to Alex.
- [ ] **Step 4: Rewrite README.md + AGENTS.md fully** (current state only: what the repo is, layout, commands, manual TCC/export steps, .env.tpl notes) and commit: `git commit -m "docs: rewrite README/AGENTS for contact-sync"`.

### Task 15: Estate docs, task consolidation, memory — INTERACTIVE

**Files:**
- Modify: `~/.claude/skills/life-map/SKILL.md`, `~/.claude/skills/notion-workspace/SKILL.md` (+ its references/workspace-map.md People section), `~/.claude/skills/projects/SKILL.md`
- Create: memory file `project_contact_sync.md` (+ MEMORY.md line)

- [ ] **Step 1: life-map** - replace the people section (new schema, counts from live queries, conventions: id invariant, circles governance, ledger semantics) and add person_accounts/contact_records/person_locations/person_employments/person_photos sections + the friend_locations stream contract under a "designed, not yet live" note. Bump last-verified.
- [ ] **Step 2: notion-workspace** - People DB entry: frozen relation-anchor, stub-page convention, notify_birthday superseded note. Remove stale claims (e.g. tags-absorb note).
- [ ] **Step 3: projects skill** - rename row to contact-sync, update description/path.
- [ ] **Step 4: Notion task consolidation with Alex** - walk the 17 project tasks; propose per-task: absorbed-by-design → Completed, superseded → Completed with a one-line Notes pointer, contradicted (mute-status realtime sync) → Cancel?, still-open content work (Empire note import, WhatsApp pictures) → keep. **Alex confirms each status change; none are made unilaterally.** Also update the Contact Sync project Notes page to point at the spec.
- [ ] **Step 5: Memory** - write `project_contact_sync.md` (shipped state, birthday-reminders migration still pending with spec-section pointer, Snapchat/WhatsApp caveats) + index line; update `project_birthday_reminders.md` to note the pending life-data repoint guidance.

---

## Self-Review (performed)

- **Spec coverage:** schema (T3), migration steps 1-7 (T4-T6 + backup in T3), parsers/rewrite-from-scratch (T8), google/apple (T9), matcher rules (T10), photos/R2/sha (T11), CLI + row-id invariant (T12), skill + google boundary + write-back (T13), E2E + docs rewrite (T14), estate docs/tasks/memory (T15), repo rename (T1). birthday-reminders guidance: spec-only by design, no task. Find My scraper: future project, contract already in spec + Notion task.
- **Type consistency:** `Record` fields and `row_id` match between ledger (T7), parsers (T8), sources (T9); `lifedata` signatures used identically in T4-T12.
- **Placeholders:** Task 10's test stubs are filled per its Step 1 instruction; no TBDs remain.
