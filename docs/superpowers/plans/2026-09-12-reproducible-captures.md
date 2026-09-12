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
