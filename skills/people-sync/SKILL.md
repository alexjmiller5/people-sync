---
name: people-sync
description: Operate the people-sync CLI - consolidate contact sources (Instagram, Facebook, Snapchat, LinkedIn, Google Contacts, Apple Contacts, WhatsApp, Venmo, Partiful, Spotify, Strava) into a soma people estate, triage the pending ledger with the user, scrape and enrich profiles through a CDP-attached Chrome, keep every input as a retained capture, and build the private photo-assisted review page. Use for any run of `people-sync`, any contact export ingest, any people/contacts triage or profile enrichment, and whenever a source's markup or export format changes.
---

# people-sync

`people-sync` is an ad hoc tool, driven by an agent with the user in the
loop. The deterministic parts - parsing exports, retaining captures,
upserting the ledger, conservative auto-matching, hashing photos, scraping a
profile page - are the CLI. Everything that needs judgment (which account is
whose, what a person's context is) is a conversation. **Nothing is
scheduled, ever.** A month of new contacts is a few hundred rows.

The installed CLI is the only thing to run; the source checkout is for
development. Soma is the estate it writes into (`soma-map` /
`soma-cli` skills for the schema and the `soma` CLI).

## Ground rules

- **The ledger is the memory.** Every record ever seen is a
  `people_sync_records` row keyed `<source>:<source_id>` with a status:
  `pending` resurfaces; `matched`, `ignored`, and `public` preserve the review decision.
  Never clear it to "start fresh".
- **Every input is retained before it is parsed.** Exports, contact pages,
  snapshots and profile visits are uploaded as immutable captures, read back,
  and only then interpreted; every row and promoted fact carries a
  provenance edge to the exact capture. A retention failure stops the run.
- **A missing or stale export skips that source; it never blocks the run.**
- **The address books are the phone book, soma is the brain.** Emails,
  phone numbers and postal addresses are never copied into soma; the
  `person_accounts.source_id` (Google `people/c...`, Apple `<uuid>:ABPerson`)
  is the pointer, resolved on demand.
- **Nothing links an account to a person without the user's word**, except
  the exact-name auto-matcher (Step 2), whose links are recorded and
  reversible.
- **Notion is optional.** With `PEOPLE_SYNC_NOTION_PEOPLE_DS` set, a new
  person's `people.id` is their Notion People page id (dash-stripped) and
  `new-person` creates the stub page first, which keeps Notion relations
  resolvable. Without it, ids are minted locally in the same shape and
  Notion is never contacted; `reconcile merge` then says its relation check
  did not run.

## Configuration the operator supplies

Environment variables, all optional except where a command needs them:

| Variable | Used by | Meaning |
|---|---|---|
| `SOMA_HUB_URL`, `SOMA_HUB_TOKEN` | captures, photos, scrape, list | the soma file service (scoped `files:*` grants for `profiles/`, `photos/records/`, `photos/people/`) |
| `NOTION_API_TOKEN`, `PEOPLE_SYNC_NOTION_PEOPLE_DS` | `new-person` | the Notion connection and the People data-source id it creates stubs in |
| `PEOPLE_SYNC_NOTION_RELATIONS` | `reconcile merge` | JSON `{"<label>": ["<data_source_id>", "<relation property id>"]}` of the DBs with a one-way relation to People (two-way ones show on the loser page itself); unset = the merge report says those were not checked |
| `PEOPLE_SYNC_CDP_ENDPOINT` | login, scrape, list | `host:port` of a Chrome DevTools endpoint to attach to (a dedicated-profile Chrome, no approval prompt) |
| `PEOPLE_SYNC_CDP_TARGET` | login, scrape, list | attach to one caller-owned tab and leave it open |
| `PEOPLE_SYNC_CDP_APPROVE_COMMAND` | attach on a real profile | a command that answers the browser's remote-debugging prompt |
| `PEOPLE_SYNC_CREDENTIAL_COMMAND`, `PEOPLE_SYNC_EMAIL_CODE_COMMAND`, `PEOPLE_SYNC_SMS_CODE_COMMAND` | `login` | commands that print a login's credentials / one-time codes; unset = `login` only verifies an existing session |
| `SOMA_COMM_HUB_TOKEN` | `comm import`, `comm refresh` only | the hub token comm's `soma` calls use instead of `SOMA_HUB_TOKEN`: it must batch-append to the four `comm_*` streams (`/v1/streams/<name>/batch`) and query them (`soma archive query --raw`), which today takes `full`; unset = `SOMA_HUB_TOKEN`, and a refused append leaves events in the local outbox while `refresh` uses the local mirror |
| `PEOPLE_SYNC_ADDRESSBOOK_HOST` | `comm` | ssh host whose macOS address book is also read (read-only, in memory) to resolve participants; unset = this Mac's address book only |
| `XDG_STATE_HOME` | everything | private state root (`.../people-sync/`: capture cache, scrape counters, halt screenshots, the WhatsApp id map, `comm/comm.sqlite`) |

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

An export, and a Google or Apple read that completed, is the source's whole
inventory, so the result also carries `absent`: how many live records it no
longer lists, and the matched ones (`record_id`, `person_id`) - an unfriend,
an unfollow both ways, a deleted contact, or an Instagram rename (the record
id is the handle, so the new handle arrives as a new pending record). A known
follow flag on an absent record drops to 0, which is also what clears an
unfollowed stranger from `unfollow`; nothing else about the row changes. Go
through the matched ones with the user. A partial read reports no `absent`.

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
never one message per record. Review actions:

- **link** an existing person / **merge** two people / **create** a person:
  `people-sync reconcile link <person_id> <record_id>`, `merge <survivor>
  <loser>`, `create <record_id> --name ...`. Dry run by default, `--apply`
  to write. Lossless by construction: existing values are never overwritten
  (conflicts land in `notes`, a replaced name survives as `nickname`,
  circles union, a merge re-points every child row and prints every Notion
  relation anchored on the loser page for a manual re-point). The merge
  report ends with `NOTION RELATIONS CHECKED: N` only when every read
  succeeded; `INCOMPLETE` names what was not checked - then read the loser
  page by hand before retiring it.
- **new person** when the estate has never had them:
  `people-sync new-person --name "<Full Name>"` (Notion stub, then the row).
- **public** for a reviewed social account the user wants to keep outside personal
  relationships: create or reuse an owner through the installed `soma` CLI,
  then `people-sync reconcile public <record_id> --organization <id>`
  (or `--figure <id>` / `--festival <id>`). Dry run first; `--apply` writes.
  Owners live in `organizations`, `public_figures`, or existing `music_festivals`.
  The `public_accounts` relation references the ledger for its platform, handle,
  source ID, and profile evidence. No duplicated observations. Public records
  leave personal triage and unfollow suggestions and remain public on re-import.
  An interrupted write is repaired by rerunning with the same owner.
  Known friends stay people regardless of followers or verification. Never
  classify from follower counts alone. Employment remains separate.
- **ignore** a stranger or account the user does not want to keep:
  `people-sync reconcile ignore <record_id>` (dry run; `--apply` to write).
  Ignored records leave triage and appear in the unfollow report when followed.
  This command refuses matched or public records; reassignment is a separate
  explicit decision.

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
soma, re-reads the live contact before clearing an org, and prints
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
stale (180 days) records. A tombstoned profile row counts as visited for
the queue; `--record-id` recaptures it, and a complete capture restores the
row (the table is a latest-profile cache).

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
`list strava` and `list spotify` (followers + following, totals checked),
`list partiful-events [--since YYYY-MM-DD] [--refresh] [--event-id ID ...]`
(the signed-in user's past events they went to or hosted; a guest list is
click-walked so every guest resolves to a profile id, and a hosted event's
"Manage Guests" list is read in one go with the ids it already carries;
retained per event, and attendance lands on the Partiful records as
`raw.events` with the capture key; `promote` turns it into `person_events`
rows once a record is linked to a person). By default it walks only events
whose guest list is not retained yet, so a periodic run is a sweep of new
events; `--since` limits it to events on or after a date, `--refresh` re-walks
retained ones, and an explicit `--event-id` is always walked. It prints
`listed`, `already_retained`, and per walked event its `id`, `title`, `date`,
`guests` and `records` (an `error` marks a hidden or failed guest list; one
that failed mid-walk keeps what landed and can be re-walked by id). The
unmatched guests of one event are
`SELECT r.id, r.name, r.status FROM people_sync_records r, json_each(r.raw, '$.events') e WHERE r.source = 'partiful' AND json_extract(e.value, '$.id') = '<event id>' AND r.status = 'pending'`.
Pass the
chosen events to `propose --events` (`{record id: [titles]}`) so a lone
Partiful mutual who was at one of them earns a box.
`unfollow` (ignored accounts the user still follows, per platform with URLs; the
report's Unfollow line; re-ingesting an export clears the ones already done).

For requested removals, run `unfollow prepare --platform <source> --actor <account>
--record-id <id>` with the operator's CDP endpoint and grouped tab. Preparation
only reads live identity and relationship state, and saves a private exact plan.
Show the full batch before `unfollow apply <plan>`; the human must type its
exact terminal confirmation. Never enter that phrase for them. Instagram
unfollow and Venmo remove-friend have executable adapters. Facebook supports
read-only preparation; Facebook and LinkedIn removal refuse execution. Friendship absence does not
overwrite the independent follow flag. After interruption, `unfollow journal`
and `unfollow resume <original-plan>` verify outcomes without repeating removals.
`whatsapp-links --output PATH` (chat deep links for WhatsApp records whose number
is in the local address book; pass the file to `review --links` so each WhatsApp
card opens the chat in the desktop app for context).
Venmo's friend inventory comes from its authenticated `/v1/users/<id>/friends`
API with the session's bearer token held in memory only. Payment
counterparties come from the payments already in soma's `txns_venmo`:
`people-sync ingest venmo-payments` (no browser) gives every
person-to-person payment one `provenance` edge to its counterparty's record
`venmo:<user id>` - `imported_from` on the payment that first brings someone
in (creating a pending record), `mentions` after - deduplicated by account
id, never by name. Run it after every Venmo scrape; it is idempotent and
never rewrites an existing record. A counterparty the user does not know is
ignored, never a person; the payment still points at the record. A
check-time estate rule can hold the "every payment is linked" side; the
reverse does not hold (friends you never paid have no payment).

**Promotion.** `people-sync promote` (dry run; `--apply`) copies scraped
city / employer / birthday / picture onto matched people where the field is
empty, each with an `evidence_of` provenance edge to the retained capture;
conflicts are printed, never resolved automatically (a different current
city or employer, or a Partiful birthday month that disagrees with a known
birthday). A more or less specific form of a known value is not a conflict,
and a location that only names a country is not a city. A LinkedIn job is
promoted only when its capture classified the top-card org as a company
(older captures cannot tell a school from an employer). `--platform` and
`--kind` (repeatable) limit the plan; Partiful attendance is planned only
when `partiful` is included. Address-book pictures
come from their APIs (`photos store`). A matched Partiful profile also lists
the person's own Instagram / Snapchat / LinkedIn (plus TikTok and Twitter,
kept as links only while those are not platform values); the ones the person
lacks come back under `accounts` with their ledger record, if any. Each one
is the user's call: `reconcile link <person> <ledger record>`.

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
soma sql "SELECT id, name FROM people WHERE deleted_at IS NULL AND (circles IS NULL OR json_array_length(circles) = 0) ORDER BY name"
soma sql "SELECT id, name FROM people WHERE deleted_at IS NULL AND notify_birthday = 1 AND birthday IS NULL"
soma sql "SELECT a.person_id, a.platform, a.handle FROM person_accounts a WHERE a.deleted_at IS NULL AND a.active = 1 AND NOT EXISTS (SELECT 1 FROM person_photos ph WHERE ph.deleted_at IS NULL AND ph.person_id = a.person_id AND ph.platform = a.platform)"
soma sql "SELECT value, count(*) n FROM people, json_each(people.circles) WHERE people.deleted_at IS NULL GROUP BY value ORDER BY value"
```

A vocabulary fix is an estate-wide rename with the user's approval, never a
half-migrated pair of spellings.

**What changed on people already linked:**

```bash
people-sync changes --since <date of the last review>
```

Read-only. One row per changed value on a matched record or its scraped
profile (`field`, `old`, `new`, `at`, with the person): a new name or
spelling, a moved city, a new job, an edited birthday, a new photo
(`profile.avatar_sha256`), a dropped follow. A first fill (empty -> value)
is not listed; that is `promote`'s job. Each row is a question for the user,
applied through `reconcile`, `promote` or a direct edit with its provenance
edge, never in bulk. Re-scrapes are what surface profile changes: matched
records are revisited once they are 180 days stale.

## Step 8 - sync and report

Let soma's own sync move the rows (`soma background status`; a
foreground `soma sync` only when needed), verify a changed row on another
replica, then report in chat: per-source ingested/new counts, sources
skipped and why, matched/confirmed/created/ignored, cleanup, photos,
captures retained and verified, sweep counts, and anything left open.

## Communication history (`comm`)

Metadata only - who, when, channel, direction, and a call's outcome and
duration - from Apple Messages, Apple call history and the WhatsApp desktop
stores, into one soma stream per source plus two tables. Run it ad hoc on the
Mac whose stores are synced (Messages in iCloud, WhatsApp linked), with the hub
token in the environment:

```bash
people-sync comm import all                 # live stores past each checkpoint
people-sync comm import apple_calls --store <backup>/callhistory.db.gz          # a retained backup
people-sync comm import whatsapp_calls --store <backup>/whatsapp-callhistory.db.gz --lid-store <backup>/whatsapp-lid.db.gz
people-sync comm refresh                    # communication_summaries from the streams
people-sync comm coverage                   # import runs + local outbox
```

**Never body text.** Store connections carry a SQLite authorizer allowing only
metadata columns; a query touching a body, subject, preview, caption, name or
attachment column fails. Never work around it, and never read bodies with `imsg`
or `wa-db` for this job.

**Records** (one JSON object per event, verbatim in the stream):

| Field | Meaning |
|---|---|
| `v` | record version, 1 |
| `event_id` | native stable id and the dedupe key: Messages `guid`, CallHistory `ZUNIQUE_ID`, WhatsApp `ZSTANZAID` / `ZCALLIDSTRING` (stable across devices and backups) |
| `at` | event time, ISO-8601 UTC with milliseconds |
| `channel` | `imessage`, `sms`, `rcs`, `phone`, `facetime_audio`, `facetime_video`, `whatsapp` |
| `direction` | `outbound` / `inbound` |
| `conversation_kind` | `direct` / `group`, from the source chat (Messages `chat.style`, WhatsApp session type), never from member count; a call with no chat is `group` when the source lists several remote participants or a group id |
| `participant_ref` | sha256 of `tel:+E164` / `mailto:local@domain` (domain lowercased), else `whatsapp-lid:<id>` or `other:<handle>` (short codes); null for an outbound group message and for group calls |
| `person_id` | `people.id` the participant resolved to at import, or null |
| `chat_ref` | messages: sha256 of the native chat id |
| `outcome`, `duration_seconds`, `source_outcome` | calls: `answered` / `missed` (incoming, not answered) / `unanswered` (outgoing, not connected) / `unknown`, the source duration, and the raw outcome codes |
| `media` | WhatsApp calls: `audio` / `video` |
| `source_status` | WhatsApp messages: raw `ZMESSAGESTATUS` |
| `instance` | store label: `live`, or `backup:<folder>/<file>` |

**Rules the importer enforces.** A message lands only when proven: outgoing =
sent (Messages `is_sent`, no `error`; WhatsApp status 6 or 8, no error status),
incoming = present in the store. Reactions, service/system rows, status posts,
self-chat, failed sends, call rows inside chats and unknown types are counted
under `excluded`, never appended. A still-pending send younger than 7 days holds
the checkpoint so the next run re-reads it. A call is `answered` only on an
explicit source outcome (Apple `ZANSWERED=1`, WhatsApp outcome 0): Apple records
no outcome for outgoing calls, so those stay `unknown` whatever their duration.

**Identity.** Participants resolve only through confirmed `person_accounts`
links (`apple_contacts`, `google_contacts` through the CardDAV external id,
`whatsapp` LIDs and the number WhatsApp pairs with them), matched in memory
against the address books; an identity two people claim stays unresolved.
Unresolved participants stay opaque refs, and `refresh` re-resolves every ref
with today's links. A Mac with few contacts needs `PEOPLE_SYNC_ADDRESSBOOK_HOST`.

**Tables.** `communication_imports`: one row per run per source, `observed_rows
= accepted + excluded + duplicates` (enforced), with `coverage`
(`complete` / `partial` / `unavailable`) and `reason`. WhatsApp is always
`partial`: the desktop store holds only what the phone synced to it.
`communication_summaries`: one row per (person, channel), id
`<person_id>:<channel>`, every column a maintained summary owned by
`comm refresh` (direct events only; `coverage_start`/`coverage_end` from the
import rows). Never edit either by hand.

**Outbox.** Accepted events are kept in `$XDG_STATE_HOME/people-sync/comm/comm.sqlite`
before the hub sees them. A refused append leaves them there (the import row
says how many wait and why); the next `comm import` hands them over first.
Rerunning appends nothing; `--full` re-reads everything and counts known
events as duplicates.

**Verify a run:** per stream, `soma archive query --raw "SELECT count(DISTINCT
event_id) FROM stream('comm_<source>')"` equals the summed `accepted` of that
source's import rows once the outbox is empty; spot-check a few summaries
against the stores (counts only); `soma check` clean for both tables.

## Gotchas

- A cloud-synced folder can hand a parser an empty file (dataless
  placeholder). Zero entries from a file that clearly has content means copy
  it to a local path first.
- `new-person` is two writes. If the soma insert fails after the Notion
  page exists, it prints the orphaned page id; re-run with it rather than
  creating a second page.
- Never hard-delete a soma row; soft delete only, or the next sync
  resurrects it.
- LinkedIn's export drops connections it cannot render (rows with only a
  `Connected On` date); they are skipped with a warning by design.
- Every `soma sql` write costs seconds; the CLI batches its writes, and a
  loop of single-row writes is the bug to fix, not a reason to schedule.
