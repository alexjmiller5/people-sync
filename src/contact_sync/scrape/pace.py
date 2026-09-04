"""Human-pace throttling for profile scraping: gaps, breaks, daily caps, and
challenge-page detection.

State (calls made per platform per UTC day) persists to a small JSON file so
caps survive across process restarts within the same day.
"""

import json
import os
import random
from datetime import datetime, timezone

DAILY_CAPS = {
    "facebook": 150,
    "instagram": 250,
    "linkedin": 80,
}
DEFAULT_DAILY_CAP = 300

DEFAULT_STATE_PATH = "data/scrape-state.json"

BREAK_EVERY = 25

# Markers observed on login/checkpoint/rate-limit pages across platforms.
# Matched case-insensitively as substrings against page text or title.
CHALLENGE_MARKERS = [
    "log in",
    "login",
    "checkpoint",
    "action blocked",
    "try again later",
    "captcha",
    "unusual activity",
    "verify it's you",
    "restricted",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def is_challenge(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in CHALLENGE_MARKERS)


class Pacer:
    def __init__(self, platform: str, state_path: str = DEFAULT_STATE_PATH):
        self.platform = platform
        self.state_path = state_path
        self.cap = DAILY_CAPS.get(platform, DEFAULT_DAILY_CAP)
        self._calls = 0

    def next_gap(self) -> float:
        self._calls += 1
        gap = random.uniform(8.0, 25.0)
        if self._calls % BREAK_EVERY == 0:
            gap += random.uniform(120.0, 300.0)
        return gap

    def _today(self) -> str:
        return _utcnow().strftime("%Y-%m-%d")

    def _load(self) -> dict:
        try:
            with open(self.state_path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _count_today(self) -> int:
        return self._load().get(self.platform, {}).get(self._today(), 0)

    def allow(self) -> bool:
        return self._count_today() < self.cap

    def record(self) -> None:
        state = self._load()
        platform_state = state.setdefault(self.platform, {})
        today = self._today()
        platform_state[today] = platform_state.get(today, 0) + 1
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        with open(self.state_path, "w") as f:
            json.dump(state, f)
