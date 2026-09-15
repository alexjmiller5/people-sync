---
name: people-sync
description: Operate the people-sync CLI - consolidate contact sources (Instagram, Facebook, Snapchat, LinkedIn, Google Contacts, Apple Contacts, WhatsApp, Venmo, Partiful, Spotify, Strava) into a life-data people estate, triage the pending ledger with the user, scrape and enrich profiles through a CDP-attached Chrome, keep every input as a retained capture, and build the private photo-assisted review page. Use for any run of `people-sync`, any contact export ingest, any people/contacts triage or profile enrichment, and whenever a source's markup or export format changes.
---

# people-sync

`people-sync` is an ad hoc tool, driven by an agent with the user in the
loop. The deterministic parts - parsing exports, retaining captures,
upserting the ledger, conservative auto-matching, hashing photos, scraping a
profile page - are the CLI. Everything that needs judgment (which account is
whose, what a person's context is) is a conversation. **Nothing is
scheduled, ever.** A month of new contacts is a few hundred rows.

The installed CLI is the only thing to run; the source checkout is for
development. Life-data is the estate it writes into (`life-map` /
`life-cli` skills for the schema and the `life` CLI).

## Ground rules

- **The ledger is the memory.** Every record ever seen is a
  `people_sync_records` row keyed `<source>:<source_id>` with a status:
  `pending` resurfaces, `matched` never re-asks, `ignored` never comes back.
  Never clear it to "start fresh".
- **Every input is retained before it is parsed.** Exports, contact pages,
  snapshots and profile visits are uploaded as immutable captures, read back,
  and only then interpreted; every row and promoted fact carries a
  provenance edge to the exact capture. A retention failure stops the run.
- **A missing or stale export skips that source; it never blocks the run.**
- **The address books are the phone book, life-data is the brain.** Emails,
  phone numbers and postal addresses are never copied into life-data; the
  `person_accounts.source_id` (Google `people/c...`, Apple `<uuid>:ABPerson`)
  is the pointer, resolved on demand.
- **Nothing links an account to a person without the user's word**, except
  the exact-name auto-matcher (Step 2), whose links are recorded and
  reversible.
- **Notion is optional.** With `PEOPLE_SYNC_NOTION_PEOPLE_DS` set, a new
  person's `people.id` is their Notion People page id (dash-stripped) and
  `new-person` creates the stub page first, which keeps Notion relations
  resolvable. Without it, ids are minted locally in the same shape and
  Notion is never contacted; `reconcile merge` then skips its relation check.

## Configuration the operator supplies

Environment variables, all optional except where a command needs them:

| Variable | Used by | Meaning |
|---|---|---|
| `LIFE_HUB_URL`, `LIFE_HUB_TOKEN` | captures, photos, scrape, list | the life-data file service (scoped `files:*` grants for `profiles/`, `photos/records/`, `photos/people/`) |
| `NOTION_API_TOKEN`, `PEOPLE_SYNC_NOTION_PEOPLE_DS` | `new-person` | the Notion connection and the People data-source id it creates stubs in |
| `PEOPLE_SYNC_NOTION_RELATIONS` | `reconcile merge` | JSON `{"<label>": ["<data_source_id>", "<relation property id>"]}` of the DBs that relate to People; unset = the relation check is skipped with a warning |
| `PEOPLE_SYNC_CDP_ENDPOINT` | login, scrape, list | `host:port` of a Chrome DevTools endpoint to attach to (a dedicated-profile Chrome, no approval prompt) |
| `PEOPLE_SYNC_CDP_TARGET` | login, scrape, list | attach to one caller-owned tab and leave it open |
| `PEOPLE_SYNC_CDP_APPROVE_COMMAND` | attach on a real profile | a command that answers the browser's remote-debugging prompt |
| `PEOPLE_SYNC_CREDENTIAL_COMMAND`, `PEOPLE_SYNC_EMAIL_CODE_COMMAND`, `PEOPLE_SYNC_SMS_CODE_COMMAND` | `login` | commands that print a login's credentials / one-time codes; unset = `login` only verifies an existing session |
| `XDG_STATE_HOME` | everything | private state root (`.../people-sync/`: capture cache, scrape counters, halt screenshots, the WhatsApp id map) |

The home-manager module (`homeModules.default`) turns these into options and
installs this skill next to the CLI. Credentials reach the process through
the environment or those commands; the CLI never reads a secret store.

## Step 0 - exports

Instagram, Facebook, Snapchat and LinkedIn are ingested from the platforms'
data exports; Google and Apple are read live; WhatsApp from a metadata-only
snapshot of the desktop app. Keep exports in a private directory outside any
checkout.

| Source | Where the export comes from | File |
|---|---|---|
| Instagram | Accounts Center -> Download your information -> Followers and following, JSON | a directory with `followers.json` + `following.json` |
| Facebook | same flow, Facebook profile -> Friends, JSON | `your_friends.json` |
| Snapchat | app -> Settings -> My Data -> request -> ZIP | `friends.json` (only its `Friends` list is people) |
| LinkedIn | Settings -> Data privacy -> Get a copy of your data -> full archive | `Connections.csv` |

Sanity-check any export shape you have not seen before: open it, confirm the
top-level key and per-entry fields, and compare the parsed count against the
entry count. A mismatch means a parser fix (TDD against an invented synthetic
fixture), never editing the export.

## Step 1 - ingest

```bash
people-sync ingest instagram --path <dir>
people-sync ingest facebook  --path <your_friends.json>
people-sync ingest snapchat  --path <friends.json>
people-sync ingest linkedin  --path <Connections.csv>
people-sync ingest google              # one gog call per contact, ~10 min per 1k
people-sync ingest apple               # local AddressBook sqlite, read-only (needs Full Disk Access)
people-sync ingest whatsapp --snapshot <metadata.sqlite> --media-dir <group-container> --self-id <own JID>
```

Each prints `{"new": N, "updated": N}` (plus `held` for rows the privacy
filter could not refresh). Re-ingesting is a no-op beyond refreshed `raw`,
follow flags and `last_seen`, so a partial run is safe to repeat.

**WhatsApp** takes an operator-prepared snapshot, never the live store: copy
`ChatStorage.sqlite` with its `-wal`/`-shm` to a private 0700 directory,
then extract only `ZWACHATSESSION`, `ZWAPROFILEPICTUREITEM` and
`ZWAPROFILEPUSHNAME` into a new sqlite file (`ATTACH` + `CREATE TABLE AS
SELECT`), so no message body is ever an input. `--self-id` is the account's
own JID (the app's `OwnJabberID` preference); the command refuses to guess
it. The media dir is the app's Group Container; pictures are read in place
and only from inside that root. Every active direct chat becomes a pending
record (opaque `lid-...` id, session or push name, cached picture); a phone
number saved as a name is dropped by the privacy check, so some rows arrive
nameless with only a picture. `match` never links WhatsApp. Each chat's
number is looked up in the local address book at ingest (skip with
`--no-contacts`) and the matching contacts' ledger ids are kept as
`contact_refs` on the record; the number itself is never retained. Those
pointers are what lets the proposer join a WhatsApp row to its Google contact.

### Retained captures and offline replay

```bash
people-sync capture linkedin --path <Connections.csv>   # retain only, no ingest
people-sync captures                                     # list + verify the local cache against the file service
people-sync replay --input <capture.json> --output <proposal.json> [--compare <previous.json>]
```

`replay` is proposal-only: after a parser fix, replay the retained capture
and diff the proposal against the ledger/cache, then apply through the
normal ingest or reconcile path. A value the privacy filter refuses is
recorded as an exclusion, never bypassed. A capture that retained only
extracted fields cannot reconstruct what was never retained; say so.

## Step 2 - match

```bash
people-sync match          # {"auto": N, "suggested": N, "left_pending": N}
```

Auto-links only exact, unambiguous normalized-name matches (Instagram on the
handle's letters; Partiful only through an Instagram handle on the profile;
WhatsApp never), writes the `person_accounts` row and marks the record
`matched`. Single-word names and any person two records resolve to are left
alone; fuzzy cases get `suggested_person_id` and stay pending. A wrong
auto-link is reversed by flipping the record back to `pending` and
soft-deleting the account row.

## Step 3 - triage

```bash
people-sync queue          # every pending record, suggestions first
```

Present the queue to the user in batches of 10-20 with the identifying
context (source, handle, display name, follow direction, scraped context),
never one message per record. Three verbs:

- **link** an existing person / **merge** two people / **create** a person:
  `people-sync reconcile link <person_id> <record_id>`, `merge <loser>
  <winner>`, `create <record_id> --name ...`. Dry run by default, `--apply`
  to write. Lossless by construction: existing values are never overwritten
  (conflicts land in `notes`, a replaced name survives as `nickname`,
  circles union, a merge re-points every child row and prints the loser's
  Notion relations for a manual re-point).
- **new person** when the estate has never had them:
  `people-sync new-person --name "<Full Name>"` (Notion stub, then the row).
- **ignore** a stranger, business or burner:
  `life sql "UPDATE people_sync_records SET status = 'ignored' WHERE id = '<record_id>'"`.
  Ignored never resurfaces; use it, or the stranger is re-triaged forever.

While the user is looking at a person, record what they volunteer in the
right table: `people.circles` (their vocabulary; read sibling rows first and
reuse a value verbatim), `birthday` / `slightly_known_birthday` /
`notify_birthday`, `person_locations` (current = `end IS NULL`),
`person_employments`, `person_relations` (family edges in both directions,
`met_through`), `people.notes`.

For a large reconciliation with many same-name candidates, build the private
review page (below) first: photos and scraped context are what disambiguate.

## Step 4 - address-book port

Apple Contacts is an inbox; Google is the canonical address book. List
people with an `apple_contacts` account and no `google_contacts` one, clean
each up with the user, create it in Google (`gog contacts create`, then
`update --birthday`), insert the `google_contacts` account row, and keep the
Apple row too. Semantic fields (labels, org, notes) stay out of Google.

## Step 5 - Google write-back cleanup

Once a Google contact's labels are circles and its org is an employment
row, clear them from Google:

```bash
people-sync google-cleanup             # dry run - always first
people-sync google-cleanup --apply     # with the user watching
```

It clears a label or org only when that exact value is already in
life-data, re-reads the live contact before clearing an org, and prints
`SKIP <name>: ...` for anything else. A SKIP is a triage to-do, not an error.

## Step 6 - profile enrichment

Every social record gets its profile scraped (display name, bio, location,
hometown, school, work, links, counts, picture) into `people_sync_profiles`
with the picture stored content-addressed; each visit is a new retained
capture, and the profile row points at the latest one. Scrape before big
reconciliation batches.

The browser is any Chrome reachable over CDP (`PEOPLE_SYNC_CDP_ENDPOINT`)
whose profile holds the user's logins - a dedicated-profile Chrome on a
remote machine through an ssh port-forward is the usual shape (`chrome-control`
skill). Create the tab(s) yourself in a session window/group and pass them:

```bash
people-sync login <platform>                                   # idempotent: verifies the session, signs in only with the credential commands wired
people-sync scrape <platform> --max 100 --target <tab-id>      # one tab
people-sync scrape instagram --target <t1> --target <t2> ... --target <t10>   # up to ten coordinated tabs, one queue
people-sync scrape <platform> --record-id <source:id>          # one specific record
```

`scrape` prints `{"done": N, "skipped": N, "halted": <reason|null>}`.
Pacing lives in the CLI (8-25 s between pages, a break every 25 attempts,
staggered starts across tabs, no daily cap). Counters persist in the state
dir; a backfill can span many sessions, and later runs visit only new or
stale (180 days) records.

**Halts are final.** A source 401/403/429, a challenge or checkpoint page, a
visible account warning, browser loss, or any unexpected error stops every
tab, screenshots the page into the state dir, and leaves the rest pending.
Show the user the reason and screenshot; never dismiss a warning, never
auto-restart, and stop that platform's bulk collection after an
"unusual activity" / "high volume of profile access" warning. During a
long run, check the log and process at least every minute.

**Logins are human by default.** With the credential commands unset,
`login` only verifies. When a session expires, the user signs in and
completes 2FA in the shared Chrome; the session persists. Wired commands
type at human pace, retry a password once, and halt on any unknown page.

**When a site changes** ("no login form", "unrecognized page after submit",
"no header"): dump the live page's inputs/buttons over CDP, fix
`scrape/login_specs.py` or the platform extractor in the repo (TDD, synthetic
fixture; button selectors support `text=<label>`), release, reinstall. Tell
the user what moved.

**List commands** for sources whose export lacks profile links:
`list facebook` (friends page -> handles for name-only records, unique exact
name only), `list partiful` (walks `/mutuals` profile by profile; matches a
person only through an Instagram handle; built-in avatars are placeholders),
`list strava` and `list spotify` (followers + following, totals checked).
Venmo's friend inventory comes from its authenticated `/v1/users/<id>/friends`
API with the session's bearer token held in memory only; payment
counterparties are a separate, explicit request, deduplicated by account id
and linked to their transactions through `provenance`, never by name.

**Promotion.** `people-sync promote` (dry run; `--apply`) copies scraped
city / employer / birthday / picture onto matched people where the field is
empty, each with an `evidence_of` provenance edge to the retained capture;
conflicts are printed, never resolved automatically. Address-book pictures
come from their APIs (`photos store`).

## The review page

```bash
people-sync propose --batch "<label>" <context.json> [--batch ...] [--google-groups <labels.json>] [--cues <cues.json>] [--links <links.json>] --output <proposals.json>
people-sync review  --batch "<label>" <context.json> [--batch ...] --photos <manifest.json> --proposals <proposals.json> --output <dir>/index.html
```

`propose` clusters each name group on concrete signals only - the same full
name, a handle that spells or abbreviates a name, a surname with a small typo,
a shared place/school/era cue (Google label names via `--google-groups`,
extra cue text per id via `--cues`), or a shared phone number via `--links`
(`{record id: [record ids]}`, built from WhatsApp `contact_refs`). Anything
unconnected is its own proposed person. Fuzzy joins carry an uncertainty
note. Proposals never change the page's evidence, so regenerating them keeps
the user's saved responses; a group whose proposal changed asks again.

Contexts are prepared JSON per name group (`current_people`,
`google_candidates`, `profiles`), the photo manifest maps stored keys to
local `photos/<file>` paths you downloaded and hash-verified through the
file service, and proposals are your suggested identity clusters with a
reason each. The page is offline, private (0600, outside any checkout),
makes no requests, and shows each proposed person as a box: the user drags
cards between boxes, drags a box onto another to combine them, or drops a
card on "separate person" / "ignore", then "Looks right" approves that
arrangement (the export carries it); the text box is for anything drag
cannot say. Apply only what was approved, box by box.
Photos are for the user to inspect, never automatic face matching.

## Step 7 - quality sweep

Short lists to work through with the user, every run:

```bash
life sql "SELECT id, name FROM people WHERE deleted_at IS NULL AND (circles IS NULL OR json_array_length(circles) = 0) ORDER BY name"
life sql "SELECT id, name FROM people WHERE deleted_at IS NULL AND notify_birthday = 1 AND birthday IS NULL"
life sql "SELECT a.person_id, a.platform, a.handle FROM person_accounts a WHERE a.deleted_at IS NULL AND a.active = 1 AND NOT EXISTS (SELECT 1 FROM person_photos ph WHERE ph.deleted_at IS NULL AND ph.person_id = a.person_id AND ph.platform = a.platform)"
life sql "SELECT value, count(*) n FROM people, json_each(people.circles) WHERE people.deleted_at IS NULL GROUP BY value ORDER BY value"
```

A vocabulary fix is an estate-wide rename with the user's approval, never a
half-migrated pair of spellings.

## Step 8 - sync and report

Let life-data's own sync move the rows (`life background status`; a
foreground `life sync` only when needed), verify a changed row on another
replica, then report in chat: per-source ingested/new counts, sources
skipped and why, matched/confirmed/created/ignored, cleanup, photos,
captures retained and verified, sweep counts, and anything left open.

## Gotchas

- A cloud-synced folder can hand a parser an empty file (dataless
  placeholder). Zero entries from a file that clearly has content means copy
  it to a local path first.
- `new-person` is two writes. If the life-data insert fails after the Notion
  page exists, it prints the orphaned page id; re-run with it rather than
  creating a second page.
- Never hard-delete a life-data row; soft delete only, or the next sync
  resurrects it.
- LinkedIn's export drops connections it cannot render (rows with only a
  `Connected On` date); they are skipped with a warning by design.
- Every `life sql` write costs seconds; the CLI batches its writes, and a
  loop of single-row writes is the bug to fix, not a reason to schedule.
