# contact-sync

Consolidates every source where you know people - Instagram, Facebook,
Snapchat, LinkedIn, Google Contacts, Apple Contacts - into a single
life-data people estate.

There is no daemon, no cron, no scheduler: this is a CLI run ad hoc, roughly
monthly. The deterministic half of a run lives here - parsing exports,
upserting the resolution ledger, conservative auto-matching, hashing and
storing profile photos. The judgment half - who a new handle actually is,
which circle they belong to, whether to ignore them - is a conversation with
an agent driving this CLI. That workflow is the `contacts-review` skill; this
repo is what it calls.

## Install

```bash
uv sync
```

The package is built with hatchling and installs editable into the project
venv, so `uv run python -m contact_sync ...` works from the repo root.

Runtime dependencies outside Python:

- The `life` CLI - the only write path to the people estate.
- The `gog` CLI - Google People API access for `ingest google` and Google
  photo fetches.
- macOS with Contacts data for `ingest apple` (reads the local AddressBook
  SQLite copies read-only).
- A Cloudflare API token with R2 write access for `photos store`.

## Commands

```
uv run python -m contact_sync <command>
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

Every ingest prints `{"new": N, "updated": N}`; `match` prints
`{"auto": N, "suggested": N, "left_pending": N}`. Re-running an ingest on the
same export is a no-op beyond refreshed `raw`, follow flags, and `last_seen`,
so a partial run is always safe to repeat.

## The ledger

`contact_records` holds one row per `(source, source_id)` ever seen, keyed
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
  `contacts-review` skill. A missing or stale export skips that source for
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

- `CF_API_TOKEN` - Cloudflare API token, used for R2 photo uploads. The
  account id is never hardcoded; it is derived from the token at runtime.
- `NOTION_API_TOKEN` - Notion integration secret, used only by `new-person`
  to create the People stub page whose id becomes the life-data row id.

Run anything that needs them through 1Password:

```bash
op run --env-file=.env.tpl -- uv run python -m contact_sync <command>
```

`CF_R2_BUCKET` optionally overrides the destination bucket for photos.

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
