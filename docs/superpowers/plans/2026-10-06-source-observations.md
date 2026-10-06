# Source Observations Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task by task.

**Goal:** Make every retained source entry independently addressable without changing resolved identities or the shared latest-profile cache.

**Architecture:** An immutable `people_sync_observations` table indexes entries in verified capture files. Capture key plus scope and entry ordinal identifies an observation; display names never identify rows. Original files retain source-specific fields and exclusion manifests. The installed CLI previews or applies an on-demand backfill from its capture directory, repairing missing provenance idempotently.

**Tech Stack:** Existing Python, argparse, Life CLI and Life file API. No new dependency or scheduler.

**Spec:** Approved shared-cache-plus-observations design; requirements below are its implementation contract.

## Global Constraints

- The system shall index each original export/list/contact row before any identity deduplication, including malformed/excluded slots preserved by the capture boundary.
- The system shall retain scope observations for empty, failed or truncated acquisitions.
- The system shall validate capture privacy and checksum before preview, and verify exact retained file bytes before writing observations.
- The system shall leave People, account links, review decisions, ledger values, shared profile cache and originals unchanged.
- Observation columns shall be immutable; retries shall insert missing rows and provenance only, preserve tombstones, and reject conflicting content at an existing ID.
- The observation index shall point to original scope/ordinal and hashes; it shall not duplicate platform-specific payloads or contact values into new columns.
- Legacy inputs without validated envelopes shall be reported as unsupported, never represented as recovered originals.
- Schema remains operator-owned user data, created and cataloged through installed Life commands.

## Review Focus

- Two identical names in one export must produce distinct observations.
- Empty or failed scopes must not disappear from coverage.
- A valid local payload with altered envelope metadata must fail retained-byte verification.
- A retry after interrupted provenance must repair edges without rewriting observations.
- A tombstone or conflicting row must never be overwritten or resurrected.

### Task 1: Observation planning and persistence

Files: create `src/people_sync/observations.py`, `tests/test_observations.py`.
Interfaces: `plan(capture: dict) -> list[dict]`; `index(captures: list[dict], *, apply: bool = False) -> dict`.

- [ ] Write tests for duplicate names, all four capture kinds, explicit empty/failed scope rows, stable IDs and checksum rejection; observe RED.
- [ ] Implement batched deterministic capture/scope/ordinal rows and immutable insertion through lifedata, with exact remote verification and provenance repair.
- [ ] Test conflicting existing rows, tombstones, interrupted provenance and private failure reporting; observe GREEN.
- [ ] Mutation-check identity ordinals and byte verification; commit.

### Task 2: Installed CLI and operational contract

Files: modify `src/people_sync/cli.py`, `README.md`, `AGENTS.md`; create CLI tests in `tests/test_observations.py`.
Interface: `people-sync observations [--state-dir PATH | --input PATH] [--apply]`; preview by default, aggregate per-capture outcomes, nonzero for failed files.

- [ ] Write CLI preview/apply/error tests; observe RED.
- [ ] Add command using existing private capture directory conventions. Document operator-owned table contract, on-demand indexing and recovery limits.
- [ ] Run targeted tests, full suite, static checks and Nix package build; obtain independent whole-change review; fix substantive findings with tests.
- [ ] Commit/push source; provision the approved user-owned catalog, index accessible retained evidence through the supported installed interface, and verify hub delivery. Any deployment approval is requested only after the result is reviewable.
