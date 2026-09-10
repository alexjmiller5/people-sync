import json
from datetime import datetime, timezone

import pytest

from people_sync.scrape import pace


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)


def test_next_gap_uniform_range(tmp_path, mocker):
    mocker.patch("people_sync.scrape.pace.random.uniform", return_value=15.0)
    p = pace.Pacer("instagram", state_path=str(tmp_path / "state.json"))
    assert p.next_gap() == 15.0


def test_next_gap_calls_uniform_with_8_25_bounds(tmp_path, mocker):
    uniform = mocker.patch("people_sync.scrape.pace.random.uniform", return_value=10.0)
    p = pace.Pacer("instagram", state_path=str(tmp_path / "state.json"))
    p.next_gap()
    uniform.assert_called_once_with(8.0, 25.0)


def test_next_gap_adds_break_every_25_calls(tmp_path, mocker):
    uniform = mocker.patch("people_sync.scrape.pace.random.uniform")
    uniform.side_effect = [10.0] * 24 + [10.0, 200.0]
    p = pace.Pacer("instagram", state_path=str(tmp_path / "state.json"))
    gaps = [p.next_gap() for _ in range(25)]
    # the 25th call adds a break on top of the normal gap
    assert gaps[:24] == [10.0] * 24
    assert gaps[24] == 10.0 + 200.0
    assert uniform.call_args_list[-1] == mocker.call(120.0, 300.0)


def test_next_gap_break_cadence_repeats(tmp_path, mocker):
    uniform = mocker.patch("people_sync.scrape.pace.random.uniform")
    # calls 1-24: base only. call 25: base + break. same pattern for 26-50.
    uniform.side_effect = [10.0] * 24 + [10.0, 200.0] + [10.0] * 24 + [10.0, 200.0]
    p = pace.Pacer("instagram", state_path=str(tmp_path / "state.json"))
    gaps = [p.next_gap() for _ in range(50)]
    assert gaps[24] == 10.0 + 200.0
    assert gaps[49] == 10.0 + 200.0


def test_next_gap_break_counter_survives_restart(tmp_path, mocker):
    uniform = mocker.patch("people_sync.scrape.pace.random.uniform")
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
    mocker.patch.dict("os.environ", {pace.DAILY_CAPS_ENV: json.dumps({platform: cap})})
    mocker.patch(
        "people_sync.scrape.pace._utcnow",
        return_value=_dt("2026-09-04T12:00:00"),
    )
    state_path = tmp_path / "state.json"
    p = pace.Pacer(platform, state_path=str(state_path))
    for _ in range(cap):
        assert p.allow() is True
        p.record()
    assert p.allow() is False


def test_allow_true_when_state_file_missing(tmp_path, mocker):
    mocker.patch("people_sync.scrape.pace._utcnow", return_value=_dt("2026-09-04T12:00:00"))
    p = pace.Pacer("instagram", state_path=str(tmp_path / "missing.json"))
    assert p.allow() is True


def test_record_persists_state_to_json_file(tmp_path, mocker):
    mocker.patch("people_sync.scrape.pace._utcnow", return_value=_dt("2026-09-04T12:00:00"))
    state_path = tmp_path / "state.json"
    p = pace.Pacer("instagram", state_path=str(state_path))
    p.record()
    p.record()

    data = json.loads(state_path.read_text())
    assert data["instagram"]["2026-09-04"]["calls"] == 2


def test_record_is_scoped_per_platform(tmp_path, mocker):
    mocker.patch("people_sync.scrape.pace._utcnow", return_value=_dt("2026-09-04T12:00:00"))
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
    mocker.patch.dict("os.environ", {pace.DAILY_CAPS_ENV: '{"linkedin": 80}'})
    state_path = tmp_path / "state.json"
    clock = mocker.patch("people_sync.scrape.pace._utcnow")

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
    mocker.patch("people_sync.scrape.pace._utcnow", return_value=_dt("2026-09-04T12:00:00"))
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
    mocker.patch("people_sync.scrape.pace._utcnow", return_value=_dt("2026-09-04T12:00:00"))
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
    mocker.patch("people_sync.scrape.pace._utcnow", return_value=_dt("2026-09-04T12:00:00"))
    state_path = tmp_path / "state.json"
    pace.Pacer("instagram", state_path=str(state_path)).record()

    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".scrape-state-")]
    assert leftovers == []


def test_allow_false_and_quarantines_corrupt_state_file(tmp_path, mocker):
    mocker.patch("people_sync.scrape.pace._utcnow", return_value=_dt("2026-09-04T12:00:00"))
    state_path = tmp_path / "state.json"
    state_path.write_text("{not valid json")

    p = pace.Pacer("instagram", state_path=str(state_path))
    assert p.allow() is False

    assert not state_path.exists()
    quarantined = list(tmp_path.glob("state.json.corrupt-*"))
    assert len(quarantined) == 1


def test_allow_logs_corruption_with_platform_and_reason_only(tmp_path, mocker):
    mocker.patch("people_sync.scrape.pace._utcnow", return_value=_dt("2026-09-04T12:00:00"))
    warn = mocker.patch.object(pace.log, "warning")
    state_path = tmp_path / "state.json"
    state_path.write_text("{not valid json")

    pace.Pacer("instagram", state_path=str(state_path)).allow()

    warn.assert_called_once()
    _, kwargs = warn.call_args
    assert kwargs == {"platform": "instagram", "reason": "invalid json"}


def test_record_self_heals_after_corrupt_state_file(tmp_path, mocker):
    mocker.patch("people_sync.scrape.pace._utcnow", return_value=_dt("2026-09-04T12:00:00"))
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
        "Solve the CAPTCHA to continue",
        "CAPTCHA challenge",
        "Check the box: I'm not a robot",
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
        "Nova Quill - Photos",
        "Welcome to the profile page",
        "500 followers, 300 following",
        "Bio: vegetarian, on a restricted diet, loves hiking",
        "Works at Checkpoint Systems",
        # the reCAPTCHA badge sits on ordinary pages - matching a bare
        # "captcha" substring here halted every run before it started
        "This site is protected by reCAPTCHA and the Google Privacy Policy "
        "and Terms of Service apply.",
    ],
)
def test_is_challenge_false_on_ordinary_text(text):
    assert pace.is_challenge(text) is False


def test_is_challenge_case_insensitive():
    assert pace.is_challenge("LOGIN REQUIRED") is True
    assert pace.is_challenge("Log in with Facebook / This page doesn't exist") is False
    assert pace.challenge_marker("please LOG IN to see more") == "please log in"


def test_login_markers_are_kept_separate_from_challenge_markers():
    """The login flow reuses CHALLENGE_MARKERS but must not halt on the words
    that merely mean "this is a login page"."""
    assert "please log in" in pace.LOGIN_MARKERS
    assert not any("log in" in marker for marker in pace.CHALLENGE_MARKERS)
    assert pace.is_challenge("Please log in to continue") is True


@pytest.mark.parametrize("platform", ["instagram", "facebook", "linkedin", "venmo"])
def test_no_default_daily_budget_preserves_existing_counts(monkeypatch, tmp_path, platform):
    monkeypatch.delenv(pace.DAILY_CAPS_ENV, raising=False)
    state = tmp_path / "state.json"
    p = pace.Pacer(platform, state_path=str(state))
    state.write_text(
        json.dumps({platform: {p._today(): {"attempts": 10000, "calls": 9990, "gap_calls": 10000}}})
    )
    assert p.cap is None
    assert p.allow()
    assert p.reserve()
    assert json.loads(state.read_text())[platform][p._today()]["attempts"] == 10001


def test_daily_caps_are_opt_in_per_platform(monkeypatch, tmp_path):
    monkeypatch.setenv(pace.DAILY_CAPS_ENV, '{"linkedin": 5, "venmo": 7}')
    assert pace.Pacer("linkedin", state_path=str(tmp_path / "s.json")).cap == 5
    assert pace.Pacer("venmo", state_path=str(tmp_path / "s.json")).cap == 7
    assert pace.Pacer("facebook", state_path=str(tmp_path / "s.json")).cap is None


@pytest.mark.parametrize(
    "raw",
    ["not json", "[1, 2]", '{"linkedin": "5"}', '{"linkedin": -1}', '{"linkedin": true}'],
)
def test_daily_caps_malformed_environment_fails_at_construction(monkeypatch, tmp_path, raw):
    monkeypatch.setenv(pace.DAILY_CAPS_ENV, raw)
    with pytest.raises(ValueError, match=pace.DAILY_CAPS_ENV):
        pace.Pacer("linkedin", state_path=str(tmp_path / "s.json"))


@pytest.mark.parametrize(
    "text",
    [
        "We need to make sure that you're a human",
        "We need to make sure that you\u2019re a human",
        "I'm not a robot",
    ],
)
def test_captcha_gates_are_challenges(text):
    assert pace.is_challenge(text)


def test_recaptcha_badge_is_not_a_challenge():
    assert not pace.is_challenge(
        "This site is protected by reCAPTCHA and the Google Privacy Policy apply."
    )
