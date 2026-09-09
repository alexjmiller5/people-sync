# people-sync

Consolidates every source where you know people - Instagram, Facebook,
Snapchat, LinkedIn, Google Contacts, Apple Contacts - into a single
life-data people estate.

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

- The `life` CLI - the only write path to the people estate.
- The `gog` CLI - Google People API access for `ingest google` and Google
  photo fetches.
- macOS with Contacts data for `ingest apple` (reads the local AddressBook
  SQLite copies read-only).
- A life-data hub URL and a scoped file token for `photos store`.

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
| `match` | Auto-links unambiguous pending records to existing people and writes their `person_accounts` rows |
| `queue` | Prints the pending triage queue as JSON, suggestions first |
| `new-person --name <name>` | Creates a Notion People stub page, then the life-data `people` row using that page id |
| `photos store --person <id> --platform <p> --file <path>` | Stores a profile photo in R2 and appends a `person_photos` row, deduped by sha256 |
| `login <platform>` | Signs the browser's profile into a platform at human pace (TOTP / SMS / mailed codes via the wired commands); idempotent |
| `scrape <platform> [--max N]` | Visits pending and matched records' profile pages (human-paced, daily-capped) and writes `people_sync_profiles` rows + pictures |
| `list facebook` | Scrolls the friends list and gives the export's name-only records their profile handles (unique exact names only) |
| `list partiful` | Clicks through every mutual on partiful.com/mutuals and writes a ledger record + profile row per person |
| `list strava` | Followers and following of the signed-in athlete into the ledger |
| `list spotify` | Followers and followed user accounts, with totals checked against the profile |

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

- `LIFE_HUB_URL` and `LIFE_HUB_TOKEN` - the life-data file service URL and
  a dedicated client token with read/write grants for `photos/people/`,
  `photos/records/`, and `profiles/`.
- `NOTION_API_TOKEN` - Notion integration secret, used only by `new-person`
  to create the People stub page whose id becomes the life-data row id.

Run anything that needs them through 1Password:

```bash
op run --env-file=.env.tpl -- uv run python -m people_sync <command>
```

Files are uploaded and read through `/v1/files/<key>`. Life Data owns the
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
| `PEOPLE_SYNC_DAILY_CAPS` | JSON object `{"<platform>": <int>}` merged over the built-in per-platform daily caps; malformed values fail at startup |
| `PEOPLE_SYNC_CREDENTIAL_COMMAND` | Run as `sh -c "<command>" people-sync-login <platform>`; prints `{"username": ..., "password": ..., "totp": ...}` (`totp` = current code or null) |
| `PEOPLE_SYNC_EMAIL_CODE_COMMAND` | Same invocation; prints the newest one-time code from email that arrived after `$PEOPLE_SYNC_CODE_AFTER` (ISO-8601 UTC, set by `login` to the moment it submitted the credentials), or nothing if none has yet (polled every 5-10 s for up to 90 s) |
| `PEOPLE_SYNC_SMS_CODE_COMMAND` | Same, for a code delivered by SMS to the machine running the job |

Every command has 60 s; a non-zero exit or a timeout halts the login with a
screenshot and a reason that names only the variable. Command output is used
and dropped, never logged.

## Scripts

One-offs in `scripts/`, run directly with `uv run python scripts/<name>.py`:

- `google_cleanup.py` - clears labels and org fields from Google Contacts
  once life-data demonstrably holds the replacement (every label already a
  circle, every org already a `person_employments` row). Dry run by default;
  `--apply` writes, `--selftest` checks the decision logic offline.
- `migrate_accounts.py` - migrates the flat handle columns on `people` into
  `person_accounts` rows, keeping source values verbatim.
- `migrate_circles.py` - migrates `tags`, `company`, and `when_we_met` values
  into the `circles` vocabulary. Modes: `worksheet` (propose), `apply`,
  `sideeffects`, `reconcile` (prove zero loss).
- `notion_people_pull.py` - snapshots the Notion People database to
  `data/notion_people_snapshot.json`.

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
  --target <tab-1> --target <tab-2> --target <tab-3> --target <tab-4>
```

One process owns the queue, with up to four profiles in flight and serialized
storage writes. `--max` applies to the whole run. Attempts consume one shared
daily budget before navigation, including failures. Page starts are staggered
by the normal 8-25 second gap divided by the number of tabs; every 25 attempts
adds the full 2-5 minute break to the shared queue. The cap is an operator
precaution, not a platform-published allowance. Concurrent invocations using
the same platform and state path are refused.

Instagram proceeds once its profile header and matching profile JSON have
arrived, preserving captured structured fields and the best available avatar.
If the JSON never arrives, a bounded 24-second wait retains the existing DOM
fallback; an incomplete header stays pending. Logs report the actual data wait.

Coordinated tabs share a stop signal. Source HTTP 401/403/429 responses, API
failure/challenge envelopes, warning text, login/checkpoint redirects, browser
loss, or unexpected failures halt the queue. Already-loading tabs stop loading;
no queued record is retried automatically. Each tab is checked before its first
navigation and while storage or pacing is in progress. A halt preserves pending
records and screenshots for review. A new run is an explicit operator action.
