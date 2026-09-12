# Reproducible People Captures

## Approved scope

Implement the capture/replay recommendation approved in the conversation: preserve inputs before field extraction, verify retention, record observation and code identity, replay offline, pin provenance to immutable evidence, recover existing files honestly, and add local WhatsApp context to the private review. This supplements the People DB design without changing its identity or preservation rules.

## Contract

1. Capture -> verified retained copy -> deterministic parse -> proposed changes -> separately approved identity decisions.
2. No canonical person merges, circle edits, address-book cleanup, or birthday-reminders changes are authorized by this work. LinkedIn bulk collection stays paused. Nothing is scheduled.
3. Retained files use the existing Life Data file service and People Sync's existing `profiles/`, `photos/records/`, and `photos/people/` grants. No provider credentials, new service, or per-platform raw tables.
4. Capture files are immutable observations with unique keys, UTC millisecond observation times, source, source/account identity when available, capture kind/format, collector code fingerprint, payload SHA-256, completeness and exclusions. Identical photo bytes stay deduplicated. Different observations never overwrite one another.
5. The app keeps a private local capture index/cache under its standard state directory, outside source control. Retained archive objects remain authoritative; the local index is recoverable operational state, not a second provenance system. Existing `provenance` edges with `from_kind=takeout` and `from_ref=<retained file key>` make successfully ingested observations discoverable in the estate.
6. Upload/read-back hash verification precedes parsing and any ledger/profile mutation. Failed capture/upload/verification halts the source. Failed parses keep the capture and an explicit result. Legacy captures without metadata are readable but never claimed to be checksum-verified against a historical expected hash.
7. Capture source profile DOM/API inputs upstream of field selection, not merely selected fields. Scope DOM to profile surfaces, remove executable/session/form material, and label the exact capture boundary. API and export privacy filtering is explicit, versioned and reported. Do not claim excluded or never loaded fields are recoverable. Old selected-field captures remain `extracted-only`; old parsed caches remain `legacy-parsed-only`.
8. Credentials, unrelated payment information, phone numbers, email addresses and street addresses must not enter People Sync retained payloads or Life Data rows. Existing verbatim export files may remain intact in private local recovery storage; where a source contains prohibited fields, archive the privacy-filtered input and record its exclusion policy. Never silently widen source allowlists. Logs name source, ordinal and reason, never source contents or credentials.
9. Pure offline replay uses a local capture with no source requests, image fetches, Notion calls or Life Data writes. It returns a deterministic proposal containing source capture identity/hash, parser code fingerprint, parsed records/profile and explicit failure/limitations. Replaying changed code produces a comparison, never silently applies changes. Human approvals remain separate and untouched.
10. Promotion provenance must reference the exact retained source capture when one is available, including a parser fingerprint in the capture/replay artifact. Existing legacy provenance is preserved. Missing archive evidence is not invented. No source attributes belong on provenance edges.
11. Source-list observations retain pagination/page ordinals and observed completion/truncation, before filtering or deduplication. An unfinished list never implies removal or a complete inventory.

## Components

- `captures.py`: versioned capture envelope, serialization/hash verification, private local index/cache, retained-file write/read. Reuses `photos.put_object/get_object`; no second storage client.
- `replay.py`: pure dispatch for source formats, legacy profile captures and preserved export bytes; JSON proposal/diff generation. Existing parser functions remain the source of field interpretation.
- Existing ingestion/list/scrape functions: acquire a capture before parsing and pass its immutable key into writes. Profile capture includes safe DOM inputs and existing extractor results for backward-compatible parsers. DOM inputs are retained for corrected offline extractors; the replay report states whether it replayed extracted fields or source inputs.
- `whatsapp.py`: snapshot-based desktop metadata/photo ingestion. No browser, new linked device, live database writes, or message sends.
- Existing private review builder: consumes prepared additional WhatsApp evidence without changing the review's approval model.

## WhatsApp boundary

Use the installed desktop app's read-only snapshot path, including WAL. Ingest actual direct-chat counterparts and their available cached profile pictures; do not automatically create people from group rosters, status feeds, or the self chat. Source references prefer opaque native LIDs; phone-form identifiers must remain local and must not leak through keys, URLs, paths or metadata. A local opaque mapping is acceptable for phone-only records and is preserved as private state; do not hash unsalted phone numbers and call them anonymous. Capture safe source names, opaque IDs, metadata timestamps and relevant group references without message text. Optional usernames are aliases only when present. Cache thumbnails are explicitly low-resolution observations. Missing files and unavailable usernames are reported, not fabricated. All new records remain pending; existing matching decisions and photos are preserved. Review grouping remains a proposal, never name-only identity proof.

## Recovery

Preserve existing review data, proposals, exports, profile rows and ledger rows before changes. Inventory local recovery sources and referenced remote files. Download and verify existing remote files, recording bytes/hash and failure classes without assuming a database link proves storage. Hash checks against known photo hashes establish integrity; a new hash of a legacy JSON establishes today's recovered bytes only.

Replay retained captures offline into a private report, without applying canonical changes. Missing originals are not reconstructed from parsed columns. Parsed-only backups are labeled accordingly. Prefer recovery to new browsing. Revisit only relevant missing profiles when existing evidence cannot answer the review question and the platform permits access. New visits are new observations. LinkedIn remains excluded. Rebuild the private review with additional evidence while retaining the original page/data and its browser-save keys. Do not manufacture approval for a changed evidence snapshot.

## Verification

Synthetic tests cover deterministic hashes/replay, tampering, malformed input retained before parse, failed upload blocking writes, legacy classification, privacy exclusions, safe DOM collection, source-list incompleteness, WhatsApp snapshot/WAL and photo path boundaries, and decision preservation. Run focused red/green tests, mutation checks, full pytest and ruff, Nix package checks for installed behavior, then an operational offline replay against retained private inputs. No personal fixtures enter git.

## Non-goals

No full browser/session dump, archive server, scheduler, new raw table per source, historical-data fabrication, automatic identity resolution, full WhatsApp message archive, or restart of every social scrape.
