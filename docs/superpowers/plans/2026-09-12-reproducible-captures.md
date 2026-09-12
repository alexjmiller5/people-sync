# Reproducible People Captures Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Make source evidence replayable, recover retained inputs honestly, and add WhatsApp context without changing approved identities.

**Architecture:** One versioned immutable capture envelope uses the existing retained-file service, with an operator-local index/cache. Pure parsers produce offline proposals; the existing provenance table links facts to exact retained observations. Existing source adapters and the private review remain the integration boundaries.

**Tech Stack:** Python >=3.13, standard library, existing httpx/structlog/websockets, pytest and ruff; existing installed Life Data interface.

**Spec:** `docs/superpowers/specs/2026-09-12-reproducible-captures-design.md`

## Global Constraints

- No canonical person merges, circle edits, address-book cleanup, or birthday-reminders changes are authorized by this work. LinkedIn bulk collection stays paused. Nothing is scheduled.
- Retained files use the existing Life Data file service and People Sync's existing `profiles/`, `photos/records/`, and `photos/people/` grants. No provider credentials, new service, or per-platform raw tables.
- Credentials, unrelated payment information, phone numbers, email addresses and street addresses must not enter People Sync retained payloads or Life Data rows.
- Logs name source, ordinal and reason, never source contents or credentials.
- Pure offline replay uses a local capture with no source requests, image fetches, Notion calls or Life Data writes.
- Human approvals remain separate and untouched.
- No personal fixtures enter git.

## File map

`src/people_sync/captures.py` owns capture envelopes, local private indexing and verified retention. `src/people_sync/replay.py` owns pure replay/diffs. `src/people_sync/whatsapp.py` owns the new snapshot adapter. Existing `cli.py`, `photos.py`, `sources.py`, `parsers.py`, `ledger.py`, `promote.py` and `scrape/*` change only at their relevant boundaries. `scripts/build_review.py` remains a renderer, not a source matcher. Private operational artifacts live outside git. No new dependencies unless a concrete blocker is reported.

### Task 1: Versioned retained captures and pure offline replay

**Files:** Create `src/people_sync/captures.py`, `src/people_sync/replay.py`, `tests/test_captures.py`, `tests/test_replay.py`. Modify `src/people_sync/photos.py`, `src/people_sync/cli.py`, relevant `tests/test_photos.py`/`tests/test_cli.py`.

**Interfaces:** Produce `captures.build_capture(source, kind, payload, *, record_id=None, captured_at=None, completeness="partial", exclusions=()) -> dict`, `captures.retain(capture, *, state_dir=None) -> str`, `captures.validate(capture) -> dict`, `captures.code_fingerprint() -> str`, and `replay.replay_capture(capture) -> dict`. Preserve `photos.archive_profile(platform, record_id, raw_eval, captured, *, context=None) -> str` and legacy payload compatibility. New optional context captures source DOM without breaking existing callers. CLI adds `capture` for supported file inputs, `captures` for local inventory/verification, and `replay --input PATH [--compare PATH] [--output PATH]`, with no apply mode.

- [ ] Write a failing synthetic replay test first:
  ```python
  def test_replay_is_offline_and_deterministic(monkeypatch):
      from people_sync import captures, replay, photos, lifedata
      def forbidden(*args, **kwargs):
          raise AssertionError("offline replay attempted an external operation")
      monkeypatch.setattr(photos, "get_object", forbidden)
      monkeypatch.setattr(photos, "fetch_url_photo", forbidden)
      monkeypatch.setattr(lifedata, "sql", forbidden)
      c = captures.build_capture("spotify", "profile", {"eval": {"name": "Example", "path": "/user/example", "avatar": None}, "captured": []}, record_id="spotify:example", captured_at="2026-01-01T00:00:00.000Z", completeness="extracted-only")
      assert replay.replay_capture(c) == replay.replay_capture(c)
      assert replay.replay_capture(c)["profile"]["display_name"] == "Example"
  ```
  Confirm fixture keys against the existing Spotify parser; make the fixture synthetic and representative rather than weakening the assertion.
- [ ] Run `uv run pytest tests/test_captures.py tests/test_replay.py -q`, confirm the missing-interface failure; record it.
- [ ] Implement the envelope with `schema_version`, `capture_id`, `source`, `kind`, `record_id`, `captured_at`, `collector_fingerprint`, `completeness`, `exclusions`, `payload`, `payload_sha256`. Fingerprint installed package source bytes deterministically with hashlib, not a required git executable. Hash canonical JSON payload bytes; encode verbatim file bytes as base64 with original filename/format where needed. Validate enums, hash, timestamps, source and shape before use. `retain` uploads a unique `profiles/<source>/captures/<capture_id>.json`, reads it back and compares the full uploaded bytes/hash, then atomically writes a 0600 local copy/index under XDG state. The index must survive parallel writers; one file per capture is sufficient. Failed upload/read-back/index writes raise `ArchiveError` with no credential-bearing exception text.
  ```python
  encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
  digest = hashlib.sha256(encoded).hexdigest()
  ```
- [ ] Dispatch replay through existing platform `parse(eval_result, captured)` functions, excluding volatile `Profile.raw` only from the normalized proposal comparison, not from retention. Preserve original record ID, observation time and input hash. Return parser fingerprint and limitations; legacy captures are explicitly `extracted-only`/unverified. Malformed and unavailable captures produce explicit results rather than empty valid profiles. Support offline export replay through preserved bytes and existing file parsers using private temporary files, with original ordinals/skips reflected in the report. No network/write-mode option in replay.
- [ ] Add tamper, write/read-back failure, identical-clock unique-key, malformed retained input, legacy classification, CLI JSON/diff, no-external-effects and private-path tests. Update archive tests to validate real stored envelope behavior. Run focused tests, mutate a hash/field mapping and observe failure, then restore and run full pytest/ruff.
- [ ] Commit the verified Task 1 changes and provide red/green commands/output, interfaces, changed paths and any concerns in the report.

### Task 2: Archive before extraction across source entry points

**Files:** Modify `src/people_sync/cli.py`, `sources.py`, `parsers.py`, `scrape/run.py`, `scrape/partiful.py`, `scrape/facebook.py`, `scrape/spotify.py`, `scrape/strava.py`, `scrape/venmo.py`; create `src/people_sync/scrape/snapshot.py` and `tests/test_capture_ingest.py`; extend relevant source/list/scrape and JS tests.

**Interfaces:** Consume Task 1 capture/retention/replay functions. Produce safe source-document collection in `scrape.snapshot` and pure source parsing helpers; existing command names and existing parser signatures remain compatible. Records may carry an optional `capture_key` for provenance, never source attributes disguised as provenance.

- [ ] Write a failing E2E-like ingest test using a temporary synthetic export and a fake retained-file service. Inject an archive failure and assert no ledger rows are written; then archive successfully and assert skipped malformed input survives in the retained file.
  ```python
  export.write_text('{"friends_v2":[{"name":"Example Person"},{"timestamp":7}]}')
  # Invoke the real CLI main with ingest facebook --path <export>.
  # An upload failure must exit nonzero and leave the fake estate empty.
  # A successful capture must retain both original entries before parsing.
  ```
- [ ] Run the focused new test and record the expected missing archive-before-ingest failure.
- [ ] Add capture-before-parser hooks for supported file ingests. Preserve source files locally; for LinkedIn, remove prohibited Email Address values before service retention and declare the versioned exclusion. Ensure parsed ledger raw cannot reintroduce them. Do not make skipped/malformed rows disappear from retained permitted source input.
- [ ] Separate Google/Apple acquisition from interpretation enough to retain privacy-filtered source-shaped input before names/primary selection/birthday conversion. Google list pagination retains resource IDs and page completion, never phone/email list summaries. Apple snapshot/query retains original permitted scalars and raw birthday timestamp with explicit timezone conversion inputs, not a transformed birthday presented as source raw. Every source fetch failure is an explicit partial outcome.
- [ ] Capture relevant profile DOM before running field-selecting JavaScript. Sanitize executable/hidden/form/session material and scope selectors to profile surfaces. Venmo stays on its strict selected `otherUser` field allowlist with no full DOM/Next.js/network dump, classified `privacy-filtered`. Failed DOM collection is an explicit partial capture, not a silent complete success. Store existing extractor output alongside source inputs for existing parsers; preserve navigational responses when later extraction fails.
- [ ] Retain list-page observations before per-entry filtering/deduplication for Facebook/Spotify/Strava/Partiful. Save page/scroll ordinal, source scope and complete/truncated outcome. Preserve virtualized entries incrementally. Archive errors stop before new ledger/profile writes. Ensure Partiful source DOM is saved on direct and mutual-list paths.
- [ ] Run focused ingestion, source, list, archive and `test_dom_js` tests; perform a mutation check on capture ordering/privacy exclusion; full pytest/ruff; commit and report.

### Task 2A: File and address-book capture-before-parse

Task 2 is executed as 2A, 2B and 2C, with one worker and a review gate per subtask. Its requirements above still bind all three; no source is deferred or omitted.

**Files:** `src/people_sync/cli.py`, `captures.py`, `replay.py`, `sources.py`, `parsers.py`, the optional field on `ledger.Record`; `tests/test_capture_ingest.py`, relevant capture/replay/source/CLI tests.

**Interfaces:** Keep `fetch_google() -> list[Record]` and `fetch_apple() -> list[Record]` callable; add `collect_google() -> dict`, `collect_apple() -> dict`, `parse_google(payload: dict) -> list[Record]`, `parse_apple(payload: dict) -> list[Record]`. Collections are privacy-filtered source-shaped payloads with original row ordinals and explicit page/database completeness or acquisition-failure entries. Add `Record.capture_key: str | None = None`; it is not serialized into ledger raw or a table column. Register contacts replay through the same pure parsers.

- [ ] Add a failing real-CLI test that retains a synthetic Facebook document with one valid and one malformed entry; archive failure must precede parser/ledger calls. The fake retained-file service records actual bytes and returns them for verification.
  ```python
  export.write_text('{"friends_v2":[{"name":"Example Person"},{"timestamp":7}]}')
  cli.main(["ingest", "facebook", "--path", str(export)])
  assert len(retained_capture_entries) == 2
  assert len(written_records) == 1
  assert written_records[0].capture_key == retained_key
  ```
- [ ] Run the focused ingest test and record its failure. Implement the file path as `capture_export -> retain -> replay_capture -> Record -> ledger.upsert`, refusing non-ok replay. The record construction is `Record(**(row | {"capture_key": retained_key}))`; parse the retained safe bytes, never reread the unfiltered original for ingestion.
- [ ] Separate Google acquisition from primary-field selection. Retain allowed People API structures (names, memberships, organizations, birthdays, photos, resourceName) before their existing transformations; remove forbidden fields recursively within those known structures. Preserve resource enumeration per page without list phone/email summaries or secret page tokens. Record `has_next`, ordinal, completion and sanitized acquisition failures. Keep typed source IDs, numbers and timestamps distinct from free text. Archive before parsing into Records.
- [ ] Separate Apple acquisition from name/boolean/birthday transforms. Preserve permitted original scalar fields and birthday epoch number, plus explicit local-time conversion inputs for deterministic replay. Do not read phone/email values. Record database ordinal and sanitized failures, not personal DB paths. Use the retained safe source payload for `parse_apple` and the existing raw-row shape.
- [ ] Enforce privacy again when validating contacts captures, so a caller cannot bypass filtering with a hand-built checksum-valid envelope. Reject unsafe/unknown input or preserve an explicit exclusion, never label unvalidated data safe. Add pure contacts replay, archive-failure/no-write, partial acquisition, source-value preservation and transformed-value tests. Fix the safe invalid-inventory identifier in this existing CLI edit.
- [ ] Run covering red/green tests, one capture-order/privacy mutation, full pytest and Ruff. Commit normally and report exact payload/parser interfaces for 2B/2C/3/4, with concise command/output evidence.

### Task 2B: Profile inputs and legacy parser compatibility

**Files:** `src/people_sync/scrape/snapshot.py`, `run.py`, `partiful.py`, `venmo.py`, `photos.py`, `cli.py`, relevant capture/profile/DOM/Partiful/Venmo/CLI tests.

**Interfaces:** Preserve platform `parse(eval_result, captured)` and `photos.archive_profile(..., context=None)`; add `snapshot.collect(browser, platform: str) -> dict` returning scoped safe DOM input, selector/scope, exclusions and explicit success/partial state. Retain that input alongside extractor output before interpreting the latter. Do not implement list-page capture here; 2C owns it.

- [ ] Write a failing fake-browser integration test recording call order. For both ordinary/coordinated profiles and Partiful mutual profiles, source DOM collection precedes field extraction, verified retention precedes parsing, and an archive failure prevents all downstream writes.
  ```python
  assert events.index("source-dom") < events.index("field-extractor")
  assert events.index("verified-retention") < events.index("parse-profile")
  assert not any(event == "write-profile" for event in archive_failure_events)
  ```
- [ ] Run the focused test and record failure. Implement scoped DOM collection, with executable/hidden/form/session material excluded and source-specific permitted surfaces only. Record collection failures explicitly, preserving already received permitted responses even when later extraction raises. Retained input, not an unsanitized sibling object, supplies the parser/cache.
- [ ] Keep Venmo on its strict selected `otherUser` boundary, never a whole DOM/Next.js/network dump. Add explicit legacy REST `display_name`/`profile_picture_url` support alongside web `displayName`/`profilePictureUrl`, with synthetic pure replay tests. A join timestamp is not a birthday.
- [ ] Preserve safe existing avatar-key conventions, but use a collision-resistant opaque component for record IDs with characters the file service rejects. Never reuse an invalid prior key on the same-image shortcut. Existing retained objects/keys are not renamed or deleted. Add percent-encoded-handle and safe-key compatibility tests.
- [ ] Add `scrape <platform> --record-id ID` for an explicitly selected fresh or stale record, using the existing scrape flow and halt rules. Validate that the record exists, belongs to the requested source and is pending/matched rather than ignored/tombstoned. The default queue remains unchanged. Test that only that record is selected and invalid/cross-platform IDs fail before browser navigation. This supports Task5's selective recapture without falsifying scraped_at or restarting a broad backfill.
- [ ] Run profile, coordinated, Partiful, Venmo, privacy and DOM-JS tests, an ordering mutation, full pytest/Ruff; commit and report exact snapshot/capture interfaces to 2C.

### Task 2C: Incremental list-source retention

**Files:** `src/people_sync/cli.py`, `scrape/facebook.py`, `spotify.py`, `strava.py`, `partiful.py`, shared snapshot/replay helpers only as needed; respective list tests.

**Interfaces:** Existing list command names stay unchanged. Each retained page/scroll observation exposes its immutable capture key with original entry ordinals, source scope and observed complete/truncated status. Carry that key with records/handle proposals for Task 3, without source attributes on provenance.

- [ ] For each of Facebook, Spotify, Strava and Partiful, write a failing collector test with repeated and virtualized rows. Capture must occur before deduplication or mutation; an upload failure must stop new writes. Preserve observations even if a later page fails.
  ```python
  assert retained_pages[0]["entries"] == first_page_before_filtering
  assert retained_pages[1]["ordinal"] == 1
  assert result["complete"] is False  # acquisition stopped before the end
  assert writes_after_archive_failure == []
  ```
- [ ] Run the source-specific tests and record failures. Integrate the existing scoped capture/retention helper at each list boundary. Preserve individual page/scroll observations, expected totals where observed, and explicit termination reasons; do not make an unfinished list imply removals or a full inventory.
- [ ] Ensure Partiful captures mutual rows before clicking and profile inputs through 2B, so direct and mutual paths share the profile contract. Keep its exact Instagram-link matching rule and existing safe mutation behavior.
- [ ] Replay supported list formats through pure existing interpretation helpers, or return explicit unsupported status with retained-source limitations. Do not claim successful reconstruction when an extractor cannot run offline. Test archive failures, virtualized pages, duplicates, partial completion and capture-key propagation for all four collectors.
- [ ] Run focused red/green and one capture-order mutation, then full pytest/Ruff, commit and report. Mark parent Task 2 complete only after all 2A/2B/2C reviews pass.

### Task 2D: Usable privacy-filtered exports with explicit exclusions

**Files:** `src/people_sync/captures.py`, shared typed identity-URL helper from 2B as needed, export capture/replay tests.

**Interfaces:** Keep capture/replay/ingest command names and preserved local originals unchanged. Reuse 2B's strictly scoped identity URL handling for canonical source URLs and matching structured handles; never treat arbitrary URLs/free text as typed identities. Archive a versioned field-exclusion manifest by original file role, ordinal and field, with fixed reasons but no excluded values.

- [ ] Add synthetic failing regressions for canonical numeric LinkedIn URL suffixes and underscored Instagram handles, alongside forbidden userinfo/query/fragment/foreign-host variants. Confirm the current shared free-text checker falsely refuses the permitted identity.
  ```python
  assert replay_capture(capture_export("linkedin", export))["records"][0]["source_id"]
  assert b"secret@example.test" not in retained_bytes
  ```
  Build the export with a synthetic valid name, numeric canonical URL, one prohibited Company value and another safe row. Assert exact record IDs and retained row ordinals, not just truthiness.
- [ ] Preserve strict generic privacy validation. At source filtering only, replace prohibited/ambiguous allowed-field values with the source format's missing value, retaining the row and all other permitted fields. Record every excluded field's role/ordinal/path and reason in the capture. Do not copy the excluded value into the manifest. Keep structural malformed entries present and original files byte-identical. An unknown source shape still fails explicitly.
- [ ] Revalidate retained filtered bytes and typed identity fields at envelope/replay boundaries. Hand-built checksum-valid envelopes containing forbidden values still fail. Do not exempt generic numeric text, arbitrary URL paths, foreign hosts or encoded contact material. No new dependency or universal PII-detector claim.
- [ ] Offline replay must report field exclusions and their effect, never claim a complete original or silently replace current ledger/cache fields. Test mixed safe/unsafe rows, malformed entries, capture-before-ingest, deterministic replay, unchanged originals and privacy mutation failures. Full pytest/Ruff, normal signed commit and concise report.

### Task 3: Pin source and promoted-fact provenance to immutable captures

**Files:** Modify `src/people_sync/ledger.py`, `src/people_sync/scrape/profile.py`, `src/people_sync/promote.py`; create `tests/test_capture_provenance.py`, extend `tests/test_promote.py`.

**Interfaces:** Consume the optional Record capture key and existing profile raw key; produce deterministic `takeout` provenance edges referencing the exact retained file. Do not create new provenance tables or alter existing source facts/approval columns. Existing legacy promotion is reported as legacy rather than rewritten as historical capture evidence.

- [ ] Write a failing test that promotes two observations of one profile and checks each new evidence edge uses its own retained key, not the mutable profile row:
  ```python
  assert edge["from_kind"] == "takeout"
  assert edge["from_ref"] == "profiles/spotify/captures/observation-1.json"
  assert "display_name" not in json.loads(edge["detail"])
  ```
- [ ] Run the focused test and record failure.
- [ ] Extend record/profile write boundaries to emit deterministic whole-row `imported_from` edges from retained capture keys, repairing missing edges on retry while preserving ignored/matched states and tombstones. Use supported `lifedata` writes exclusively. Preserve original imported snapshot identity through replay results; never stamp a replay as a fresh source visit.
- [ ] Carry the exact raw key into promotion operations and evidence IDs. Existing manual/legacy evidence remains valid and unchanged. Reject or explicitly report missing source evidence for new promotion; do not invent a takeout ref. Replay remains proposal-only, so no automatic canonical changes occur.
- [ ] Test retry repair, distinct observations, missing evidence, existing matches/ignored records/tombstones and no source attributes on edges. Run full suite/ruff, commit, report.

### Task 4: Snapshot-based WhatsApp evidence ingestion

**Files:** Create `src/people_sync/whatsapp.py`, `tests/test_whatsapp.py`; modify `src/people_sync/cli.py`, `src/people_sync/replay.py`, optional supported platform listing in promotion.

**Interfaces:** Consume captures and record/profile write paths. Produce `whatsapp.collect(snapshot_path, media_dir, *, state_dir, self_id=None) -> dict` from an explicitly supplied, WAL-consistent snapshot; `whatsapp.parse_snapshot(payload) -> list[dict]` pure; installed `ingest whatsapp --snapshot PATH --media-dir PATH [--self-id ID]` or an equivalently explicit command. No copied machine paths, secrets-store commands or personal IDs in product code.

- [ ] Build a tiny synthetic SQLite snapshot with a direct counterpart, group/status/self rows, phone-form and opaque LID identities, cached-photo metadata and a valid thumbnail. Test that the direct counterpart alone becomes pending evidence, no phone/JID leaks, and photo bytes are retained before profile mutation. Test path traversal and symlink escape refusal.
  ```python
  assert report["direct_chats"] == 1
  assert all("@s.whatsapp.net" not in json.dumps(row) for row in persisted_rows)
  assert all(row.get("status", "pending") == "pending" for row in ledger_rows)
  ```
- [ ] Run the focused test and confirm failure before implementation.
- [ ] Implement explicit read-only snapshot consumption, no live SQLite modifications. Prefer native `@lid` IDs. Maintain a 0600 local random opaque-ID mapping for phone-only counterparts, never unsalted phone hashes; preserve mapping in private recovery state, never upload phone values. Exclude self using explicit native ID or trusted self metadata; ambiguous self detection must be reported, not guessed from a name. No group/status/broadcast rows become people.
- [ ] Capture permitted source rows, available push/display name/username fields, timestamps and photo metadata before parsing. Do not read message bodies. Use actual media files only within the supplied media root; identify JPEG/PNG signatures and preserve byte hashes. Distinguish thumbnail resolution and missing files. Record source observations as pending ledger/profile evidence, never auto-link names/photos.
- [ ] Make WhatsApp snapshot captures replay offline using the same pure parser. Test idempotent repeat, WAL-visible data prepared by operator snapshot, missing photo, unknown schema, privacy exclusions and preservation of matched records. Full suite/ruff, commit, report.

### Task 5: Recovery, installed verification, review enrichment and documentation

**Files:** Modify `README.md`, `AGENTS.md`; private operational reports and review inputs outside git. Skill edits in the private agent-config repo only after reading skill-edit instructions. Nix input bump in nix-config only after reading nix skills.

**Interfaces:** Use the installed capture/verify/replay/WhatsApp commands from Tasks 1-4 and existing private review builder. No private operational literals in product code. Existing Life Data schema and provenance catalog apply.

- [ ] Inventory and preserve existing private exports, review data/proposals and current relevant table snapshots. Use exact scoped directories, never broad recursive home scans. Originals are copied, not moved or overwritten. Record content hashes and source classification in a private manifest.
- [ ] Enumerate archive/photo refs through the Life CLI and verify/download via the existing scoped file service. Record every missing/unreadable/corrupt file and distinguish legacy hashes measured now from historical expected hashes. Keep any failed-input capture found locally. Never call a selected-field artifact complete original evidence.
- [ ] Offline replay retained supported captures into a proposal report. Compare with current source cache fields, but apply no canonical or identity changes. Preserve actual old capture time/unknown time rather than inventing one.
- [ ] Prepare a WAL-consistent WhatsApp desktop snapshot via the supported read workflow; ingest through the installed command, then augment the private review from its new pending evidence. Preserve old review page, original contexts/proposals and browser saves. Additional source accounts may be suggested by explicit existing contact/source links or reviewed conversationally; name/photo alone never implies approved identity. Regenerate review with migration-safe saved-decision handling and clear new-evidence status.
- [ ] Evaluate recovery gaps against review needs; collect only justified missing non-LinkedIn profiles through the new installed capture flow, with existing halt rules. Report fresh observations separately. No blanket rerun.
- [ ] Run installed offline replay smoke, local tests, ruff, Nix package/flake checks, a private-page browser smoke through chrome-control, and verify changed source rows through native Life sync on another replica. Update current-state docs/runbook, commit and push non-deploy repos per project rules. Keep broader matching task In Progress until the user's identity review is actually completed; report remaining gaps honestly.

## Self-review

Coverage: Tasks 1-2 implement retention/hash/input scope and deterministic replay; Task 3 exact provenance and preservation; Task 4 WhatsApp; Task 5 recovery, verification and private review. Shared interfaces are named above. Each task includes a fail-first behavioral check and an isolated deliverable. The user approved implementation of the recommendation; no additional design-choice gate is required absent expanded authority.
