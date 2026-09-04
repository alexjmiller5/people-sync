# AGENTS.md

Python CLI that consolidates contact sources (Instagram, Facebook, Snapchat,
LinkedIn, Google Contacts, Apple Contacts) into the life-data people estate.
No daemon, no cron: it is run ad hoc, roughly monthly, by an agent working
through the `contacts-review` skill. That skill is the runbook (procedures,
triage, sweeps); this file is how to work on the code.

## Layout

```
src/contact_sync/
  cli.py           argparse surface: ingest / match / queue / new-person / photos store
  lifedata.py      the ONLY life-data write path (shells out to the `life` CLI)
  ledger.py        contact_records upserts, keyed <source>:<source_id>
  parsers.py       instagram / facebook / snapchat / linkedin export parsers
  sources.py       google (via gog) and apple (local AddressBook sqlite) ingests
  match.py         conservative auto-linker
  photos.py        R2 profile-photo storage, sha256-deduped, plus per-platform fetchers
  notion_people.py Notion People stub-page creation (the row-id invariant)
tests/             pytest, synthetic fixtures only
scripts/           reconcile.py (triage link/merge/create), one-off migrations,
                   the Google write-back cleanup
docs/superpowers/  design spec and plan
data/              contact exports, gitignored, never committed
```

## Write path

**Every life-data write goes through `lifedata.py`, which shells out to the
`life` CLI. Never open `life.db` with sqlite directly** - the hub's sync
depends on the CLI's bookkeeping, and a raw write is invisible to it. Soft
deletes only (`SET deleted_at = updated_at`); a hard delete is resurrected by
the next sync.

`lifedata.sq()` quotes every value interpolated into SQL. Use it; do not
f-string a raw value into a query.

Birthdays are `YYYY-MM-DD`. A source that gives a month and day but no year
is stored ISO 8601 style as `--MM-DD`; filter with
`substr(birthday, -5)` when matching a month/day.

`people.id` is always a Notion People page id with the dashes stripped. New
people therefore get their Notion stub page first and the row second, which is
exactly what `new-person` does - never insert a `people` row with an invented
id.

## Triage reconcile (`scripts/reconcile.py`)

The three moves a `contacts-review` triage session repeats: `link` a pending
Google record onto an existing person, `merge` two people rows, `create` a
person from a Google record. Dry run is the default and prints every statement;
`--apply` executes.

They are LOSSLESS by construction, which is the property to preserve when
editing them: an existing life-data value is never overwritten. A conflicting
Google name part is appended to `notes` (`google_last_name: ...`), a replaced
name survives in `nickname` or as `aka: ...`, a conflicting birthday is printed
as `CONFLICT birthday` and dropped, circles are only ever unioned, and a merge
appends every conflicting loser scalar as `merged from ...`. Label and org
strings become circles verbatim (`CIRCLE_ALIASES` holds the one exception).

`merge` never writes to Notion. It queries the People-related Notion DBs
(`NOTION_PEOPLE_RELATIONS`) for pages still pointing at the loser page and
prints them for a manual re-point; without `NOTION_API_TOKEN` it warns and
skips that check.

Scripts are importable by their bare module name (`pyproject`'s pytest
`pythonpath` and ruff `src` both include `scripts`), which is what lets
`reconcile.py` reuse `google_cleanup.user_groups` and lets tests import it.

## Platform vocabulary

`instagram`, `facebook`, `snapchat`, `linkedin`, `google_contacts`,
`apple_contacts`, `whatsapp`, `venmo`, `partiful`, `spotify`. The set is a
convention, not an enum, so nothing validates it - a typo is silent and
fragments every later query.

`person_accounts.platform` and `person_photos.platform` must agree: photos
join back to accounts on `(person_id, platform)`. Note that the ledger's
`contact_records.source` uses the same vocabulary, so `match.py` can copy it
straight across into `person_accounts.platform`.

Before adding a value, read sibling rows (`SELECT DISTINCT platform ...`) and
reuse an existing one verbatim.

## Privacy

The repo is public-grade: no personal data in code, tests, fixtures, docs, or
commit messages. Concretely:

- Fixtures under `tests/fixtures/` are fully synthetic. Never sanitize a real
  export into a fixture; write a new one with invented names and handles.
- **Warnings log source, index, and reason only** - never the entry's
  contents. `log.warning("skipping malformed entry", source=..., index=i,
  reason=...)` is the shape; adding the name or handle leaks a person into the
  logs.
- Emails, phone numbers, and addresses are NEVER copied into life-data. The
  Apple query selects presence counts (`phone_count`, `email_count`), not
  values, and the Google raw dict keeps names, memberships, organizations,
  birthdays, and the photo url only. Both keep the platform's `source_id`, so
  a contact detail is resolved on demand through `gog` or Contacts instead of
  being duplicated.

That PII boundary is the reason `sources.py` builds a curated `raw` dict for
Google and Apple rather than storing the API response verbatim. If a new field
is needed, add it to that dict deliberately - do not widen it to the whole
response.

## Matcher contract

`match.py` errs toward leaving records pending, because a wrong auto-link
silently corrupts the contact graph while a missed one just lands in triage.
The rules, all of which have tests:

- Exact normalized equality only. No fuzzy matching, no edit distance.
  Normalization is casefold, strip accents, drop non-letters. Instagram
  matches on the handle's letters (spaces removed), every other source on the
  record's name.
- A record is considered only when exactly one person resolves from it.
- Uniqueness is enforced per `(person, source)`: two pending records from the
  SAME source resolving to one person is ambiguous and touches neither. The
  same person appearing in google AND apple is confirmation, not ambiguity.
- A person whose name normalizes to fewer than 2 words is never auto-matched;
  it becomes a `suggested_person_id` for triage instead.

Loosening any of these needs a test proving the new case and the old
never-auto-match cases still hold.

## TDD and tests

Tests first, always. `just test` (pytest), `just check` (ruff check + format
check, read-only), `just fmt` (ruff format + fix). All three must be clean
before a commit.

External effects are mocked: `lifedata.sql` / `lifedata.insert`, `httpx`, and
`subprocess`-backed helpers (`sources._run`). Nothing in the suite touches the
real estate, the network, or the address book. Mutation-test what you write:
break the field mapping, confirm the test fails.

`cli.py` looks parser and source functions up as module attributes at call
time rather than binding them at import, so tests can patch them - keep it
that way.

## Gotchas

- **`ModuleNotFoundError: No module named 'contact_sync'`** means iCloud
  stamped the venv's editable `.pth` file hidden (Python 3.13+ ignores hidden
  `.pth`). `chflags nohidden` fixes it for one command at best - iCloud
  re-hides the file within seconds. The reliable form is to bypass the `.pth`:
  `PYTHONPATH=src uv run python -m contact_sync ...`. `uv run --with .` is NOT
  a workaround - it can serve a stale cached wheel. `just test` is already
  immune: pyproject sets `pythonpath = ["src"]` for pytest.
- **Evicted iCloud files hang git and read as empty.** Re-materialize the
  working tree first:
  `find . -path ./.venv -prune -o -type f -print0 | xargs -0 cat > /dev/null`.
  A parser reporting 0 entries from a file that clearly has content is the
  same problem in `data/`.
- Facebook exports carry names only, so the record id is derived from the
  display name: two friends sharing a name collapse into one ledger row and
  one of them never reaches triage. `ledger.upsert` warns when it happens.
- LinkedIn's export includes blank rows with only a `Connected On` date for
  connections it cannot render. They are skipped with a warning; that is
  LinkedIn's data loss, not a parser bug.

`.superpowers/` is agent scratch (plans, task briefs, run reports) and is
gitignored - it holds personal data and never gets committed.
