import json
from datetime import datetime, timezone

import pytest

from contact_sync.scrape import pace


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)


def test_next_gap_uniform_range(tmp_path, mocker):
    mocker.patch("contact_sync.scrape.pace.random.uniform", return_value=15.0)
    p = pace.Pacer("instagram", state_path=str(tmp_path / "state.json"))
    assert p.next_gap() == 15.0


def test_next_gap_calls_uniform_with_8_25_bounds(tmp_path, mocker):
    uniform = mocker.patch("contact_sync.scrape.pace.random.uniform", return_value=10.0)
    p = pace.Pacer("instagram", state_path=str(tmp_path / "state.json"))
    p.next_gap()
    uniform.assert_called_once_with(8.0, 25.0)


def test_next_gap_adds_break_every_25_calls(tmp_path, mocker):
    uniform = mocker.patch("contact_sync.scrape.pace.random.uniform")
    uniform.side_effect = [10.0] * 24 + [10.0, 200.0]
    p = pace.Pacer("instagram", state_path=str(tmp_path / "state.json"))
    gaps = [p.next_gap() for _ in range(25)]
    # the 25th call adds a break on top of the normal gap
    assert gaps[:24] == [10.0] * 24
    assert gaps[24] == 10.0 + 200.0
    assert uniform.call_args_list[-1] == mocker.call(120.0, 300.0)


def test_next_gap_break_cadence_repeats(tmp_path, mocker):
    uniform = mocker.patch("contact_sync.scrape.pace.random.uniform")
    # calls 1-24: base only. call 25: base + break. same pattern for 26-50.
    uniform.side_effect = [10.0] * 24 + [10.0, 200.0] + [10.0] * 24 + [10.0, 200.0]
    p = pace.Pacer("instagram", state_path=str(tmp_path / "state.json"))
    gaps = [p.next_gap() for _ in range(50)]
    assert gaps[24] == 10.0 + 200.0
    assert gaps[49] == 10.0 + 200.0


def test_next_gap_break_counter_survives_restart(tmp_path, mocker):
    uniform = mocker.patch("contact_sync.scrape.pace.random.uniform")
    state_path = str(tmp_path / "state.json")

    uniform.side_effect = [10.0] * 24
    p1 = pace.Pacer("instagram", state_path=state_path)
    for _ in range(24):
        p1.next_gap()

    # process restarts: a brand new Pacer instance, no in-memory state
    uniform.side_effect = [10.0, 200.0]
    p2 = pace.Pacer("instagram", state_path=state_path)
    gap = p2.next_gap()
    assert gap == 10.0 + 200.0


@pytest.mark.parametrize(
    "platform,cap",
    [
        ("facebook", 150),
        ("instagram", 250),
        ("linkedin", 80),
        ("some_new_platform", 300),
    ],
)
def test_allow_respects_daily_cap(tmp_path, mocker, platform, cap):
    mocker.patch(
        "contact_sync.scrape.pace._utcnow",
        return_value=_dt("2026-09-04T12:00:00"),
    )
    state_path = tmp_path / "state.json"
    p = pace.Pacer(platform, state_path=str(state_path))
    for _ in range(cap):
        assert p.allow() is True
        p.record()
    assert p.allow() is False


def test_allow_true_when_state_file_missing(tmp_path, mocker):
    mocker.patch("contact_sync.scrape.pace._utcnow", return_value=_dt("2026-09-04T12:00:00"))
    p = pace.Pacer("instagram", state_path=str(tmp_path / "missing.json"))
    assert p.allow() is True


def test_record_persists_state_to_json_file(tmp_path, mocker):
    mocker.patch("contact_sync.scrape.pace._utcnow", return_value=_dt("2026-09-04T12:00:00"))
    state_path = tmp_path / "state.json"
    p = pace.Pacer("instagram", state_path=str(state_path))
    p.record()
    p.record()

    data = json.loads(state_path.read_text())
    assert data["instagram"]["2026-09-04"]["calls"] == 2


def test_record_is_scoped_per_platform(tmp_path, mocker):
    mocker.patch("contact_sync.scrape.pace._utcnow", return_value=_dt("2026-09-04T12:00:00"))
    state_path = tmp_path / "state.json"
    ig = pace.Pacer("instagram", state_path=str(state_path))
    fb = pace.Pacer("facebook", state_path=str(state_path))
    ig.record()
    fb.record()
    fb.record()

    data = json.loads(state_path.read_text())
    assert data["instagram"]["2026-09-04"]["calls"] == 1
    assert data["facebook"]["2026-09-04"]["calls"] == 2


def test_cap_rolls_over_at_utc_midnight(tmp_path, mocker):
    state_path = tmp_path / "state.json"
    clock = mocker.patch("contact_sync.scrape.pace._utcnow")

    clock.return_value = _dt("2026-09-04T23:59:00")
    p = pace.Pacer("linkedin", state_path=str(state_path))
    for _ in range(80):
        assert p.allow() is True
        p.record()
    assert p.allow() is False

    # a new day resets the count, even against the same Pacer instance
    clock.return_value = _dt("2026-09-05T00:01:00")
    assert p.allow() is True


def test_record_loads_existing_state_file(tmp_path, mocker):
    mocker.patch("contact_sync.scrape.pace._utcnow", return_value=_dt("2026-09-04T12:00:00"))
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"instagram": {"2026-09-04": {"calls": 5, "gap_calls": 5}}}))

    p = pace.Pacer("instagram", state_path=str(state_path))
    assert p.allow() is True
    p.record()

    data = json.loads(state_path.read_text())
    assert data["instagram"]["2026-09-04"]["calls"] == 6


def test_default_state_path_is_data_scrape_state_json():
    p = pace.Pacer("instagram")
    assert p.state_path == "data/scrape-state.json"


def test_two_pacer_instances_see_each_others_writes(tmp_path, mocker):
    # Sequential interleaving on the same state file, two separate instances
    # (simulating two processes): each re-reads under the lock rather than
    # trusting an in-memory snapshot.
    mocker.patch("contact_sync.scrape.pace._utcnow", return_value=_dt("2026-09-04T12:00:00"))
    state_path = tmp_path / "state.json"
    a = pace.Pacer("instagram", state_path=str(state_path))
    b = pace.Pacer("instagram", state_path=str(state_path))

    a.record()
    b.record()
    assert a.allow() is True
    a.record()
    b.record()

    data = json.loads(state_path.read_text())
    assert data["instagram"]["2026-09-04"]["calls"] == 4


def test_write_is_atomic_no_tmp_file_left_behind(tmp_path, mocker):
    mocker.patch("contact_sync.scrape.pace._utcnow", return_value=_dt("2026-09-04T12:00:00"))
    state_path = tmp_path / "state.json"
    pace.Pacer("instagram", state_path=str(state_path)).record()

    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".scrape-state-")]
    assert leftovers == []


def test_allow_false_and_quarantines_corrupt_state_file(tmp_path, mocker):
    mocker.patch("contact_sync.scrape.pace._utcnow", return_value=_dt("2026-09-04T12:00:00"))
    state_path = tmp_path / "state.json"
    state_path.write_text("{not valid json")

    p = pace.Pacer("instagram", state_path=str(state_path))
    assert p.allow() is False

    assert not state_path.exists()
    quarantined = list(tmp_path.glob("state.json.corrupt-*"))
    assert len(quarantined) == 1


def test_allow_logs_corruption_with_platform_and_reason_only(tmp_path, mocker):
    mocker.patch("contact_sync.scrape.pace._utcnow", return_value=_dt("2026-09-04T12:00:00"))
    warn = mocker.patch.object(pace.log, "warning")
    state_path = tmp_path / "state.json"
    state_path.write_text("{not valid json")

    pace.Pacer("instagram", state_path=str(state_path)).allow()

    warn.assert_called_once()
    _, kwargs = warn.call_args
    assert kwargs == {"platform": "instagram", "reason": "invalid json"}


def test_record_self_heals_after_corrupt_state_file(tmp_path, mocker):
    mocker.patch("contact_sync.scrape.pace._utcnow", return_value=_dt("2026-09-04T12:00:00"))
    state_path = tmp_path / "state.json"
    state_path.write_text("not json at all")

    p = pace.Pacer("instagram", state_path=str(state_path))
    p.record()

    data = json.loads(state_path.read_text())
    assert data["instagram"]["2026-09-04"]["calls"] == 1


@pytest.mark.parametrize(
    "text",
    [
        "Please log in to continue",
        "Help us confirm your identity - checkpoint required",
        "Action Blocked",
        "Try again later",
        "Please complete this CAPTCHA",
        "We've detected unusual activity on your account",
        "Please verify it's you before continuing",
        "Your account has been restricted",
        "https://example.com/checkpoint/12345",
        "Sorry, we restrict certain activity to protect our community",
    ],
)
def test_is_challenge_true_on_markers(text):
    assert pace.is_challenge(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "Alex Smith - Photos",
        "Welcome to the profile page",
        "500 followers, 300 following",
        "Bio: vegetarian, on a restricted diet, loves hiking",
        "Works at Checkpoint Systems",
    ],
)
def test_is_challenge_false_on_ordinary_text(text):
    assert pace.is_challenge(text) is False


def test_is_challenge_case_insensitive():
    assert pace.is_challenge("LOGIN REQUIRED") is True
