# AGENTS.md

Python CLI that consolidates contact sources (Instagram, Facebook, Snapchat,
LinkedIn, Google Contacts, Apple Contacts) into the life-data people estate.
No daemon, no cron: it is run ad hoc, roughly monthly, by an agent working
through the `people-review` skill. That skill is the runbook (procedures,
triage, sweeps); this file is how to work on the code.

## Layout

```
src/people_sync/
  cli.py           argparse surface: ingest / match / queue / new-person / scrape /
                   login / photos store
  lifedata.py      the ONLY life-data write path (shells out to the `life` CLI)
  ledger.py        people_sync_records upserts, keyed <source>:<source_id>
  parsers.py       instagram / facebook / snapchat / linkedin export parsers
  sources.py       google (via gog) and apple (local AddressBook sqlite) ingests
  match.py         conservative auto-linker
  photos.py        R2 profile-photo storage, sha256-deduped, plus per-platform fetchers
  notion_people.py Notion People stub-page creation (the row-id invariant)
  scrape/          CDP harness (cdp.py), human pacing (pace.py), the scrape loop
                   (run.py), per-platform extractors, and the login flow
                   (login.py) with its selector table (login_specs.py)
tests/             pytest, synthetic fixtures only
scripts/           reconcile.py (triage link/merge/create), one-off migrations,
                   the Google write-back cleanup
docs/superpowers/  design spec and plan
data/              contact exports, gitignored, never committed
flake.nix          packages.default (the CLI)
```

## Installing on a Mac

`flake.nix` exposes `packages.<system>.default`: the `people-sync` CLI,
built with plain `buildPythonApplication` (all three runtime deps ship as
nixpkgs `python313Packages`, so no uv2nix machinery is needed). A host
flake adds it to its packages; the operator's shell supplies the
environment the browser-driving commands read (see "Browser and login
configuration" in the README): `PEOPLE_SYNC_ENDPOINT` for the Chrome to
attach to, and the credential / code commands for `login`.

There is deliberately no daemon, launchd agent, or schedule in this repo:
scraping is ad hoc, driven by an agent with a person in the loop (the
`people-review` skill), because every run needs judgment - which account is
whose, and what to do when a site changes its markup. Do not add a
scheduler.

`nix build .#default` and `nix flake check` both need to stay clean.

## Logins

`people-sync login <platform>` signs that platform's dedicated Chrome
profile in, over CDP, at human pace: trusted per-key events (real `code`,
Shift modifier, `Input.insertText` only for a character no US key produces)
with 80-200 ms jitter, 300-900 ms pauses between fields, a mouse move then a
trusted click at an element's center. It is idempotent - an already signed-in
profile is detected and left alone before anything is typed, which is the
ordering to preserve when editing `_sign_in`.

**Never type into a field without proving it is the right one.** `_type_into`
is the only path that types, and it clicks, asserts the element IS
`document.activeElement` (never merely contains it), clears (select-all +
delete, then one Backspace per character where select-all is not a shortcut)
and verifies the value is empty before the first keystroke. Every element
predicate is built by `cdp.element_js`, which binds the FIRST VISIBLE match
of the selector - a real box plus `checkVisibility` with the opacity and
visibility options - and misses otherwise. Google's identifier page ships a
display:none password input ahead of the visible one: a bare `querySelector`
check matched it, so the click landed at (0,0) and the password was typed
into the visible email field and submitted. A prefilled field (LinkedIn)
that is typed into rather than cleared makes attempt 2 send a concatenated
value and burns the one retry. The predicates are JavaScript strings, so
`tests/test_dom_js.py` executes them in node against a stub DOM - the fake
site elsewhere only answers them by substring; keep both in step when a
predicate changes.

The signed-in detector is always `wait_for`, never a single `eval`:
client-rendered nav paints late, and one early sample reads a signed-in
profile as signed out.

Credentials never live in this repo or its config. Three commands supply
them, each run as `sh -c "<command>" people-sync-login <platform>` (so the
platform is `$1`), with a 60 s timeout; a non-zero exit or timeout halts the
login (screenshot, reason naming only the variable):

- `PEOPLE_SYNC_CREDENTIAL_COMMAND` - prints `{"username": ..., "password": ..., "totp": ...}`; `totp` is the current code or null
- `PEOPLE_SYNC_EMAIL_CODE_COMMAND` - prints the newest one-time code from email that arrived after `$PEOPLE_SYNC_CODE_AFTER` (ISO-8601 UTC: when the credentials were submitted; a reader that ignores it can hand back last attempt's code), or nothing if none has yet (polled every 5-10 s for 90 s)
- `PEOPLE_SYNC_SMS_CODE_COMMAND` - same, from SMS on the machine running the job

`PEOPLE_SYNC_CDP_APPROVE_COMMAND` (or `--approve-command`) is different in
kind: a command that approves the browser's remote-debugging prompt on hosts
that show one. `Browser.connect` starts it detached right before the
websocket upgrade (which is what raises the prompt) and only on the
approval-mode path; an explicit `--endpoint` is a dedicated profile with no
prompt, so the command is never run there.

The product never knows what those commands do, and must not learn: no
credential store, no vault, no path from any machine belongs in this code.
Command output is a secret - it is used and dropped, never logged or written
to the state dir.

The 2FA dispatcher picks the field the site actually shows (TOTP, then
email, then SMS) and skips a kind it has no source for, so a site sharing
one input across kinds still works. Email/SMS codes are polled for up to
90 s with 5-10 s gaps.

Halt rules, all of which have tests:

- A captcha, a "confirm on your phone" prompt, an unusual-login
  interstitial, a page with no login form, or an unrecognized page after
  submit: screenshot to `<state dir>/login-<platform>-<ts>.png`, one
  `("login halted", platform=..., reason=...)` line, exit non-zero. Never
  guess at an unknown page.
- A password the site did not accept is retried exactly once
  (`MAX_PASSWORD_ATTEMPTS = 2`). There is never a third attempt - a lockout
  costs far more than a skipped run.
- ANY exception takes the halt path, not a traceback: only the exception
  type name is reported, because a message can carry a command line
  (`subprocess.TimeoutExpired` embeds the full argv) or page content. A code
  command that exits non-zero halts naming its env var rather than polling
  for 90 s.
- Logs carry platform, step, and reason only. Never a username, never a
  code, never page text - there is a test asserting no secret reaches the
  captured log output.

Challenge phrases live in `pace.py`: `CHALLENGE_MARKERS` is shared, and
`LOGIN_MARKERS` ("log in", "login") is the scraper-only half that the login
flow deliberately excludes. Phrases only, never bare nouns - a bare
"captcha" matches the reCAPTCHA badge that sits on ordinary login pages and
halted every run before it started.

Selectors are the one thing expected to drift: they all live in
`login_specs.py`, one entry per platform, and a stale one halts (loudly)
rather than typing into the wrong field.

## Write path

**`people_sync_records` and `people_sync_profiles` are this project's own
tables, not the `people` table.** The `people_sync_` prefix is the project
name (People Sync): `people_sync_records` is the ingest/resolution ledger and
`people_sync_profiles` the scraped-profile cache. `people` is the life-data
person table they resolve INTO, keyed by Notion page id. Never read one
expecting the other, and note that `FROM people` is a prefix of
`FROM people_sync_records` - match table names on a word boundary.

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

The three moves a `people-review` triage session repeats: `link` a pending
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
`people_sync_records.source` uses the same vocabulary, so `match.py` can copy it
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

- **`ModuleNotFoundError: No module named 'people_sync'`** means iCloud
  stamped the venv's editable `.pth` file hidden (Python 3.13+ ignores hidden
  `.pth`). `chflags nohidden` fixes it for one command at best - iCloud
  re-hides the file within seconds. The reliable form is to bypass the `.pth`:
  `PYTHONPATH=src uv run python -m people_sync ...`. `uv run --with .` is NOT
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
