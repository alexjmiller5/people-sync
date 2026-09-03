# People DB & Contact Sync - Design Spec

Date: 2026-09-02. Status: approved design, pre-implementation.

## Overview

Redesign the life-data people estate into a normalized schema that captures
everything Alex knows about people, and build the monthly ad-hoc
consolidation workflow that keeps it fed from every source where he knows
people: Apple Contacts, Google Contacts, Instagram, Snapchat, LinkedIn,
Facebook, WhatsApp, Venmo, Partiful.

life-data is the authority for people data. Notion People stays frozen as
the relation anchor for its dependent Notion DBs (Gifts, Quotes, Trips,
Calendar), which deliberately remain in Notion for their UI. The
birthday-reminders app is the downstream consumer that makes this urgent
(662 people, only 24 birthdays today); it is migrated in a separate session
using the guidance section below.

## Non-goals

* **No daemon, no cron, no launchd.** The consolidation is an ad-hoc,
  roughly monthly run that Alex kicks off in a Claude session
  ("let's do contacts review"). One-off operational work stays direct.
* **No migration of Notion's dependent DBs** (Gifts/Quotes/Trips/Calendar).
* **No changes to birthday-reminders in this project** - guidance only.
* **No Find My scraper yet** - its stream contract is defined here; the
  scraper is a separate future mini-job project.
* **No realtime social syncs** (e.g. nightly Instagram mute-status sync) -
  Alex has already judged realtime social scraping not viable; the monthly
  export cadence is the design.

## Schema (life-data, via `life` CLI)

All tables get the standard sync columns (`id`, `created_at`, `updated_at`,
`deleted_at`) from `life table create`. Soft deletes only. Multi-valued
scalars are JSON array text columns; real relations are junction tables.

### people (rework of the existing 662-row table)

Keeps: `name`, `first_name`, `middle_name`, `last_name`, `nickname`,
`surname_at_birth`, `gender`, `birthday`, `slightly_known_birthday`,
`death_date`, `deceased`, `likes`, `dislikes`.

Adds:

* `circles` (JSON array) - the single vocabulary for every community,
  cohort, era, and context a person belongs to. Absorbs the old `tags`,
  the circle-shaped `company` values, the `when_we_met` era labels, and
  Google Contacts labels.
* `notes` (TEXT) - free-text context.
* `notify_birthday` (INTEGER 0/1) - migrated from the Notion
  "Birthday Notifications" checkbox.

Drops after migration: `company`, `position_title`, `employer`,
`when_we_met`, `tags`, `instagram`, `facebook`, `snapchat`, `linkedin_url`,
`google_contacts_url`.

**Row id invariant:** **`people.id`** **is always a Notion People page id**
(dash-stripped). True for all imported rows today; preserved for new people
by creating a bare Notion People page (name only) at person-creation time
and using its page id as the row id. This is what keeps Notion-side
relations (Gifts etc.) working for every person, old and new.

### person\_accounts (new - the only home for identities)

One row per (person, platform, handle). Columns: `person_id`, `platform`,
`handle`, `url`, `source_id`, `display_name`, `active` (INTEGER, default 1),
`notes`.

* `platform` values (open set): `instagram`, `facebook`, `snapchat`,
  `linkedin`, `google_contacts`, `apple_contacts`, `whatsapp`, `venmo`,
  `partiful`.
* `source_id` is the platform's stable id where one exists (Google People
  `resourceName`, Apple Contacts id). For `google_contacts` /
  `apple_contacts` rows this is a foreign key into those systems: emails,
  phones, and addresses are NOT copied into life-data - they resolve
  on demand through `gog` / local Contacts.
* Handle renames: the old row is kept with `active = 0`; the new handle is
  a new row. Handle history is free.
* A person may hold multiple accounts on one platform.

### contact\_records (new - the resolution ledger)

One row per (source, source\_id) ever seen by any ingest. This is the memory
that makes runs incremental: dismissed strangers never resurface.

Columns: `source`, `source_id`, `handle`, `name`, `raw` (verbatim source
JSON, latest wins), `follows_me` (INTEGER nullable), `i_follow` (INTEGER
nullable), `status` (`pending` / `matched` / `ignored`), `person_id`
(set when matched), `suggested_person_id` (matcher proposal awaiting
triage), `first_seen`, `last_seen`.

* Row id = `<source>:<source_id>` so re-ingests are idempotent upserts:
  new record → insert as `pending`; known record → bump `last_seen`,
  refresh `raw` and follow flags.
* A run's triage queue is exactly `status = 'pending'`.

### person\_locations (new - historical, claim-grade)

Columns: `person_id`, `city`, `country`, `start`, `end` (NULL = current),
`source` (`manual` / `findmy` / `inferred`), `notes`.

* Current city is derived: the row with `end IS NULL`. No city column on
  people, so nothing can drift.
* A move = close the open row, insert a new one. History is automatic.

### person\_employments (new - historical)

Columns: `person_id`, `company`, `title`, `start`, `end` (NULL = current),
`source`, `notes`. Same shape as person\_locations. Replaces the `employer`
multi-select and `position_title` column; fed by LinkedIn/Google org fields
and triage.

### person\_photos (new - append-only)

Columns: `person_id`, `platform`, `r2_key`, `sha256`, `fetched_at`,
`notes`.

* Images live in R2 (`life-data-archive` bucket,
  `photos/people/<person_id>/` prefix); only keys in the db.
* Append-only: a changed profile picture is a new row; old rows stay -
  photo history is free. `sha256` prevents re-storing an unchanged image
  on every sweep.

### person\_relations (existing - extended)

Unchanged table; the `relation_type` vocabulary gains `met_through`
(person A was met through person B). Replaces "Through <person>"-style
string labels with real graph edges - additively; the originating string
value still becomes a circle unless Alex explicitly says otherwise.

### friend\_locations stream (contract only; scraper is a future project)

Hub-backed append-only stream for friends' measured positions (Find My
scraper on the mac mini, to be built as its own mini-job project).

Record: `{"person_id": "<people.id>", "lat": <deg>, "lon": <deg>,
"tst": <unix s>, "acc": <m, optional>, "source": "findmy"}`.
Claim-grade: measured. Manual whereabouts claims do NOT go here - they are
person\_locations rows. Document in life-map when first wired.

## Migration (one-time, interactive with Alex)

Order matters; `life export > backup.sql` first, and `life sync` after each
completed step.

1. **Reconcile Notion drift**: pull Notion People pages with
   `last_edited_time` after the 2026-09-01 import and fold any human edits
   into life-data (the in-progress label-fixing work may have landed there).
2. **Create new tables**; add `circles`, `notes`, `notify_birthday` to
   people.
3. **notify\_birthday** ← Notion "Birthday Notifications" checkbox, matched
   by page id = row id.
4. **Handles → person\_accounts**: one row per non-empty flat handle column;
   verify migrated row count equals the count of non-empty source values;
   then drop the flat columns.
5. **Circles union - PRESERVATION INVARIANT.** Every distinct value of
   `tags` ∪ `company` ∪ `when_we_met` (and later, Google Contacts labels)
   becomes a circle **verbatim** on every person who carried it. Nothing is
   dropped, merged, renamed, split, or reclassified except by Alex's
   explicit per-value decision in an interactive mapping session. That
   includes: values that look like duplicates of each other (they are
   separate circles unless Alex merges them), values that look like
   locations or employers, comma-joined values (split only with approval -
   some values legitimately contain commas), and values that look like
   junk (they are not; ask). `met_through` edges and person\_locations /
   person\_employments rows created during the session are additions; the
   original string survives as a circle unless Alex explicitly retires it.
6. **Employment**: `employer` + `position_title` → person\_employments rows
   (`end = NULL`, `source = 'notion_import'`); then drop the columns. The
   session decides ambiguous values (e.g. multi-employer strings).
7. **Verification**: before/after counts for every migrated value
   (per-circle person counts vs. old per-tag/company/era counts must
   reconcile to zero loss); spot-check named rows with Alex.

## The code (this repo, renamed `contact-sync`)

The repo is reworked in place: rename to `contact-sync` (repo + local dir;
update projects skill, repo description/topics per repo-metadata skill).

**All existing parser and test code is presumed wrong and is deleted, not
adapted.** Parsers are rewritten TDD from scratch against the real export
files on disk, with committed fixtures that are fully synthetic/sanitized -
no real names, handles, or any personal data ever enters the repo. Each
parser is additionally validated against a freshly downloaded export during
implementation (the June-2025 exports may predate current formats).

Deleted with it: Notion People writers, Notion task creation
(`new_contacts.py`), the launchd/nix-darwin module and mini scheduling
(this is no longer a scheduled job), `enrich.py` matching.

Scripts (plain Python via `uv run`, invoked ad hoc by Claude):

* `ingest instagram|facebook|snapchat|linkedin --path <export>` - parse
  export → upsert contact\_records (followers/following/friends/connections
  set the follow flags where the source knows them).
* `ingest google` - via `gog` (People API): contacts, labels, org fields →
  contact\_records. No manual export.
* `ingest apple` - local Contacts access (per the apple-contacts skill) →
  contact\_records. No manual export.
* `match` - normalized-name matching between `pending` records and people.
  Auto-links only exact, unambiguous matches (writes person\_accounts row +
  ledger `matched`); everything fuzzy gets `suggested_person_id` and stays
  `pending`. Single-word names are never auto-matched. Every auto-link is
  recorded in the ledger, so it is auditable and reversible.
* `photos fetch` - Apple/Google contact photos via API; hash, upload to R2,
  append person\_photos rows.

WhatsApp, Venmo, and Partiful have no exports: accounts are entered during
triage; their photos are captured best-effort (chrome-control-assisted for
specific people Alex cares about).

## The workflow (`contacts-review` skill, in agent-config)

Alex starts it by asking; roughly monthly. The skill documents:

1. Per-platform export refresh procedures (manual click-ops). A stale or
   missing export skips that source for the run - never blocks it.
2. Run the ingests and matcher.
3. Chat triage of the `pending` queue, grouped by confidence:
   match / new person / ignore. Filling circles, locations, birthdays,
   relations as decided.
4. **New person creation**: create the bare Notion People page first, use
   its page id as the life-data row id (the invariant above).
5. **Apple → Google port**: Apple Contacts is an inbox. New Apple contacts
   are cleaned up, created in Google Contacts via `gog`, and both linked
   as person\_accounts rows. Google is the canonical address book.
6. **Google write-back cleanup**: after a Google contact's labels and org
   fields are consolidated into circles/employments, clear them from the
   Google contact - but only after the life-data write is re-read and
   verified. Nothing is deleted from Google before its replacement is
   confirmed on disk.
7. Photo fetch for touched people.
8. Data-quality sweep: circle-less people, missing birthdays among
   close circles, vocabulary typos (fix = rename with Alex's approval).
9. `life sync`, then a short run report.

**Google Contacts boundary (the dedup principle): Google is the phone
book, people db is the brain.** Google keeps exactly what the iPhone
needs - name, phones, emails, addresses, birthday (for now), photo.
Everything semantic (labels, org, circles, socials, notes) lives only in
life-data. Accepted duplication: name, birthday, address. An open Notion
task (due 2026-09-16) decides the eventual source of truth for birthdays.

## birthday-reminders migration guidance (separate session - do not touch here)

* Replace the Notion People query with a life-data hub pull:
  `POST <hub>/v1/rows/pull` with
  `{"table": "people", "columns": ["id", "name", "birthday", "notify_birthday"], "since": ""}`,
  filtering client-side for `notify_birthday = 1`, `deleted_at` null, and
  birthday month/day = today.
* Mint a scoped token: `life token create birthday-reminders --scopes
  tables:read`; store it in the Birthday-Reminders 1P vault per the naming
  grammar, add hub URL + token to the project's ENV item / `.env.tpl`,
  `just sync-secrets`.
* Notion Tasks creation is unchanged (tasks stay in Notion).
* The Notion "Birthday Notifications" checkbox is superseded by
  `people.notify_birthday`; after cutover the checkbox is dead - flag, do
  not maintain both.
* Remove `PEOPLE_DATA_SOURCE_ID` and the People-DB Notion permissions when
  done; update the project memory file.

## Notion task consolidation (at ship time, with Alex)

The Contact Sync project's 17 open tasks map to this design roughly as:
absorbed by the schema/workflow (source enrichment tasks, Google links,
new-contact triage, contacts-review skill, monthly cadence, Apple cleanup
workflow, label fixing, location seeding), superseded (birthday
notification wiring → birthday-reminders migration), likely cancel
(realtime mute-status sync - contradicts the monthly-cadence decision), and
deferred content work that stays open (e.g. the Empire-community import
from the Apple note, WhatsApp pictures). Final status changes are made
with Alex when the implementation ships, not before.

## Testing

* TDD throughout: parser tests on sanitized fixtures first; matcher tests
  incl. the never-auto-match cases; ledger idempotency tests (double
  ingest = no new pending rows).
* Migration is rehearsed against a copy of `life.db` (dry-run producing
  the before/after reconciliation counts) before touching the live estate.
* Mutation-test the tests: break a parser field mapping, confirm failure.
* E2E = the first real contacts-review run with Alex, on fresh exports.

## Docs & estate updates at ship

* life-map: new tables, schemas, counts, the friend\_locations contract,
  people conventions (id invariant, circles vocabulary location).
* notion-workspace: People DB frozen-anchor status; stub-page convention.
* projects skill + repo-metadata: repo rename.
* This repo's README/AGENTS.md: full rewrite (current state only).
* Memory: project file update; birthday-reminders memory notes the
  pending migration guidance.

