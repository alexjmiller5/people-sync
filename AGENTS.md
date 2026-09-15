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
  photos.py        Life Data profile-photo storage, sha256-deduped, plus per-platform fetchers
  notion_people.py new person ids: a Notion stub page when configured, else local
  scrape/          CDP harness (cdp.py), human pacing (pace.py), the scrape loop
                   (run.py), per-platform extractors, and the login flow
                   (login.py) with its selector table (login_specs.py)
tests/             pytest, synthetic fixtures only
  reconcile.py     triage link/merge/create (`people-sync reconcile`)
  google_cleanup.py Google write-back cleanup (`people-sync google-cleanup`)
  review.py + review.html  the private review page (`people-sync review`)
  whatsapp.py      metadata-snapshot ingest
skills/people-sync/ the generic agent runbook, shipped with the package
docs/superpowers/  design spec and plan
data/              contact exports, gitignored, never committed
flake.nix          packages.default (the CLI)
```

## Installing on a Mac

`flake.nix` also exports `homeModules.default` (`programs.people-sync`): it
installs the CLI wrapped with the operator's settings (CDP endpoint,
credential/code commands, Notion data-source ids) and exposes
`skills/people-sync` at `$XDG_DATA_HOME/people-sync/skills/people-sync`. Those
settings are the only things a user's config supplies; anything else an
operator has to hand-write in their config is a missing option here. The
Notion People data-source id and the People-related databases are user facts
and come from `PEOPLE_SYNC_NOTION_PEOPLE_DS` / `PEOPLE_SYNC_NOTION_RELATIONS`,
never from code.

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

## Extractors

Venmo reads only selected fields of `pageProps.otherUser` on personal profiles.
Never archive the whole Next.js state or capture its network responses: those
also contain credentials and payment data. `currentUser` describes the signed-in
operator, not the person being visited. The social-data export can omit friends
entirely; neither payment activity nor a displayed friend count is an inventory.

One module per platform under `scrape/` with the same surface, driven by
`scrape/run.py`: `URL` (`{handle}` template), `CAPTURE` (response URL
patterns to keep while the page loads), optional `READY_JS` (a predicate
the loop waits for before extracting - client-rendered profiles paint after
the load event), `EXTRACTOR_JS` (an IIFE returning JSON, or
`{error: "<sentinel>"}` when the page is not a profile), and
`parse(eval_result, captured) -> Profile`. Every `*_JS` string is
syntax-checked in node by `tests/test_dom_js.py`, and every extractor is
unit-tested against a synthetic fixture in the shape its JS returns.
Failed records stay pending and are simply retried next pass.

Instagram adds `capture_ready(captured, handle)` alongside `READY_JS`:
navigation returns once both its rendered header and matching profile response
are ready. Unrelated users in captured responses must never satisfy readiness
or supply another profile's fields. Missing profile JSON gets a bounded wait
and the existing complete-header fallback, not an empty cached profile.

Repeat `scrape --target` up to ten times for a coordinated queue. One process
selects records once, staggers starts, reserves attempts atomically in `Pacer`,
and serializes writes through the existing CLI path. There is no default daily
cap; all tabs share full periodic breaks. A per-platform run lock prevents competing
invocations. A source block or warning sets one shared stop event, stops tab
loading, and leaves unfinished records pending. Do not use independent scraper
processes as a substitute for this queue. No scheduled resume or automatic retry.
`scrape --record-id ID` selects exactly one pending/matched, nondeleted record
from the requested source, bypassing only staleness. Invalid selections fail
before connecting to the browser; the default queue is unchanged.

Sources whose export lacks profile links get a `list` command (`facebook`:
the friends page gives name-only records a handle by unique exact name;
`partiful`: the mutuals page routes each row to `/u/<uid>` only on click,
so the list is walked click-by-click and profiles are written as it goes;
`strava`: followers + following of the signed-in athlete; `spotify`: followers
and followed users, excluding artist pages and checking the displayed totals). Partiful
records match a person only through the Instagram handle on their profile
(`match.py`), never by name. The mutual-list importer archives the original
profile extractor result and mutual-row context before parsing or navigating
back, and passes its file key to `upsert_profile` without uploading it again.
An archive failure halts the import and leaves the existing ledger/cache intact.

List collectors retain incremental `list-input-v1` observations through
`snapshot.retain_list` before deduplication, artist/self filtering or writes.
Each immutable capture records its source scope, selector, observation ordinal,
original entry ordinals, observed total, exclusions and complete/truncated status.
Empty terminal observations carry fixed termination reasons. Facebook's scroll
stopping heuristic, Strava's single rendered page per direction and Partiful's
rendered row count do not prove full inventory coverage. Strava emits `None`
for unobserved follow directions. Spotify marks a scope complete only when its
unique observed count equals the displayed total; unknown totals remain partial.
All list commands validate file-service configuration before opening a browser.
Partiful's `ROW_JS` is passive: ID assignment and scrolling happen through
`ROW_PREPARE_JS` only after verified row retention. If scrolling replaces or
changes the row, acquisition halts before clicking. Spotify's typed list-ID
boundary preserves percent-encoded `#` for row and owner path segments; source
IDs decode and handles re-encode. General profile/text/URL checks remain strict.

List entries and Facebook handle proposals carry `capture_refs`, with
`capture_key`, `scope`, `ordinal` and `entry_ordinal` for every retained occurrence.
`Record.capture_refs` is an optional in-memory tuple of these references; the
existing `capture_key` is also in-memory only. Partiful keeps the profile key in
`capture_key` and mutual-row references separately. Neither field enters ledger
columns or `raw`. Multiple observations keep all references. Replay preserves
metadata in full proposals but excludes both fields from normalized record-value
comparisons. Pure list replay explicitly reports unsupported inventory
reconstruction; retained scoped rows remain available as evidence.

`scrape/snapshot.py` owns the profile-input-v1 privacy boundary shared by
ordinary/coordinated scrapes and Partiful mutual profiles. `collect` reads only
declared profile regions before field extraction and returns an ordered safe
DOM tree with selector, exclusions and success/partial status. There is no
body/main fallback. Venmo never collects DOM or responses, only selected
`otherUser` fields. List-page acquisition is separate.

`prepare` filters extractor fields, context and matching source API profiles;
the exact resulting payload supplies both `photos.archive_profile` and parsing.
Only declared typed identities/counts/dates bypass free-text contact detection.
Canonical identity URLs have source-specific hosts/paths and no userinfo,
queries or fragments; all other URLs retain the strict generic privacy check.
Unknown and unsafe values carry fixed exclusion labels. Exact `raw_eval` strings
survive only when filtering leaves them intact, including no duplicate keys.
Malformed structured input without a provable boundary is excluded explicitly.
Legacy capture replay remains compatible, including Venmo REST field names;
`date_joined` is never a birthday.

`photos.archive_profile` uses unique versioned capture keys and upload/read-back
verification before parsing, avatar fetches or cache writes. Profile-input
envelopes revalidate their payload and declared policy. Failed parses keep their
files; unavailable placeholders link to them. Already received permitted API
input and collected DOM survive later failures with explicit failure metadata.
Archive failure stops the run, including the coordinated queue. Unreceived
responses and process death before retention remain outside this guarantee.
Unsafe avatar record-key components use full SHA256; safe existing keys remain
compatible, and invalid prior keys never satisfy the same-image reuse shortcut.
Existing retained objects are never renamed or deleted.
Signed avatar URLs use a separate transient acquisition handoff after verified
retention; they never enter the retained payload, parsed Profile or persisted
rows. Ordinary/coordinated storage consumes that handoff; Partiful harvest
passes `_avatar_url` only until ingest pops it. Existing direct/page fetch,
deduplication and block status handling apply; HTTP error details omit signed
URLs. Facebook ledger IDs follow the export's normalized-name contract (including
Unicode/apostrophes), independently of profile handles and safe storage keys.

## WhatsApp snapshot evidence

`people-sync ingest whatsapp --snapshot PATH --media-dir PATH --self-id JID` reads
an operator-prepared, WAL-consistent metadata-only copy of the desktop app's
store (`whatsapp.py`): chat sessions, push names and cached-picture metadata,
never message bodies, opened `mode=ro&immutable=1`. Only active direct chats
become evidence; self (by the explicit native JID), group, status, broadcast and
community rows are counted under `excluded`. Identity is the native opaque
`@lid`; a phone-only chat gets a random id from the 0600 `whatsapp-ids.json`
map in the state dir, so phone-form JIDs and media paths (which embed numbers)
never enter a capture. Names go through the shared export value check, so a
number saved as a name is excluded, not retained. Pictures are read only from
inside the media root (traversal and symlink escapes are refused unread),
signature-checked, and embedded in the `contacts` capture so it replays offline;
the capture and every picture are retained before any estate write. Records stay
`pending`: `match.py` skips the source entirely, and there is no profile URL
(none exists for a native chat, and usernames are not in this schema). At
ingest each phone-form JID's digits are looked up in the local address book
(`sources.phone_index`, joined to Google records through the CardDAV external
id, `sources.google_contact_ids`); the matching contacts' ledger ids are kept
as `contact_refs` (typed `contact-ref`, optional in older captures) and the
number is used in memory only. `--no-contacts` skips the lookup.

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

`PEOPLE_SYNC_CDP_TARGET` attaches to a caller-owned tab, preserving its session
across login and scrape commands. The caller groups and closes that tab; the
CLI never creates a replacement or closes an explicitly selected target.

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

Every `life sql` write costs seconds (a read is instant), so estate writes
are batched: `ledger.batch_update` turns a set of row updates into one
`UPDATE ... CASE <key>` statement per 200 rows, `ledger.imported_from_many`
checks and inserts evidence for a whole ingest in two round trips, and
`profile.upsert_profiles` does the same for profile rows (`upsert_profile`
is the one-item form). A source that writes rows one at a time in a loop
is the bug to fix, not a reason to add a scheduler or a daemon.

`ledger.imported_from(table, row_id, capture_key=None, capture_refs=())` attaches
whole-row `imported_from` evidence to each distinct retained key after successful
record/profile writes. IDs hash the capture key, destination table/row, relation
and field. Retries insert missing edges only, including when a Facebook handle
already equals its unique-name proposal. Deleted edges and source/profile rows
stay untouched. Duplicate ledger observations retain their distinct keys before
last-row deduplication; held IDs receive no import claims. Tombstoned records
are reported through `held` with reason `deleted-record`.

Promotion operations carry `raw_r2_key`; new `takeout` evidence references that
file, never the mutable profile row. The report exposes `missing_evidence` and
`legacy` record IDs. Missing captures block new promotion; historical profile-row
edges remain unchanged. Import-edge detail is null; promotion detail contains
only assertion properties. Replay remains proposal-only and preserves the
original capture identity/time without writing or claiming a fresh visit.

Birthdays are `YYYY-MM-DD`. A source that gives a month and day but no year
is stored ISO 8601 style as `--MM-DD`; filter with
`substr(birthday, -5)` when matching a month/day.

`people.id` is always a Notion People page id with the dashes stripped. New
people therefore get their Notion stub page first and the row second, which is
exactly what `new-person` does - never insert a `people` row with an invented
id.

## Triage reconcile (`people-sync reconcile`)

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

Export capture uses `export-field-filter-v1`: unsafe or ambiguous allowed-field
values become missing CSV cells or JSON nulls without dropping row ordinals or
changing the original files. Malformed objects retain their row slots; unknown
top-level shapes and duplicate JSON keys/CSV headers fail explicitly.
`payload.field_exclusions` contains version 1 and entries with file role,
zero-based data-row ordinal, field path and fixed reason. A null ordinal means
file-level metadata; `field` plus an index identifies an excluded original
column/object member without retaining an unknown key, and `preamble` identifies
discarded CSV preamble text. No excluded value enters the manifest.
Canonical LinkedIn URLs and Instagram URLs/handles use the shared typed URL
boundary. LinkedIn permits a single decoded UTF-8 segment with Unicode letters,
marks, numbers and Pi/Pf quotation punctuation plus the existing ASCII `._-`;
controls, separators, residual escapes, dot traversal and credential markers fail.
Inspection never rewrites the URL or the parser's lowercased encoded record ID.
Export epochs are integer Unix seconds in `[0, 4102444800)` (1970 through 2099);
booleans, floats, strings and out-of-range values are excluded at capture and
rejected during revalidation. JSON null is the missing epoch representation.
Arbitrary free text retains generic privacy checks. Envelope validation
and offline replay reject unsafe retained values, even with a valid checksum.
Replay reports exclusions and missing-value effects as proposals; this is not
authorization to replace current ledger/cache fields. It does not apply changes.

Replay marks proposed records affected by permitted-field exclusions with
`Record.hold_existing`, an in-memory flag passed through `sources.retained_records`.
The flag follows the file role and retained record input, including the winning
duplicate proposal, and never becomes a ledger column or part of `raw`.
`ledger.upsert` holds flagged EXISTING rows completely unchanged, including
decisions, tombstones and timestamps, and continues eligible safe/new rows.
Its optional `held` result lists `record_id`, `capture_key` and a fixed reason;
held rows are excluded from `new`/`updated` counts. Import consumers must exclude
these held IDs from successful-import claims, including `imported_from` edges.

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


## Retained file storage

Life Data owns person photos (`photos/people/`), record avatars
(`photos/records/`) and retained source snapshots (`profiles/`). This is an
approved shared-service contract: `photos.py` uses only `LIFE_HUB_URL` and
a dedicated `LIFE_HUB_TOKEN`, with separate `files:read:<prefix>/` and
`files:write:<prefix>/` grants for those three namespaces. Existing object
keys and rows stay stable. No Cloudflare token or bucket config reaches
this client. Scrapes validate both settings before opening a source page.

## Notion credential boundary

People Sync owns a dedicated internal connection with Read content and
Insert content only. Connect exactly People, Gifts, Quotes, Trips and
Calendar. No Update content, comments, user information or agent access.
Notion applies those capabilities to every connected database, so Insert
content also applies to the four relation-check databases; the application
creates pages only in People. `NOTION_API_TOKEN` comes from this project's
environment, including the installed mini wrapper. Stub creation references
the stable `title` property ID. Relation checks are read-only; human
operators re-point relations and handle deletions.

## Private review page

`people-sync review` (`review.py`) renders prepared review-context JSON into an
offline HTML page using the packaged `review.html`. Pass repeated `--batch LABEL JSON`, a
`--photos` manifest mapping retained photo keys to local `photos/<filename>`
paths, and `--output` outside the repository. Download and verify retained
photos through the file service before rendering. No credentials enter the
HTML; the page makes no API requests or database writes.

Optional `--proposals JSON` supplies prepared identity clusters, keyed by
exact `json.dumps([batch, name], ensure_ascii=False)` group keys with Python's
default separators. Each proposal contains `clusters` and an optional
`question`; each cluster has `label`, `reason`, `person_ids`, `record_ids`,
and optional `uncertainty`. References are validated within their group with
separate person/record namespaces and no duplicate assignment. A question-only
proposal can have no clusters; an individual cluster must contain IDs.

Clusters show compact names, circles, Google context and clickable profile
thumbnails by default; full evidence remains under See full account details.
The page groups all saved evidence under these proposals, retains unassigned
items as unresolved, and accepts one approval or freeform correction per name
group. No proposal or no clusters means approval is disabled. This is a review
surface, not a matcher; photos never imply an identity link.

The snapshot digest depends only on the original evidence groups so existing
v1 browser saves remain recoverable. Each proposal has its own digest; changing
it invalidates that group's approval while preserving previous responses and
text for reference. V2 saves use `people-review:v2:<snapshot>` and never write
the original `people-review:v1:<snapshot>` key. JSON exports include proposals,
responses, prior responses, unresolved IDs, and the original legacy state.
Keep every generated page, image and decision export outside source control.
The optional `tests/review_browser.mjs` check exercises a synthetic page via
the chrome-control CDP bridge. It refuses to run outside `/review-smoke.html`,
expects the synthetic Batch Smoke groups Example, Next and Unresolved, and
restores their original browser saves after checking interactions.
