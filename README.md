# people-sync

Consolidates every source where you know people - Instagram, Facebook,
Snapchat, LinkedIn, Google Contacts, Apple Contacts - into a single
soma people estate.

There is no daemon, no cron, no scheduler: this is a CLI run ad hoc, roughly
monthly. The deterministic half of a run lives here - parsing exports,
upserting the resolution ledger, conservative auto-matching, hashing and
storing profile photos. The judgment half - who a new handle actually is,
which circle they belong to, whether to ignore them - is a conversation with
an agent driving this CLI. That workflow is the `people-review` skill; this
repo is what it calls.

## Install

```bash
uv sync
```

The package is built with hatchling and installs editable into the project
venv, so `uv run python -m people_sync ...` works from the repo root.

The flake also exposes `packages.<system>.default`, the same `people-sync`
CLI packaged with plain `buildPythonApplication`, for installing it on a
machine (e.g. the Mac whose Chrome holds the social logins) with nix. There
is no daemon and no schedule: every run is an agent driving the CLI with a
person in the loop.

Runtime dependencies outside Python:

- The `soma` CLI - the only write path to the people estate.
- The `gog` CLI - Google People API access for `ingest google` and Google
  photo fetches.
- macOS with Contacts data for `ingest apple` (reads the local AddressBook
  SQLite copies read-only).
- A soma hub URL and a scoped file token for `photos store`.

## Commands

```
uv run python -m people_sync <command>
```

| Command | What it does |
|---|---|
| `ingest instagram --path <dir>` | Parses `followers.json` + `following.json` from an Instagram export directory into the ledger, setting the follow flags |
| `ingest facebook --path <file>` | Parses a Facebook `your_friends.json` export |
| `ingest snapchat --path <file>` | Parses a Snapchat `friends.json` export (the current `Friends` list only) |
| `ingest linkedin --path <file>` | Parses a LinkedIn `Connections.csv` from the full-archive export |
| `ingest google` | Enumerates Google Contacts via `gog` and pulls each contact's full People API record |
| `ingest apple` | Reads the local Apple Contacts databases |
| `ingest whatsapp --snapshot <sqlite> --media-dir <dir> --self-id <jid>` | Reads an operator-prepared, metadata-only WhatsApp snapshot read-only: active direct chats become pending records with their cached profile pictures; self, groups, status and broadcast rows are excluded, phone numbers never leave the machine |
| `propose --batch <label> <context.json> ... --output <proposals.json>` | Proposes identity clusters for the review page from names, handles, spellings, place/school/era cues, iMessage cues and shared phone numbers; unconnected entries become separate proposed people |
| `capture <source> --path <p>` | Retains a privacy-filtered export as an immutable capture without ingesting it |
| `captures [--state-dir <d>]` | Lists the locally cached captures and verifies each against the file service |
| `observations [--state-dir <d> \| --input <capture.json>] [--apply]` | Previews an immutable source-entry index; apply verifies retained bytes and inserts missing observations and provenance |
| `replay --input <capture.json> [--compare <prev>] [--output <p>]` | Parses a retained capture offline into a proposal, with no network and no estate writes |
| `match` | Auto-links unambiguous pending records to existing people and writes their `person_accounts` rows |
| `queue` | Prints the pending triage queue as JSON, suggestions first |
| `new-person --name <name>` | Creates the soma `people` row; with a Notion People data source configured, a Notion stub page comes first and its id becomes the row id |
| `photos store --person <id> --platform <p> --file <path>` | Stores a profile photo in R2 and appends a `person_photos` row, deduped by sha256 |
| `login <platform>` | Signs the browser's profile into a platform at human pace (TOTP / SMS / mailed codes via the wired commands); idempotent |
| `scrape <platform> [--max N]` | Visits pending and matched records' profile pages (paced, without a daily cap by default) and writes `people_sync_profiles` rows + pictures |
| `list facebook` | Scrolls the friends list and gives the export's name-only records their profile handles (unique exact names only) |
| `list partiful` | Clicks through every mutual on partiful.com/mutuals and writes a ledger record + profile row per person |
| `list partiful-events [--since DATE] [--refresh] [--event-id ID ...]` | Walks the guest lists of past events the user went to or hosted, skipping events whose guest list is already retained (`--refresh` re-walks them), and prints each walked event with its guest and record counts |
| `list strava` | Followers and following of the signed-in athlete into the ledger |
| `list spotify` | Followers and followed user accounts, with totals checked against the profile |

Every ingest retains its input first: the export, contact page or WhatsApp
snapshot is validated, uploaded as a capture under `profiles/<source>/captures/`,
read back and cached under the state dir before any parsing or ledger write, and
every record and profile row written from it carries an `imported_from`
provenance edge to that exact file. A capture that cannot be retained stops the
ingest. `replay` re-parses a capture offline so a parser fix can be checked
against the retained input without visiting the source again; its output is a
proposal, never an automatic change to the ledger, the cache or a person.

Every ingest prints `{"new": N, "updated": N}`; `match` prints
`{"auto": N, "suggested": N, "left_pending": N}`. Re-running an ingest on the
same export is a no-op beyond refreshed `raw`, follow flags, and `last_seen`,
so a partial run is always safe to repeat.

## The ledger

`people_sync_records` holds one row per `(source, source_id)` ever seen, keyed
`<source>:<source_id>`, with a status of `pending`, `matched`, or `ignored`.
That memory is what makes runs incremental: a `matched` record is never
re-asked and an `ignored` one never resurfaces, so a monthly run only ever
surfaces what is genuinely new.

## Data layout

Contact exports live under `data/`, which is gitignored and stays that way -
it is raw personal data and no export file is ever committed.

```
data/instagram/followers.json, following.json
data/facebook/your_friends.json
data/snapchat/friends.json
data/linkedin/Complete_LinkedInDataExport_<date>/Connections.csv
```

Google and Apple need nothing on disk; they are read live.

## Manual steps

These cannot be codified:

- **Per-platform export downloads.** Instagram, Facebook, Snapchat, and
  LinkedIn only hand out contact lists through a click-ops request flow, and
  the archives take minutes (Meta) to a day (LinkedIn) to arrive. The exact
  per-platform procedure and where each file lands is in the
  `people-review` skill. A missing or stale export skips that source for
  the run; it never blocks it.
- **Full Disk Access for `ingest apple`.** The first read of the Apple
  Contacts databases triggers a macOS TCC prompt; the invoking terminal needs
  Full Disk Access granted in System Settings before the ingest can see them.
- **Google OAuth consent.** `gog` holds its own credentials; a first run (or
  an `invalid_grant` after a revoked token) needs an interactive
  re-authorization in a human's own terminal.

## Secrets

`.env.tpl` is the canonical manifest, holding 1Password `op://` references
only and no plaintext:

- `SOMA_HUB_URL` and `SOMA_HUB_TOKEN` - the soma file service URL and
  a dedicated client token with read/write grants for `photos/people/`,
  `photos/records/`, and `profiles/`.
- `NOTION_API_TOKEN` - dedicated Notion connection token for People stub insertion
  and the relation reads performed by `reconcile merge`.

Run anything that needs them through 1Password:

```bash
op run --env-file=.env.tpl -- uv run python -m people_sync <command>
```

Files are uploaded and read through `/v1/files/<key>`. Soma owns the
retained photos and source snapshots; this client never holds provider
storage credentials. Existing `r2_key` values remain stable references.

## Browser and login configuration

`people-sync list spotify` reads the signed-in user's followers and following
pages. It checks the displayed totals, ingests user accounts with both direction
flags, and excludes artist pages. `people-sync scrape spotify` then saves each
pending user's profile header and profile picture, when present.

`people-sync scrape venmo` reads existing ledger handles from signed-in
personal profile pages. It captures identity, friendship status and the profile
picture; payment feeds, contact details and authentication state are excluded.
The web profile exposes a friend count, not a complete friend directory.
Venmo's social-data export can contain activity without a friends list;
verify its structure and never treat payment activity as a friend inventory.

`scrape` and `login` drive a Chrome over CDP. Where it is and how it is
signed in come from options or environment variables; nothing here is ever
stored by the app.

| Option / variable | Meaning |
|---|---|
| `--endpoint` / `PEOPLE_SYNC_CDP_ENDPOINT` | `host:port` of a Chrome started with its own `--remote-debugging-port` (a dedicated profile) |
| `--data-dir` / `PEOPLE_SYNC_CHROME_DATA_DIR` | Chrome data dir whose `DevToolsActivePort` names the port; default is Chrome's own data dir |
| `--approve-command` / `PEOPLE_SYNC_CDP_APPROVE_COMMAND` | Command that approves the browser's remote-debugging prompt on hosts that show one; started detached before connecting, never with `--endpoint` |
| `PEOPLE_SYNC_DAILY_CAPS` | JSON object `{"<platform>": <int>}` with optional per-platform daily caps; omitted platforms have no daily cap; malformed values fail at startup |
| `PEOPLE_SYNC_CREDENTIAL_COMMAND` | Run as `sh -c "<command>" people-sync-login <platform>`; prints `{"username": ..., "password": ..., "totp": ...}` (`totp` = current code or null) |
| `PEOPLE_SYNC_EMAIL_CODE_COMMAND` | Same invocation; prints the newest one-time code from email that arrived after `$PEOPLE_SYNC_CODE_AFTER` (ISO-8601 UTC, set by `login` to the moment it submitted the credentials), or nothing if none has yet (polled every 5-10 s for up to 90 s) |
| `PEOPLE_SYNC_SMS_CODE_COMMAND` | Same, for a code delivered by SMS to the machine running the job |

Every command has 60 s; a non-zero exit or a timeout halts the login with a
screenshot and a reason that names only the variable. Command output is used
and dropped, never logged.

## Installing for a user

`flake.nix` exports `homeModules.default`. A home-manager configuration
enables it and supplies only the facts no vendor could know:

```nix
programs.people-sync = {
  enable = true;
  endpoint = "127.0.0.1:9222";            # the Chrome the browser commands attach to
  credentialCommand = "my-login-secrets"; # optional: `login` types; unset = verify only
  notion.peopleDataSource = "<data_source_id>";
  notion.relations.Gifts = [ "<data_source_id>" "<relation property id>" ];
};
```

That installs `people-sync` wrapped with those settings and exposes the
agent runbook at `$XDG_DATA_HOME/people-sync/skills/people-sync`, ready to be
linked into an agent's skill catalog (`~/.claude/skills`, `~/.agents/skills`).
Operator commands (`reconcile`, `google-cleanup`, `review`, `promote`) are
subcommands of the installed CLI; nothing is run from a checkout.

## Development

```bash
just test    # pytest
just check   # ruff check + format check
just fmt     # ruff format + fix
```

Set `PEOPLE_SYNC_CDP_TARGET` to an existing CDP page target to use a specific
tab across login, list and scrape calls. The caller owns that tab: the CLI
detaches on exit without closing it, preserving tab-scoped sessions. An
invalid target fails; it never selects another tab.

For a coordinated scrape, create and group the tabs first, then pass each ID:

```bash
people-sync scrape instagram --max 100 --endpoint <host:port> \
  --target <tab-1> --target <tab-2> --target <tab-3> --target <tab-4> --target <tab-5> --target <tab-6> \
  --target <tab-7> --target <tab-8> --target <tab-9> --target <tab-10>
```

One process owns the queue, with up to ten profiles in flight and serialized
database writes. `--max` applies to the whole run; omit it to exhaust the queue.
There is no daily cap unless explicitly configured. Attempts are recorded
before navigation, including failures. Page starts are staggered
by the normal 8-25 second gap divided by the number of tabs; every 25 attempts
adds the full 2-5 minute break to the shared queue. Concurrent invocations using
the same platform and state path are refused.

Instagram proceeds once its profile header and matching profile JSON have
arrived, preserving captured structured fields and the best available avatar.
If the JSON never arrives, a bounded 24-second wait retains the existing DOM
fallback; an incomplete header stays pending. Logs report the actual data wait.

Profile captures are archived before parsing, photo fetching or database updates.
Malformed extractor results and captured response bodies survive parser failures.
Partiful mutuals are archived before leaving each profile. An archive upload
failure stops the run. This protects received captures, within the existing
field/response allowlists; it cannot recover older omissions, responses that
never arrived, or a process killed before it could upload.

Coordinated tabs share a stop signal. Source HTTP 401/403/429 responses, API
failure/challenge envelopes, warning text, login/checkpoint redirects, browser
loss, or unexpected failures halt the queue. Already-loading tabs stop loading;
no queued record is retried automatically. Each tab is checked before its first
navigation and while storage or pacing is in progress. A halt preserves pending
records and screenshots for review. A new run is an explicit operator action.

## Approval-gated relationship removal

`people-sync unfollow` and `people-sync unfollow --json` remain read-only
reports. Removing a relationship is a separate, exact batch:

```sh
people-sync unfollow prepare --platform instagram --actor example_operator \
  --record-id instagram:example_target --endpoint localhost:9222
people-sync unfollow apply /path/to/private/plan.json --endpoint localhost:9222
people-sync unfollow journal
people-sync unfollow resume /path/to/private/plan.json --endpoint localhost:9222
```

`prepare` only reads the selected accounts. It verifies the signed-in actor,
records stable target IDs and reports which relationships are already absent.
It never clicks a removal, requests approval, or writes to the estate.
Repeat `--record-id` for each intended target. There is no implicit "all".
Plans bind the ignored ledger rows, operation, signed-in account, canonical
profile URLs, observed numeric IDs, full SHA256 digest and one-hour expiry.
For offline planning, `plan --operation unfollow` accepts independently observed
IDs through repeated `--remote-id instagram:example_target=900001`.
Retained ledger IDs are included automatically. Conflicting or duplicate IDs
refuse planning. Applying a handle-only plan is refused before approval;
run `prepare` first. A bound ID must match the live profile before every click.

| Platform | Explicit operation | Executable adapter |
| --- | --- | --- |
| Instagram | `unfollow` | Strict English profile/dialog controls |
| Facebook | `unfriend` | Read-only preparation; apply refuses |
| LinkedIn | `remove-connection` | Unsupported; apply refuses |
| Venmo | `remove-friend` | Strict profile/menu controls; Unfriend submits directly |

Facebook final submission is disabled while control ownership and document
binding remain unverified. Its read-only preparation remains available.
Facebook uses the authenticated numeric account ID for `--actor`; Instagram
and Venmo use the exact account handle. Read-only profile and menu checks have
live validation. Final removal and the subsequent live postcondition have
**not been exercised against a real account**; synthetic tests cover them.
Authenticated viewer identity, stable target identity and exact controls must
all match. Missing or ambiguous controls, a login wall, a different
account, redirects, expired approval or a source warning halt the batch.
There are no guessed selectors or API fallbacks. Friendship/connection removal
is never substituted for unfollowing, or vice versa.

`apply` presents every operation and URL on the controlling terminal, then
requires the human to type `REMOVE <count> <platform> <operation> <full digest>`
exactly. No `--yes`, `--force`, environment approval or piped input exists.
Agents must leave this final confirmation to the human. A TTY interlock cannot
authenticate who controls a terminal; automation must never type the phrase.
The selected batch is rechecked after approval and before each action.
Use `--target` (or `PEOPLE_SYNC_CDP_TARGET`) for a caller-owned, grouped tab;
the caller closes that tab. Otherwise the CLI creates and closes its own tab.

Private operational state lives in `$XDG_STATE_HOME/people-sync/unfollow`
(default `~/.local/state/people-sync/unfollow`): immutable `0400` plans, read-only observations and a
`0600` SQLite journal under `0700` directories. Keep this state for recovery.
It holds exact identities, URLs and outcomes, never credentials or full-page
captures. An attempt is durably recorded **before** any click. Warnings stop
the serial queue, which uses the scraper's Pacer and platform run lock.

Only a positive absence observation from a newly loaded document permits local
completion. Instagram writes `i_follow=0`. Friendship removal preserves the
independent follow flag and writes `raw.people_sync_relationship` with the
operation, actor, absent state, approved digest and verification time. The
default queue excludes that completed relationship. A subsequent source import
can replace the observation with fresh source data.
The CLI compares the current row before writing through `soma`, preserves its
profile URL and ignored status, and adds
`evidence_of` provenance referencing the approved review batch. Lost storage
replies can be repaired idempotently. Recovery preserves an acknowledged ledger
timestamp and skips that completed write, even when provenance is interrupted
more than once.

After interruption or an uncertain result, use `resume` with the **original
plan**, even if expired. Its `VERIFY ... <digest>` prompt authorizes verification
and local bookkeeping only. It never repeats a click, and a new plan cannot
bypass an existing attempt. If the relationship is still present, it remains
unknown and stops. Unattempted targets require a new plan and approval. A partial
resume exits 2; refusals/errors exit 1; interruption exits 130. Success prints
verified and unattempted counts. Unsupported batches never report completion.

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


### Keep public social accounts outside personal relationships

Create or reuse an owner in `organizations`, `public_figures`, or
`music_festivals` using your installed `soma` CLI and catalog conventions.
Then review and apply:

```sh
people-sync reconcile public <record-id> --organization <organization-id>
people-sync reconcile public <record-id> --organization <organization-id> --apply
```

Use `--figure` or `--festival` for the other owner types. The optional public
account catalog consists of `public_accounts(record_id, organization_id,
public_figure_id, music_festival_id)` with exactly one owner. Account identity
and cached profile details stay in the source ledger and profile table.
The ledger needs the `public` status in its catalog. These are user-managed
tables, created through Soma, not repository configuration.

Public accounts stay out of personal triage, matching, and unfollow suggestions,
including after re-import. Rerun the same command after an interrupted write.
It refuses reassignment and deleted rows; resolve those explicitly through Soma
Data. Classification is a review decision, never inferred from follower counts.
Employment is independent of organizations.

## Source observation index

The shared `people_sync_profiles` table remains the latest profile cache. The
optional `people_sync_observations` table addresses each original row in a
validated capture independently of resolved people or account IDs. Identical
names at different ordinals stay separate. A scope row with a null entry ordinal
preserves empty, failed and truncated acquisitions. Platform-specific values
remain in the original file, with hashes and addresses in the index.

Create this operator-owned table through Soma before applying:

```sh
soma table create people_sync_observations 'capture_key:text!' 'source:text!' \
  'kind:select!(profile|export|list|contacts)' 'captured_at:datetime!' \
  'completeness:select!(complete|privacy-filtered|extracted-only|partial|legacy-parsed-only)' \
  'capture_sha256:text!' 'payload_sha256:text!' 'scope:text!' \
  'entry_ordinal:int' 'entry_sha256:text!'
```

Describe and catalog the table for your estate, mark its value columns immutable,
and refresh its catalog documentation. `id` is a deterministic `psobs:` SHA256
of capture key, scope and entry ordinal. `capture_sha256` hashes the canonical
envelope; `payload_sha256` hashes its payload. `entry_sha256` hashes the addressed
entry, or its scope summary when the ordinal is null. The source and completeness
values come from the capture envelope, not a claim of platform-wide coverage.

Run `people-sync observations` during an on-demand review; it reads the local
capture cache without networking or writing. Use `--apply` to check each capture
against the retained file service and write observations and `imported_from`
provenance in batches. The usual Soma CLI and file-service credentials apply.
There is no scheduler and collectors do not automatically populate this index.
Repeat apply after an interrupted run: existing observations are checked, missing
edges repaired, and deleted rows held. Conflicting content is refused. Apply may
partially succeed; its JSON report and exit status identify unverified captures
and invalid inputs. An empty input directory is an error.

This indexes only retained, validated envelopes. Legacy extracted files, missing
originals, inputs excluded before capture, and unreceived responses cannot be
recovered by indexing. No profile refresh, identity merge, follow action or
inventory reconstruction occurs.
