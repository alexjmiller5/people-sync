"""Human-pace throttling for profile scraping: gaps, breaks, daily caps, and
challenge-page detection.

State (calls made per platform per UTC day, plus the break-cadence counter)
persists to a small JSON file so both survive process restarts within the
same day. Every read-modify-write is done under an advisory file lock
(`fcntl.flock` on a sibling `.lock` file) and written atomically (temp file +
`os.replace`), so two Pacer processes sharing a state file - or a process
that dies mid-write - never corrupt or under-count it.
"""

import contextlib
import fcntl
import json
import os
import random
import tempfile
import time
from datetime import datetime, timezone

import structlog

log = structlog.get_logger(__name__)

DAILY_CAPS = {
    "facebook": 150,
    "instagram": 250,
    "linkedin": 80,
}
DEFAULT_DAILY_CAP = 300
# Operator override: a JSON object {platform: cap} merged over DAILY_CAPS.
DAILY_CAPS_ENV = "PEOPLE_SYNC_DAILY_CAPS"

DEFAULT_STATE_PATH = "data/scrape-state.json"

BREAK_EVERY = 25

# Markers observed on login/checkpoint/rate-limit pages across platforms.
# Matched case-insensitively as substrings against page text or title.
# Phrases, not bare nouns - "restricted"/"checkpoint" alone trip on ordinary
# bios and employer names ("restricted diet", "Checkpoint Systems"), and a
# bare "captcha" trips on the reCAPTCHA badge ("This site is protected by
# reCAPTCHA...") that sits on perfectly ordinary login pages.
#
# Split in two because the login flow (scrape/login.py) reuses
# CHALLENGE_MARKERS but must NOT halt on LOGIN_MARKERS - those words are the
# page it is deliberately driving.
LOGIN_MARKERS = (
    "log in",
    "login",
)

CHALLENGE_MARKERS = (
    # a captcha gate (the reCAPTCHA badge on ordinary pages says neither)
    "make sure that you're a human",
    "make sure that you\u2019re a human",
    "i'm not a robot",
    "i\u2019m not a robot",
    "you're not a robot, right",
    "you\u2019re not a robot, right",
    "i am human",
    "checkpoint required",
    "checkpoint/",
    "action blocked",
    "try again later",
    "solve the captcha",
    "complete the captcha",
    "complete this captcha",
    "captcha challenge",
    "i'm not a robot",
    "verify you are human",
    "verify you're human",
    "unusual activity",
    "verify it's you",
    "account restricted",
    "your account has been restricted",
    "we restrict certain activity",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def is_challenge(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in LOGIN_MARKERS + CHALLENGE_MARKERS)


def daily_caps() -> dict[str, int]:
    """Built-in caps with the operator's DAILY_CAPS_ENV object merged over
    them. Anything malformed raises here - before a single page loads - rather
    than silently running uncapped."""
    raw = os.environ.get(DAILY_CAPS_ENV)
    if not raw:
        return dict(DAILY_CAPS)
    try:
        override = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError(f"{DAILY_CAPS_ENV} is not valid JSON") from None
    valid = isinstance(override, dict) and all(
        isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in override.values()
    )
    if not valid:
        raise ValueError(f"{DAILY_CAPS_ENV} must be a JSON object of non-negative integer caps")
    return {**DAILY_CAPS, **override}


class Pacer:
    def __init__(self, platform: str, state_path: str = DEFAULT_STATE_PATH):
        self.platform = platform
        self.state_path = state_path
        self._cap = daily_caps().get(platform, DEFAULT_DAILY_CAP)

    @property
    def cap(self) -> int:
        return self._cap

    def _today(self) -> str:
        return _utcnow().strftime("%Y-%m-%d")

    @contextlib.contextmanager
    def _locked(self):
        lock_path = f"{self.state_path}.lock"
        os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
        with open(lock_path, "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def _quarantine_corrupt(self) -> None:
        log.warning("scrape state file corrupt", platform=self.platform, reason="invalid json")
        corrupt_path = f"{self.state_path}.corrupt-{int(time.time())}"
        os.replace(self.state_path, corrupt_path)

    def _load(self) -> dict | None:
        """Must be called while holding `_locked()`. Returns None if the file
        is corrupt (already logged and quarantined by moving it aside)."""
        try:
            with open(self.state_path) as f:
                text = f.read()
        except FileNotFoundError:
            return {}
        if not text.strip():
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            self._quarantine_corrupt()
            return None

    def _save(self, state: dict) -> None:
        directory = os.path.dirname(self.state_path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".scrape-state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(state, f)
            os.replace(tmp_path, self.state_path)
        except BaseException:
            os.unlink(tmp_path)
            raise

    def _today_entry(self, state: dict) -> dict:
        return state.get(self.platform, {}).get(self._today(), {"calls": 0, "gap_calls": 0})

    def next_gap(self) -> float:
        with self._locked():
            state = self._load()
            gap_calls = 0 if state is None else self._today_entry(state).get("gap_calls", 0)
            gap_calls += 1
            self._write_gap_calls({} if state is None else state, gap_calls)
        gap = random.uniform(8.0, 25.0)
        if gap_calls % BREAK_EVERY == 0:
            gap += random.uniform(120.0, 300.0)
        return gap

    def _write_gap_calls(self, state: dict, gap_calls: int) -> None:
        platform_state = state.setdefault(self.platform, {})
        today = self._today()
        entry = platform_state.setdefault(today, {"calls": 0, "gap_calls": 0})
        entry["gap_calls"] = gap_calls
        self._save(state)

    def allow(self) -> bool:
        with self._locked():
            state = self._load()
            if state is None:
                return False
            return self._today_entry(state).get("calls", 0) < self.cap

    def record(self) -> None:
        with self._locked():
            state = self._load()
            if state is None:
                state = {}
            platform_state = state.setdefault(self.platform, {})
            today = self._today()
            entry = platform_state.setdefault(today, {"calls": 0, "gap_calls": 0})
            entry["calls"] = entry.get("calls", 0) + 1
            self._save(state)
