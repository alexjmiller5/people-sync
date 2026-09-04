# Profile Scraping Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Tasks marked INTERACTIVE need Alex (a CDP "Allow" click, or decisions).

**Goal:** Capture every social profile behind the ledger (Facebook, Instagram, LinkedIn, then Venmo, Spotify, Snapchat web, Partiful mutuals) into `contact_profiles` with raw pages archived, so reconciliation has hometown/school/employer context and profile pictures.

**Architecture:** A Python CDP client attaches to Alex's real logged-in Chrome (Tier 2 of the chrome-control skill), navigates profile pages at human pace, and runs a per-platform extractor JS in the page to pull the data the page already rendered or embedded. Results go to R2 verbatim (raw) plus `contact_profiles` (structured, latest wins) via the `life` CLI. Resumable, capped per day, stops on any challenge page.

**Tech Stack:** Python 3.13, `websockets` (new dependency, the CDP transport), existing `lifedata`/`photos` modules, chrome-control Tier 2 mechanics, `life` CLI.

**Spec:** `docs/superpowers/specs/2026-09-02-people-db-contact-sync-design.md` - "Addendum (2026-09-04): profile scraping phase" is binding.

## Global Constraints

- Act as Alex in his own browser; human pace: randomized gap 8-25 s between profile loads, a 2-5 minute break every 25 loads, per-platform daily caps (facebook 150, instagram 250, linkedin 80, others 300), immediate halt on any login/checkpoint/"action blocked"/rate-limit page. Never automate past a credential or CAPTCHA wall.
- One CDP connection per run (one "Allow" click by Alex); DevToolsActivePort is read every time, never a hardcoded port.
- Raw page data verbatim to R2 `life-data-archive` under `profiles/<platform>/<record_key>/<scraped_at>.json` before any parsing; avatar bytes to `photos/records/<platform>/<record_key>-<sha8>.<ext>`.
- All life-data writes via `lifedata`; soft deletes only; timestamps ISO-8601 UTC ms.
- Emails and phone numbers seen on any profile are NEVER stored.
- No personal data in the repo: fixtures are synthetic renderings of the observed page shapes; recon notes with real values live only in the gitignored workspace.
- Plain commit messages, no trailers, no em dashes. TDD for every extractor.

---

### Task 1: `contact_profiles` table (direct op) - no repo code

- [ ] `life export > ~/Documents/manual-backups/life-data-pre-profiles-$(date +%Y%m%d).sql`
- [ ] `life table create contact_profiles record_id:text platform:text profile_url:text platform_id:text display_name:text bio:text location:text hometown:text education:text work:text birthday:text links:text is_private:integer is_verified:integer follower_count:integer following_count:integer mutual_count:integer avatar_r2_key:text avatar_sha256:text raw_r2_key:text scraped_at:text`
- [ ] Verify via `PRAGMA table_info(contact_profiles)`; `life sync`. Row id convention: `<record_id>` (one row per ledger record, latest wins via UPDATE).

### Task 2: CDP harness

**Files:** Create `src/contact_sync/scrape/__init__.py`, `src/contact_sync/scrape/cdp.py`, `src/contact_sync/scrape/pace.py`; Test `tests/test_cdp.py`, `tests/test_pace.py`.

**Interfaces:**
- `cdp.Browser.connect() -> Browser` reads `~/Library/Application Support/Google/Chrome/DevToolsActivePort`, opens the browser websocket (`websockets`), `Target.createTarget` a fresh tab, `Target.attachToTarget {flatten:true}`, keeps the session id. Methods: `navigate(url, wait_ms)` (Page.navigate then Page.loadEventFired or timeout), `eval(js) -> Any` (Runtime.evaluate with returnByValue), `close()`. Every CDP message id-correlated; a 30 s handshake timeout with a clear "click Allow in Chrome" message.
- `pace.Pacer(platform, state_path)` - `next_gap() -> float` (uniform 8-25 s, plus a 120-300 s break every 25 calls), `allow() -> bool` (daily cap from the constraints, persisted per platform per UTC day in a JSON state file under `data/scrape-state.json`), `record()`.
- `pace.is_challenge(html_or_title: str) -> bool` - true on login/checkpoint/"action blocked"/"try again later"/captcha markers (a documented list).
- Tests: pace fully unit-tested (cap rollover at UTC midnight, break cadence, challenge markers); cdp tested with a fake websocket server (message correlation, timeout error text).
- Commit: `feat: cdp harness + human pacing`.

### Task 3 (INTERACTIVE): recon spikes - one short note per platform

For each of facebook, instagram, linkedin, venmo, spotify, snapchat-web, partiful: with Alex's Allow click, open ONE profile (or the list page) in his Chrome, and determine where the data lives (inline JSON blob, `__INITIAL_STATE__`-style, or rendered DOM), write the extractor JS that returns one JSON object with the spec's fields, verify it on 3 real pages, and save `recon-<platform>.md` in the workspace (selectors/keys only - no real values in the repo). Also record: the URL pattern per record (facebook needs the friends-list pass to learn ids), the challenge-page markers observed, and how the avatar URL is obtained. Each note ends with a synthetic fixture (page shape with fake values) for Task 4's tests.

### Task 4: extractors + `scrape` CLI

**Files:** Create `src/contact_sync/scrape/<platform>.py` per platform (extractor JS constant + `parse(result: dict) -> Profile`), `src/contact_sync/scrape/run.py` (the loop), tests per platform on the Task 3 fixtures; Modify `cli.py` to add `scrape <platform> [--max N] [--list]`.

**Interfaces:**
- `Profile` dataclass mirroring contact_profiles columns (+ `avatar_url`, `raw: dict`).
- `run.scrape(platform, max_n)`: select ledger records for the platform (pending or matched) with no contact_profiles row or `scraped_at` older than 180 days, in ledger order; for each: `Pacer.allow()` else stop; `Browser.navigate(url)`; `is_challenge` check -> halt with a clear message; `eval(EXTRACTOR_JS)` -> parse; upload raw JSON to R2; fetch avatar via `photos.fetch_url_photo` (through the browser session cookie when the CDN requires it - recon decides) and store bytes to the records key space with sha dedupe; UPDATE-or-INSERT contact_profiles; `Pacer.record()`; sleep `next_gap()`. Structlog: platform/index/reason only.
- `scrape facebook --list`: walk the friends page, upsert `platform_id`/`profile_url` onto contact_profiles rows for the name-keyed facebook ledger records (name match, ambiguous names reported, not guessed).
- Commit per platform.

### Task 5 (INTERACTIVE, multi-day): the runs

Facebook list pass, then facebook profiles, then instagram, then linkedin, each in background sessions respecting the caps; a per-day report (done/remaining/halts). Alex's Mac must be awake with Chrome open; a halt on a challenge page is reported immediately and the platform pauses 24 h.

### Task 6: Venmo + Spotify list passes

New ledger sources `venmo` and `spotify` (parsers from the scraped lists, source_id = username / user id), then their profile pass through the same loop. Snapchat web friends list likewise only if recon found it cheap.

### Task 7 (INTERACTIVE): Partiful mutuals

Walk `https://partiful.com/mutuals`; for each mutual read the bio's Instagram url; resolve to a person via `person_accounts` (platform instagram, handle); on a match: insert the partiful account row + store the Partiful picture into person_photos; no match -> report the list to Alex (create-or-ignore decisions).

### Task 8: promotion into people + enriched reconciliation

Modify `scripts/reconcile.py`: on `link`/`create`, promote the record's `contact_profiles.avatar_r2_key` into `person_photos` (history kept, sha dedupe) and print the profile facts (hometown, education, work, birthday) as PROPOSED additions - applied only with `--with-profile-facts` after Alex approves per batch. Modify the reconciliation presentation to show hometown/school/employer beside each candidate. Tests.

### Task 9: docs

life-map contract for `contact_profiles`; contacts-review skill: the scrape step, the claude-in-chrome rule for future new-connection visits, the halt-and-wait rule; AGENTS.md; memory.
