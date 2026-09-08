"""Automated login flows, driven end to end through the real CDP client
against the fake Chrome from test_cdp.py.

`FakeSite` is a tiny stand-in for a login page: it answers the presence,
rect, and page-text evals the flow makes, and records what was typed and
clicked, so the tests assert on the actual CDP traffic rather than on mocks.
No network, no real site, no real credential - every value here is invented.
"""

import json
import re

import pytest

from people_sync.scrape import cdp, login, login_specs
from tests.test_cdp import fake_chrome  # noqa: F401  (pytest fixture)

SPEC = login.LoginSpec(
    platform="testsite",
    url="https://test.example/login",
    username_selector="input#user",
    password_selector="input#pass",
    submit_selector="button#submit",
    logged_in_js="IS_LOGGED_IN",
    totp_selector="input#totp",
    email_code_selector="input#emailcode",
    sms_code_selector="input#smscode",
    remember_selector="input#remember",
)

CRED_COMMAND = """printf '{"username":"%s","password":"pw-synthetic","totp":null}' "$1" """
CRED_COMMAND_TOTP = (
    """printf '{"username":"user-synthetic","password":"pw-synthetic","totp":"654321"}' """
)


class FakeSite:
    """A scripted login page. `present` is the set of selectors currently on
    it; `on_submit`/`on_navigate` let a test advance it to the next step."""

    def __init__(self, chrome, logged_in_js="IS_LOGGED_IN"):
        self.logged_in_js = logged_in_js
        self.present = {"input#user", "input#pass", "button#submit"}
        self.hidden: set[str] = set()  # in the DOM, display:none
        self.checked: set[str] = set()
        self.no_focus: set[str] = set()  # clicking these does not focus them
        self.no_clear: set[str] = set()  # nothing clears it: select-all or Backspace
        self.no_select_all: set[str] = set()  # select-all is a no-op (non-macOS shortcut)
        self.selected: str | None = None  # field whose whole value is selected
        self._target: str | None = None
        self.logged_in = False  # bool, or a callable for a late-rendering nav
        self.text = "Log in to Testsite"
        self.typed: dict[str, str] = {}
        self.clicks: list[str] = []
        self.focus: str | None = None
        self.navigations: list[str] = []
        self.on_submit = None
        self.on_navigate = None
        self.on_click = None
        self.on_typed = None  # (site, field) after each character lands
        for method in (
            "Runtime.evaluate",
            "Input.dispatchKeyEvent",
            "Input.dispatchMouseEvent",
            "Page.navigate",
            "Page.captureScreenshot",
        ):
            chrome.on(method, getattr(self, "_" + method.split(".")[1].lower()))

    # -- CDP handlers ---------------------------------------------------

    @staticmethod
    async def _reply(ws, msg, result):
        await ws.send(
            json.dumps({"id": msg["id"], "sessionId": msg.get("sessionId"), "result": result})
        )

    @staticmethod
    def _selector(expression):
        match = re.search(
            r"var sel=(\".*?\"),l=\[\]\.filter\.call\(document\.querySelectorAll\((\".*?\")\)",
            expression,
        )
        if match:  # a text selector: reconstruct the spec's own spelling
            label, css = json.loads(match.group(1)), json.loads(match.group(2))
            return f"text={label}" if css == cdp._CLICKABLE else f"text[{css}]={label}"
        match = re.search(r"querySelectorAll?\((\".*?\")\)", expression)
        return json.loads(match.group(1)) if match else None

    def _visible(self, selector):
        # a comma list matches like CSS: any visible member
        return any(
            part in self.present and part not in self.hidden
            for part in (selector or "").split(", ")
        )

    async def _evaluate(self, ws, msg):
        expression = msg["params"]["expression"]
        selector = self._selector(expression)
        visible = self._visible(selector)
        if expression == self.logged_in_js:
            value = self.logged_in() if callable(self.logged_in) else self.logged_in
        elif "scrollIntoView" in expression:  # the click target's rect
            self._target = selector if visible else None
            value = {"x": 10.0, "y": 20.0, "width": 100.0, "height": 40.0} if self._target else None
        elif "activeElement" in expression:
            value = visible and self.focus == selector
        elif ".checked" in expression:
            value = visible and selector in self.checked
        elif "value.length" in expression:
            value = len(self.typed.get(selector, "")) if visible else 0
        elif ".value" in expression:
            value = visible and self.typed.get(selector, "") == ""
        elif "checkVisibility" in expression:
            value = visible
        elif "document.title" in expression:
            value = self.text
        else:
            value = None
        await self._reply(ws, msg, {"result": {"value": value}})

    async def _dispatchkeyevent(self, ws, msg):
        params = msg["params"]
        field = self.focus
        if field is None:
            pass
        elif (
            params["type"] == "keyDown"
            and params.get("text")
            and params.get("key") not in ("Enter", "Backspace")
        ):
            if self.selected == field:  # typing replaces a selection
                self.typed[field] = ""
                self.selected = None
            self.typed[field] = self.typed.get(field, "") + params["text"]
            if self.on_typed:
                self.on_typed(self, field)
        elif "selectAll" in params.get("commands", []):
            if field not in self.no_select_all and field not in self.no_clear:
                self.selected = field
        elif params.get("key") == "Enter" and params["type"] == "keyDown":
            self.clicks.append("<enter>")
            if self.on_submit:
                self.on_submit(self)
        elif params.get("key") == "Backspace" and params["type"] == "keyDown":
            if field in self.no_clear:
                pass
            elif self.selected == field:
                self.typed[field] = ""
                self.selected = None
            else:
                self.typed[field] = self.typed.get(field, "")[:-1]
        await self._reply(ws, msg, {})

    async def _dispatchmouseevent(self, ws, msg):
        if msg["params"]["type"] == "mousePressed" and self._target:
            target = self._target
            self.clicks.append(target)
            self.focus = None if target in self.no_focus else target
            if self.on_click:
                self.on_click(self, target)
            if target in ("button#submit", "button#code-submit") and self.on_submit:
                self.on_submit(self)
        await self._reply(ws, msg, {})

    async def _navigate(self, ws, msg):
        self.navigations.append(msg["params"]["url"])
        if self.on_navigate:
            self.on_navigate(self)
        sid = msg.get("sessionId")
        await self._reply(ws, msg, {})
        await ws.send(json.dumps({"method": "Page.loadEventFired", "sessionId": sid, "params": {}}))

    async def _capturescreenshot(self, ws, msg):
        import base64

        await self._reply(ws, msg, {"data": base64.b64encode(b"PNG").decode()})


@pytest.fixture
def site(fake_chrome):  # noqa: F811
    return FakeSite(fake_chrome)


@pytest.fixture(autouse=True)
def _fast(mocker, monkeypatch):
    """No real waiting anywhere: typing jitter, inter-field pauses, and the
    2FA poll all run on patched clocks."""
    clock = {"t": 0.0}

    # cdp's own waits (typing jitter, wait_for polling) advance the same
    # clock but are not recorded - the assertions here are about the flow's
    # pauses, not about keystrokes.
    mocker.patch.object(cdp, "_sleep", lambda s: clock.__setitem__("t", clock["t"] + s))

    def sleep(seconds):
        clock["t"] += seconds
        sleep.calls.append(seconds)

    sleep.calls = []
    mocker.patch.object(login, "_sleep", sleep)
    mocker.patch.object(login, "_now", lambda: clock["t"])
    mocker.patch.object(cdp, "_now", lambda: clock["t"])
    monkeypatch.setitem(login_specs.SPECS, "testsite", SPEC)
    return sleep


def run(fake_chrome, tmp_path, platform="testsite"):  # noqa: F811
    return login.login(
        platform,
        state_dir=str(tmp_path),
        devtools_port_path=fake_chrome.devtools_port_file(tmp_path),
    )


def shots(tmp_path):
    return sorted(p.name for p in tmp_path.glob("login-*.png"))


# -- idempotence -------------------------------------------------------------


def test_already_logged_in_exits_without_typing_anything(
    fake_chrome,  # noqa: F811
    site,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(login.CREDENTIAL_COMMAND_ENV, CRED_COMMAND)
    site.logged_in = True

    result = run(fake_chrome, tmp_path)

    assert result == {"platform": "testsite", "status": "already-logged-in", "reason": None}
    assert site.typed == {}
    assert not any(m.get("method") == "Input.dispatchKeyEvent" for m in fake_chrome.messages)


def test_logged_in_check_happens_before_any_typing(
    fake_chrome,  # noqa: F811
    site,
    tmp_path,
    monkeypatch,
):
    """Mutation guard: reorder the flow so a key is typed before the detector
    runs and this fails."""
    monkeypatch.setenv(login.CREDENTIAL_COMMAND_ENV, CRED_COMMAND)
    site.on_submit = lambda s: setattr(s, "logged_in", True)

    run(fake_chrome, tmp_path)

    methods = [m.get("method") for m in fake_chrome.messages]
    first_key = methods.index("Input.dispatchKeyEvent")
    detector = next(
        i
        for i, m in enumerate(fake_chrome.messages)
        if m.get("method") == "Runtime.evaluate" and m["params"].get("expression") == "IS_LOGGED_IN"
    )
    assert detector < first_key


# -- the happy path ----------------------------------------------------------


def test_types_username_and_password_then_submits(
    fake_chrome,  # noqa: F811
    site,
    tmp_path,
    monkeypatch,
    _fast,
):
    monkeypatch.setenv(login.CREDENTIAL_COMMAND_ENV, CRED_COMMAND)
    site.on_submit = lambda s: setattr(s, "logged_in", True)

    result = run(fake_chrome, tmp_path)

    assert result["status"] == "logged-in"
    # the credential command receives the platform as $1
    assert site.typed == {"input#user": "testsite", "input#pass": "pw-synthetic"}
    assert site.clicks == ["input#user", "input#pass", "button#submit"]
    assert site.navigations == ["https://test.example/login"]
    pauses = [s for s in _fast.calls if 0.3 <= s <= 0.9]
    assert len(pauses) >= 2  # a natural pause between fields and before submit


def test_two_page_flow_clicks_the_username_submit_first(
    fake_chrome,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    spec = SPEC.__class__(**{**SPEC.__dict__, "username_submit_selector": "button#next"})
    monkeypatch.setitem(login_specs.SPECS, "testsite", spec)
    monkeypatch.setenv(login.CREDENTIAL_COMMAND_ENV, CRED_COMMAND)
    site = FakeSite(fake_chrome)
    site.present = {"input#user", "button#next"}

    def reveal_password(s, target):
        if target == "button#next":
            s.present |= {"input#pass", "button#submit"}

    site.on_click = reveal_password
    site.on_submit = lambda s: setattr(s, "logged_in", True)

    result = run(fake_chrome, tmp_path)

    assert result["status"] == "logged-in"
    assert site.clicks == ["input#user", "button#next", "input#pass", "button#submit"]


# -- 2FA dispatch ------------------------------------------------------------


def _show_totp(site):
    site.present = {"input#totp", "button#submit"}


def test_totp_code_comes_from_the_credential_json(
    fake_chrome,  # noqa: F811
    site,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(login.CREDENTIAL_COMMAND_ENV, CRED_COMMAND_TOTP)
    submits = []

    def on_submit(s):
        submits.append(1)
        if len(submits) == 1:
            _show_totp(s)
        else:
            s.logged_in = True

    site.on_submit = on_submit

    result = run(fake_chrome, tmp_path)

    assert result["status"] == "logged-in"
    assert site.typed["input#totp"] == "654321"


def test_email_code_is_polled_until_it_arrives(
    fake_chrome,  # noqa: F811
    site,
    tmp_path,
    monkeypatch,
    _fast,
):
    counter = tmp_path / "count"
    counter.write_text("0")
    monkeypatch.setenv(login.CREDENTIAL_COMMAND_ENV, CRED_COMMAND)
    monkeypatch.setenv(
        login.EMAIL_CODE_COMMAND_ENV,
        f"n=$(cat {counter}); echo $((n+1)) > {counter}; "
        f'if [ "$n" -ge 2 ]; then echo "424242 $1"; fi',
    )
    submits = []

    def on_submit(s):
        submits.append(1)
        if len(submits) == 1:
            s.present = {"input#emailcode", "button#submit"}
        else:
            s.logged_in = True

    site.on_submit = on_submit

    result = run(fake_chrome, tmp_path)

    assert result["status"] == "logged-in"
    # the code command also receives the platform as $1
    assert site.typed["input#emailcode"] == "424242 testsite"
    gaps = [s for s in _fast.calls if 5.0 <= s <= 10.0]
    assert len(gaps) == 2
    assert sum(_fast.calls) < login.CODE_POLL_TIMEOUT_S


def test_sms_code_used_when_the_sms_field_is_the_one_shown(
    fake_chrome,  # noqa: F811
    site,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(login.CREDENTIAL_COMMAND_ENV, CRED_COMMAND)
    monkeypatch.setenv(login.SMS_CODE_COMMAND_ENV, "echo 111222")
    submits = []

    def on_submit(s):
        submits.append(1)
        if len(submits) == 1:
            s.present = {"input#smscode", "button#submit"}
        else:
            s.logged_in = True

    site.on_submit = on_submit

    result = run(fake_chrome, tmp_path)

    assert result["status"] == "logged-in"
    assert site.typed["input#smscode"] == "111222"


def test_remember_this_device_is_ticked_when_offered(
    fake_chrome,  # noqa: F811
    site,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(login.CREDENTIAL_COMMAND_ENV, CRED_COMMAND_TOTP)
    submits = []

    def on_submit(s):
        submits.append(1)
        if len(submits) == 1:
            s.present = {"input#totp", "input#remember", "button#submit"}
        else:
            s.logged_in = True

    site.on_submit = on_submit

    run(fake_chrome, tmp_path)

    assert "input#remember" in site.clicks
    assert site.clicks.index("input#remember") == len(site.clicks) - 2
    assert site.clicks[-1] == "button#submit"  # ticked before the code is submitted


def test_missing_code_command_halts_naming_the_env_var(
    fake_chrome,  # noqa: F811
    site,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(login.CREDENTIAL_COMMAND_ENV, CRED_COMMAND)
    monkeypatch.delenv(login.EMAIL_CODE_COMMAND_ENV, raising=False)
    site.on_submit = lambda s: setattr(s, "present", {"input#emailcode", "button#submit"})

    result = run(fake_chrome, tmp_path)

    assert result["status"] == "halted"
    assert login.EMAIL_CODE_COMMAND_ENV in result["reason"]
    assert shots(tmp_path)


def test_code_that_never_arrives_halts_after_the_poll_window(
    fake_chrome,  # noqa: F811
    site,
    tmp_path,
    monkeypatch,
    _fast,
):
    monkeypatch.setenv(login.CREDENTIAL_COMMAND_ENV, CRED_COMMAND)
    monkeypatch.setenv(login.EMAIL_CODE_COMMAND_ENV, "true")
    site.on_submit = lambda s: setattr(s, "present", {"input#emailcode", "button#submit"})

    result = run(fake_chrome, tmp_path)

    assert result["status"] == "halted"
    assert "code" in result["reason"]
    assert sum(_fast.calls) >= login.CODE_POLL_TIMEOUT_S


# -- halts -------------------------------------------------------------------


@pytest.mark.parametrize(
    "page_text",
    [
        "Confirm your identity - solve the CAPTCHA below",
        "Check your notifications - approve this login on your phone",
        "We noticed an unusual login attempt",
    ],
)
def test_challenge_pages_halt_with_a_screenshot(
    fake_chrome,  # noqa: F811
    site,
    tmp_path,
    monkeypatch,
    page_text,
):
    monkeypatch.setenv(login.CREDENTIAL_COMMAND_ENV, CRED_COMMAND)
    site.text = page_text

    result = run(fake_chrome, tmp_path)

    assert result["status"] == "halted"
    assert site.typed == {}
    assert len(shots(tmp_path)) == 1
    assert shots(tmp_path)[0].startswith("login-testsite-")


def test_unknown_page_without_a_login_form_halts(
    fake_chrome,  # noqa: F811
    site,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(login.CREDENTIAL_COMMAND_ENV, CRED_COMMAND)
    site.present = set()
    site.text = "Service temporarily unavailable"

    result = run(fake_chrome, tmp_path)

    assert result["status"] == "halted"
    assert site.typed == {}
    assert shots(tmp_path)


def test_second_password_failure_halts_without_a_third_attempt(
    fake_chrome,  # noqa: F811
    site,
    tmp_path,
    monkeypatch,
):
    """Mutation guard on the one-retry cap: widen the retry loop and the
    submit count goes to 3."""
    monkeypatch.setenv(login.CREDENTIAL_COMMAND_ENV, CRED_COMMAND)
    submits = []
    site.on_submit = lambda s: submits.append(1)  # never logs in

    result = run(fake_chrome, tmp_path)

    assert result["status"] == "halted"
    assert len(submits) == 2
    assert site.navigations == ["https://test.example/login"] * 2
    assert shots(tmp_path)


def test_no_credential_command_errors_naming_the_env_var(
    fake_chrome,  # noqa: F811
    site,
    tmp_path,
    monkeypatch,
):
    monkeypatch.delenv(login.CREDENTIAL_COMMAND_ENV, raising=False)

    with pytest.raises(login.LoginError, match=login.CREDENTIAL_COMMAND_ENV):
        run(fake_chrome, tmp_path)


def test_credential_command_that_prints_non_json_errors_clearly(
    fake_chrome,  # noqa: F811
    site,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(login.CREDENTIAL_COMMAND_ENV, "echo not-json")

    with pytest.raises(login.LoginError, match="JSON"):
        run(fake_chrome, tmp_path)


def test_unknown_platform_errors_before_connecting(tmp_path):
    with pytest.raises(login.LoginError, match="nosuchsite"):
        login.login("nosuchsite", state_dir=str(tmp_path))


# -- the shipped specs -------------------------------------------------------


def test_every_spec_is_complete_and_self_consistent():
    shipped = set(login_specs.SPECS) - {"testsite"}  # the fixture's synthetic spec
    assert shipped == {
        "instagram",
        "facebook",
        "linkedin",
        "venmo",
        "spotify",
        "partiful",
        "google",
    }
    for platform in shipped:
        spec = login_specs.SPECS[platform]
        assert spec.platform == platform
        assert spec.url.startswith("https://")
        assert spec.username_selector and spec.logged_in_js
        # every spec can answer at least one kind of 2FA prompt
        assert any([spec.totp_selector, spec.email_code_selector, spec.sms_code_selector])


def test_passwordless_spec_skips_the_password_step(
    fake_chrome,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    spec = SPEC.__class__(**{**SPEC.__dict__, "password_selector": None})
    monkeypatch.setitem(login_specs.SPECS, "testsite", spec)
    monkeypatch.setenv(login.CREDENTIAL_COMMAND_ENV, CRED_COMMAND)
    monkeypatch.setenv(login.SMS_CODE_COMMAND_ENV, "echo 999888")
    site = FakeSite(fake_chrome)
    site.present = {"input#user", "button#submit"}
    submits = []

    def on_submit(s):
        submits.append(1)
        if len(submits) == 1:
            s.present = {"input#smscode", "button#submit"}
        else:
            s.logged_in = True

    site.on_submit = on_submit

    result = run(fake_chrome, tmp_path)

    assert result["status"] == "logged-in"
    assert "input#pass" not in site.typed
    assert site.typed["input#smscode"] == "999888"


# -- fix round 1 -------------------------------------------------------------


def two_page_spec(monkeypatch, **overrides):
    spec = SPEC.__class__(
        **{**SPEC.__dict__, "username_submit_selector": "button#next", **overrides}
    )
    monkeypatch.setitem(login_specs.SPECS, "testsite", spec)
    return spec


def test_hidden_password_input_is_never_treated_as_present(
    fake_chrome,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    """Google's identifier page ships a display:none password input. A bare
    querySelector check matched it, the click landed at (0,0) and the
    password went into the visible email field - and got submitted."""
    two_page_spec(monkeypatch)
    monkeypatch.setenv(login.CREDENTIAL_COMMAND_ENV, CRED_COMMAND)
    site = FakeSite(fake_chrome)
    site.present = {"input#user", "button#next", "input#pass"}
    site.hidden = {"input#pass"}

    result = run(fake_chrome, tmp_path)

    assert result["status"] == "halted"
    assert result["reason"] == "password field never appeared"
    assert "pw-synthetic" not in "".join(site.typed.values())
    assert site.typed == {"input#user": "testsite"}


def test_field_that_does_not_take_focus_halts_before_typing(
    fake_chrome,  # noqa: F811
    site,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(login.CREDENTIAL_COMMAND_ENV, CRED_COMMAND)
    site.no_focus = {"input#user"}

    result = run(fake_chrome, tmp_path)

    assert result["status"] == "halted"
    assert "focus" in result["reason"]
    assert site.typed == {}
    assert shots(tmp_path)


def test_prefilled_field_is_cleared_before_typing(
    fake_chrome,  # noqa: F811
    site,
    tmp_path,
    monkeypatch,
):
    """LinkedIn prefills the email. Typing into it produced a concatenated
    value that could only ever fail - and burned the one retry."""
    monkeypatch.setenv(login.CREDENTIAL_COMMAND_ENV, CRED_COMMAND)
    site.typed["input#user"] = "stale-value"
    site.on_submit = lambda s: setattr(s, "logged_in", True)

    result = run(fake_chrome, tmp_path)

    assert result["status"] == "logged-in"
    assert site.typed["input#user"] == "testsite"


def test_field_that_will_not_clear_halts(
    fake_chrome,  # noqa: F811
    site,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(login.CREDENTIAL_COMMAND_ENV, CRED_COMMAND)
    site.typed["input#user"] = "stale-value"
    site.no_clear = {"input#user"}

    result = run(fake_chrome, tmp_path)

    assert result["status"] == "halted"
    assert "clear" in result["reason"]


def test_signed_in_detector_is_waited_for_not_sampled_once(
    fake_chrome,  # noqa: F811
    site,
    tmp_path,
    monkeypatch,
):
    """Client-rendered nav paints late; one eval called a signed-in profile
    signed out, typed into it, and halted with a screenshot of the feed."""
    monkeypatch.setenv(login.CREDENTIAL_COMMAND_ENV, CRED_COMMAND)
    calls = {"n": 0}

    def late_nav():
        calls["n"] += 1
        return calls["n"] > 2

    site.logged_in = late_nav

    result = run(fake_chrome, tmp_path)

    assert result["status"] == "already-logged-in"
    assert site.typed == {}
    assert not shots(tmp_path)


def test_unexpected_cdp_error_halts_with_a_screenshot(
    fake_chrome,  # noqa: F811
    site,
    tmp_path,
    monkeypatch,
):
    """An element that vanishes mid-flow raises CdpError; that must take the
    halt path (screenshot + PII-free line + non-zero), not a traceback."""
    monkeypatch.setenv(login.CREDENTIAL_COMMAND_ENV, CRED_COMMAND)
    site.present = {"input#user", "input#pass"}  # submit button is gone

    result = run(fake_chrome, tmp_path)

    assert result["status"] == "halted"
    assert result["reason"] == "CdpError"
    assert shots(tmp_path)


def test_code_command_timeout_halts_naming_only_the_env_var(
    fake_chrome,  # noqa: F811
    site,
    tmp_path,
    monkeypatch,
):
    """TimeoutExpired's str embeds the full argv - i.e. the credential
    command line - which must never reach a log or a reason string."""
    monkeypatch.setenv(login.CREDENTIAL_COMMAND_ENV, CRED_COMMAND)
    monkeypatch.setenv(login.EMAIL_CODE_COMMAND_ENV, "sleep 30")
    monkeypatch.setattr(login, "COMMAND_TIMEOUT_S", 0.2)
    site.on_submit = lambda s: setattr(s, "present", {"input#emailcode", "button#submit"})

    result = run(fake_chrome, tmp_path)

    assert result["status"] == "halted"
    assert login.EMAIL_CODE_COMMAND_ENV in result["reason"]
    assert "sleep" not in result["reason"]


def test_failing_code_command_halts_instead_of_polling_for_90s(
    fake_chrome,  # noqa: F811
    site,
    tmp_path,
    monkeypatch,
    _fast,
):
    monkeypatch.setenv(login.CREDENTIAL_COMMAND_ENV, CRED_COMMAND)
    monkeypatch.setenv(login.EMAIL_CODE_COMMAND_ENV, "exit 3")
    site.on_submit = lambda s: setattr(s, "present", {"input#emailcode", "button#submit"})

    result = run(fake_chrome, tmp_path)

    assert result["status"] == "halted"
    assert login.EMAIL_CODE_COMMAND_ENV in result["reason"]
    assert not [s for s in _fast.calls if 5.0 <= s <= 10.0]  # never entered the poll


def test_remember_checkbox_already_ticked_is_left_alone(
    fake_chrome,  # noqa: F811
    site,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(login.CREDENTIAL_COMMAND_ENV, CRED_COMMAND_TOTP)
    submits = []

    def on_submit(s):
        submits.append(1)
        if len(submits) == 1:
            s.present = {"input#totp", "input#remember", "button#submit"}
            s.checked = {"input#remember"}
        else:
            s.logged_in = True

    site.on_submit = on_submit

    run(fake_chrome, tmp_path)

    assert "input#remember" not in site.clicks


def test_code_that_is_not_accepted_halts(
    fake_chrome,  # noqa: F811
    site,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(login.CREDENTIAL_COMMAND_ENV, CRED_COMMAND_TOTP)
    site.on_submit = lambda s: setattr(s, "present", {"input#totp", "button#submit"})

    result = run(fake_chrome, tmp_path)

    assert result["status"] == "halted"
    assert "not accepted" in result["reason"]
    assert shots(tmp_path)


def test_no_secret_reaches_the_logs(
    fake_chrome,  # noqa: F811
    site,
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.setenv(
        login.CREDENTIAL_COMMAND_ENV,
        """printf '{"username":"handle-zzz","password":"secret-yyy","totp":null}' """,
    )
    monkeypatch.setenv(login.EMAIL_CODE_COMMAND_ENV, "echo 313373")
    submits = []

    def on_submit(s):
        submits.append(1)
        if len(submits) == 1:
            s.present = {"input#emailcode", "button#submit"}
        else:
            s.logged_in = True

    site.on_submit = on_submit

    result = run(fake_chrome, tmp_path)
    captured = capsys.readouterr()

    assert result["status"] == "logged-in"
    for secret in ("handle-zzz", "secret-yyy", "313373"):
        assert secret not in captured.out
        assert secret not in captured.err


def test_prefilled_field_is_cleared_key_by_key_when_select_all_is_a_no_op(
    fake_chrome,  # noqa: F811
    site,
    tmp_path,
    monkeypatch,
):
    """Select-all is a macOS-only editing command. Where it does nothing the
    flow must still empty the prefilled field (one Backspace per character)
    instead of typing the credential onto the end of it."""
    monkeypatch.setenv(login.CREDENTIAL_COMMAND_ENV, CRED_COMMAND)
    site.typed["input#user"] = "stale-value"
    site.no_select_all = {"input#user"}
    site.on_submit = lambda s: setattr(s, "logged_in", True)

    result = run(fake_chrome, tmp_path)

    assert result["status"] == "logged-in"
    assert site.typed["input#user"] == "testsite"


def test_spec_without_a_submit_selector_submits_with_enter(
    fake_chrome,  # noqa: F811
    site,
    tmp_path,
    monkeypatch,
):
    """A form whose submit button has no stable selector is submitted with a
    trusted Enter in the field just typed into."""
    monkeypatch.setenv(login.CREDENTIAL_COMMAND_ENV, CRED_COMMAND)
    site.present.discard("button#submit")
    site.on_submit = lambda s: setattr(s, "logged_in", True)
    spec = SPEC.__class__(**{**SPEC.__dict__, "submit_selector": None})
    monkeypatch.setitem(login_specs.SPECS, "testsite", spec)

    result = run(fake_chrome, tmp_path)

    assert result["status"] == "logged-in"
    assert site.clicks == ["input#user", "input#pass", "<enter>"]


def test_code_submit_enter_presses_enter_in_the_code_field(
    fake_chrome,  # noqa: F811
    site,
    tmp_path,
    monkeypatch,
):
    """code_submit_selector=ENTER submits the 2FA code with Enter even though
    the credential form itself has a clickable submit button."""
    monkeypatch.setenv(login.CREDENTIAL_COMMAND_ENV, CRED_COMMAND_TOTP)
    spec = SPEC.__class__(
        **{**SPEC.__dict__, "totp_selector": "input#code", "code_submit_selector": login.ENTER}
    )
    monkeypatch.setitem(login_specs.SPECS, "testsite", spec)

    def ask_for_code(s):
        s.present |= {"input#code"}
        s.on_submit = lambda s2: setattr(s2, "logged_in", True)

    site.on_submit = ask_for_code

    result = run(fake_chrome, tmp_path)

    assert result["status"] == "logged-in"
    assert site.clicks[-2:] == ["input#code", "<enter>"]
    assert site.typed["input#code"] == "654321"


def test_code_path_turns_a_push_prompt_into_a_code_prompt(
    fake_chrome,  # noqa: F811
    site,
    tmp_path,
    monkeypatch,
):
    """A site whose default second step is "approve on your other device"
    (a challenge marker) is walked through its "try another way" clicks to
    the authenticator prompt instead of halting."""
    monkeypatch.setenv(login.CREDENTIAL_COMMAND_ENV, CRED_COMMAND_TOTP)
    spec = SPEC.__class__(
        **{
            **SPEC.__dict__,
            "totp_selector": "input#code",
            "code_submit_selector": login.ENTER,
            "code_path": ("text=Try another way", "text=Authentication app", "text=Continue"),
        }
    )
    monkeypatch.setitem(login_specs.SPECS, "testsite", spec)

    def push_prompt(s):
        s.text = "Check your notifications on another device"
        s.present = {"text=Try another way"}

    def advance(s, target):
        if target == "text=Try another way":
            s.present |= {"text=Authentication app", "text=Continue"}
        elif target == "text=Continue":
            s.text = "Enter the 6-digit code"
            s.present = {"input#code"}
            s.on_submit = lambda s2: setattr(s2, "logged_in", True)

    site.on_submit = push_prompt
    site.on_click = advance

    result = run(fake_chrome, tmp_path)

    assert result["status"] == "logged-in"
    assert site.clicks[-5:] == [
        "text=Try another way",
        "text=Authentication app",
        "text=Continue",
        "input#code",
        "<enter>",
    ]
    assert site.typed["input#code"] == "654321"


def test_code_commands_receive_the_request_time_in_the_environment(
    fake_chrome,  # noqa: F811
    site,
    tmp_path,
    monkeypatch,
):
    """A reader must ignore codes that arrived before this login asked for
    one: the moment the credentials went in is handed over as
    PEOPLE_SYNC_CODE_AFTER."""
    monkeypatch.setenv(login.CREDENTIAL_COMMAND_ENV, CRED_COMMAND)
    monkeypatch.setenv(login.SMS_CODE_COMMAND_ENV, 'printf "%s" "$PEOPLE_SYNC_CODE_AFTER"')
    spec = SPEC.__class__(**{**SPEC.__dict__, "sms_code_selector": "input#code"})
    monkeypatch.setitem(login_specs.SPECS, "testsite", spec)

    def ask_for_code(s):
        s.present |= {"input#code"}
        s.on_submit = lambda s2: setattr(s2, "logged_in", True)

    site.on_submit = ask_for_code

    result = run(fake_chrome, tmp_path)

    assert result["status"] == "logged-in"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", site.typed["input#code"])


def test_code_form_that_submits_itself_still_counts_as_logged_in(
    fake_chrome,  # noqa: F811
    site,
    tmp_path,
    monkeypatch,
):
    """Venmo submits on the last digit and navigates away: the button we
    would click is gone (CdpError) but the session is there."""
    monkeypatch.setenv(login.CREDENTIAL_COMMAND_ENV, CRED_COMMAND_TOTP)
    spec = SPEC.__class__(**{**SPEC.__dict__, "totp_selector": "input#code"})
    monkeypatch.setitem(login_specs.SPECS, "testsite", spec)

    def ask_for_code(s):
        s.present = {"input#code"}  # the credential form is gone
        s.on_submit = None

    site.on_submit = ask_for_code

    def auto_submit(s, field):
        if s.typed.get(field) == "654321":
            s.present = set()  # navigated away: nothing left to click
            s.logged_in = True

    site.on_typed = auto_submit

    result = run(fake_chrome, tmp_path)

    assert result["status"] == "logged-in"


def test_password_only_page_skips_the_username(
    fake_chrome,  # noqa: F811
    site,
    tmp_path,
    monkeypatch,
):
    """A site that remembers the account and shows only the password field
    (Venmo) is signed into without a username step."""
    monkeypatch.setenv(login.CREDENTIAL_COMMAND_ENV, CRED_COMMAND)
    site.present = {"input#pass", "button#submit"}
    site.on_submit = lambda s: setattr(s, "logged_in", True)

    result = run(fake_chrome, tmp_path)

    assert result["status"] == "logged-in"
    assert "input#user" not in site.typed
    assert site.typed["input#pass"] == "pw-synthetic"
