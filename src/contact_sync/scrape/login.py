"""Fully automated, human-paced login for a site's dedicated Chrome profile.

The flow, per platform (selectors in `login_specs.py`):

    connect -> navigate to the login page -> already signed in? exit
    -> type the username and password with trusted per-key events at human
    jitter, natural pauses between fields, a trusted click on submit
    -> whichever 2FA field the site shows next, fill it from the matching
    source (TOTP from the credential JSON, email/SMS from their commands,
    polled because codes take time to arrive), tick "remember this device"
    -> confirm the signed-in detector.

Anything else - a captcha, a "confirm on your phone" prompt, an unusual-login
interstitial, a page with no login form, a password the site did not accept
twice - stops the run: screenshot to the state dir, one PII-free log line,
non-zero exit. It never guesses at a page it does not recognize, and it never
tries a third password.

Credentials never reach this repo's configuration. Three commands supply
them, named only by environment variable; each receives the platform as `$1`
and prints to stdout:

    CONTACT_SYNC_CREDENTIAL_COMMAND  {"username": ..., "password": ..., "totp": ...}
    CONTACT_SYNC_EMAIL_CODE_COMMAND  the newest one-time code from email
    CONTACT_SYNC_SMS_CODE_COMMAND    the newest one-time code from SMS

What those commands do is none of this product's business. Their output
lives in memory for the duration of the call and is never logged, stored,
or written to the state dir.
"""

import json
import os
import random
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import structlog

from contact_sync.scrape import pace
from contact_sync.scrape.cdp import Browser, visible_js

log = structlog.get_logger(__name__)

_sleep = time.sleep
_now = time.monotonic

CREDENTIAL_COMMAND_ENV = "CONTACT_SYNC_CREDENTIAL_COMMAND"
EMAIL_CODE_COMMAND_ENV = "CONTACT_SYNC_EMAIL_CODE_COMMAND"
SMS_CODE_COMMAND_ENV = "CONTACT_SYNC_SMS_CODE_COMMAND"
STATE_DIR_ENV = "CONTACT_SYNC_STATE_DIR"
DEFAULT_STATE_DIR = "data"

NAV_WAIT_MS = 15000
LOGGED_IN_TIMEOUT_S = 5.0  # client-rendered nav paints late
FIELD_PAUSE_S = (0.3, 0.9)  # a human moving between fields
FORM_TIMEOUT_S = 15.0  # login form to appear
STEP_TIMEOUT_S = 30.0  # submit to produce the next step
CODE_POLL_TIMEOUT_S = 90.0  # a mailed/texted code to arrive
CODE_POLL_GAP_S = (5.0, 10.0)
COMMAND_TIMEOUT_S = 60.0
MAX_PASSWORD_ATTEMPTS = 2  # one retry, never a third

# Page phrases that mean a human is being asked for something this flow must
# never fake or work around. Checked only when no known field is on the page,
# so an ordinary "we texted you a code" step is handled, not halted.
#
# The base list is pace.CHALLENGE_MARKERS (one home for both callers);
# pace.LOGIN_MARKERS is deliberately NOT included - "log in"/"login" describe
# the page this flow exists to drive.
HALT_MARKERS = pace.CHALLENGE_MARKERS + (
    "confirm on your phone",
    "approve this login",
    "check your notifications",
    "unusual login",
    "suspicious login",
    "solve the puzzle",
    "too many attempts",
    "your account has been locked",
)


class LoginError(RuntimeError):
    """Misconfiguration: unknown platform, or a credential command that is
    missing or did not print the JSON this flow needs."""


class LoginHalt(RuntimeError):
    """A page state this flow refuses to drive. Carries a PII-free reason."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class LoginSpec:
    platform: str
    url: str
    username_selector: str
    submit_selector: str
    logged_in_js: str
    password_selector: str | None = None
    # Set for two-page forms (Google): click this after the username, then
    # wait for the password field to appear.
    username_submit_selector: str | None = None
    totp_selector: str | None = None
    email_code_selector: str | None = None
    sms_code_selector: str | None = None
    code_submit_selector: str | None = None
    remember_selector: str | None = None


def _element_js(selector: str, test: str) -> str:
    return (
        f"(function(){{var e=document.querySelector({json.dumps(selector)});"
        f"return !!e&&{test};}})()"
    )


def _focus_js(selector: str) -> str:
    return _element_js(selector, "(e===document.activeElement||e.contains(document.activeElement))")


def _empty_js(selector: str) -> str:
    return _element_js(selector, 'e.value===""')


def _checked_js(selector: str) -> str:
    return _element_js(selector, "e.checked===true")


def _present(browser: Browser, selector: str | None) -> bool:
    """Present AND visible - a display:none duplicate is not a field a human
    could be typing into (see cdp.visible_js)."""
    return bool(selector) and bool(browser.eval(visible_js(selector)))


def _type_into(browser: Browser, selector: str, text: str, label: str) -> None:
    """Click a field, prove it took focus, empty it, then type. Skipping any
    of those sends the value somewhere else: to whatever had focus, or onto
    the end of a value the browser or the site prefilled."""
    browser.click(selector)
    if not browser.eval(_focus_js(selector)):
        raise LoginHalt(f"{label} field did not take focus")
    browser.clear_field()
    if not browser.eval(_empty_js(selector)):
        raise LoginHalt(f"{label} field did not clear")
    browser.type_text(text)


def _pause() -> None:
    _sleep(random.uniform(*FIELD_PAUSE_S))


def _run_command(command: str, platform: str, env_name: str) -> str:
    """Run a caller-supplied command with the platform as `$1`. Both the
    output and the command line are secrets: only `env_name` is ever named in
    an error, and `from None` keeps TimeoutExpired (whose str embeds the full
    argv) out of the chained traceback."""
    try:
        result = subprocess.run(
            ["sh", "-c", command, "contact-sync-login", platform],
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        raise LoginHalt(f"{env_name} timed out after {COMMAND_TIMEOUT_S:.0f}s") from None
    if result.returncode != 0:
        raise LoginHalt(f"{env_name} failed") from None
    return result.stdout.strip()


def _credential(platform: str) -> dict:
    command = os.environ.get(CREDENTIAL_COMMAND_ENV)
    if not command:
        raise LoginError(f"{CREDENTIAL_COMMAND_ENV} is not set - nothing can supply the login")
    output = _run_command(command, platform, CREDENTIAL_COMMAND_ENV)
    try:
        credential = json.loads(output)
    except json.JSONDecodeError:
        # `from None`: JSONDecodeError.doc is the raw stdout, i.e. the
        # credential itself - it must not ride along on the traceback.
        raise LoginError(
            f"{CREDENTIAL_COMMAND_ENV} did not print JSON "
            '({"username": ..., "password": ..., "totp": ...})'
        ) from None
    if not credential.get("username"):
        raise LoginError(f"{CREDENTIAL_COMMAND_ENV} JSON has no username")
    return credential


def _halt_marker(page_text: str) -> str | None:
    lowered = (page_text or "").lower()
    return next((marker for marker in HALT_MARKERS if marker in lowered), None)


def _guard(browser: Browser) -> None:
    marker = _halt_marker(browser.text())
    if marker:
        raise LoginHalt(f"challenge page: {marker}")


def _code_field(browser: Browser, spec: LoginSpec) -> bool:
    return any(
        _present(browser, selector)
        for selector in (spec.totp_selector, spec.email_code_selector, spec.sms_code_selector)
    )


def _fill_credentials(browser: Browser, spec: LoginSpec, credential: dict) -> None:
    _type_into(browser, spec.username_selector, credential["username"], "username")
    _pause()

    if spec.username_submit_selector:
        browser.click(spec.username_submit_selector)
        if spec.password_selector and not browser.wait_for(
            visible_js(spec.password_selector), FORM_TIMEOUT_S
        ):
            raise LoginHalt("password field never appeared")
        _pause()

    if spec.password_selector:
        _type_into(browser, spec.password_selector, credential.get("password") or "", "password")
        _pause()

    browser.click(spec.submit_selector)


def _next_step(browser: Browser, spec: LoginSpec) -> str:
    """What the site did with the credentials: signed us in, asked for a
    code, refused them (retry), or something unrecognized (halt)."""
    deadline = _now() + STEP_TIMEOUT_S
    while True:
        if browser.eval(spec.logged_in_js):
            return "logged-in"
        if _code_field(browser, spec):
            return "2fa"
        _guard(browser)
        if _now() >= deadline:
            break
        _sleep(1.0)

    if _present(browser, spec.password_selector or spec.username_selector):
        return "retry"
    raise LoginHalt("unrecognized page after submit")


def _pick_code_source(browser: Browser, spec: LoginSpec, credential: dict) -> tuple[str, str]:
    """Which 2FA field is on the page AND can actually be answered. A field
    we can see but have no source for is a halt naming the missing command."""
    candidates = [
        ("totp", spec.totp_selector, bool(credential.get("totp")), CREDENTIAL_COMMAND_ENV),
        (
            "email",
            spec.email_code_selector,
            bool(os.environ.get(EMAIL_CODE_COMMAND_ENV)),
            EMAIL_CODE_COMMAND_ENV,
        ),
        (
            "sms",
            spec.sms_code_selector,
            bool(os.environ.get(SMS_CODE_COMMAND_ENV)),
            SMS_CODE_COMMAND_ENV,
        ),
    ]
    shown = [c for c in candidates if c[1] and _present(browser, c[1])]
    for kind, selector, available, _ in shown:
        if available:
            return kind, selector
    if shown:
        missing = ", ".join(sorted({c[3] for c in shown}))
        raise LoginHalt(f"2FA step needs a code source: {missing} is not set")
    raise LoginHalt("2FA step with no field this spec knows")


def _obtain_code(kind: str, platform: str, credential: dict) -> str:
    if kind == "totp":
        return str(credential["totp"])

    env = EMAIL_CODE_COMMAND_ENV if kind == "email" else SMS_CODE_COMMAND_ENV
    command = os.environ[env]
    deadline = _now() + CODE_POLL_TIMEOUT_S
    while True:
        code = _run_command(command, platform, env)
        if code:
            return code
        if _now() >= deadline:
            raise LoginHalt(f"no {kind} code arrived within {CODE_POLL_TIMEOUT_S:.0f}s")
        log.info("waiting for code", platform=platform, kind=kind)
        _sleep(random.uniform(*CODE_POLL_GAP_S))


def _do_2fa(browser: Browser, spec: LoginSpec, credential: dict) -> None:
    kind, selector = _pick_code_source(browser, spec, credential)
    log.info("2fa step", platform=spec.platform, kind=kind)
    code = _obtain_code(kind, spec.platform, credential)

    _type_into(browser, selector, code, "code")
    _pause()

    if _present(browser, spec.remember_selector) and not browser.eval(
        _checked_js(spec.remember_selector)
    ):
        browser.click(spec.remember_selector)
        _pause()

    browser.click(spec.code_submit_selector or spec.submit_selector)
    if not browser.wait_for(spec.logged_in_js, STEP_TIMEOUT_S):
        raise LoginHalt(f"{kind} code was not accepted")


def _sign_in(browser: Browser, spec: LoginSpec) -> str:
    browser.navigate(spec.url, NAV_WAIT_MS)
    if browser.wait_for(spec.logged_in_js, LOGGED_IN_TIMEOUT_S):
        return "already-logged-in"

    # Read only once we know we need it, so a no-op run never touches the
    # credential command at all.
    credential = _credential(spec.platform)
    try:
        for attempt in range(1, MAX_PASSWORD_ATTEMPTS + 1):
            if attempt > 1:
                log.info("retrying login", platform=spec.platform, attempt=attempt)
                browser.navigate(spec.url, NAV_WAIT_MS)
                if browser.wait_for(spec.logged_in_js, LOGGED_IN_TIMEOUT_S):
                    return "logged-in"
            _guard(browser)
            if not browser.wait_for(visible_js(spec.username_selector), FORM_TIMEOUT_S):
                raise LoginHalt("no login form on the page")

            _fill_credentials(browser, spec, credential)
            outcome = _next_step(browser, spec)
            if outcome == "logged-in":
                return "logged-in"
            if outcome == "2fa":
                _do_2fa(browser, spec, credential)
                return "logged-in"
        raise LoginHalt("credentials not accepted")
    finally:
        credential.clear()


def _screenshot(browser: Browser, platform: str, state_dir: str | None) -> str | None:
    directory = Path(state_dir or os.environ.get(STATE_DIR_ENV) or DEFAULT_STATE_DIR)
    path = directory / f"login-{platform}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.png"
    try:
        browser.screenshot(path)
    except Exception as e:  # a halt must survive a failed screenshot
        log.warning("screenshot failed", platform=platform, reason=type(e).__name__)
        return None
    return str(path)


def _halted(browser: Browser, platform: str, state_dir: str | None, reason: str) -> dict:
    shot = _screenshot(browser, platform, state_dir)
    log.error("login halted", platform=platform, reason=reason, screenshot=shot)
    return {"platform": platform, "status": "halted", "reason": reason}


def login(
    platform: str,
    endpoint: str | None = None,
    data_dir: str | None = None,
    state_dir: str | None = None,
    devtools_port_path: str | None = None,
) -> dict:
    """Sign this profile's Chrome into `platform`. Idempotent: an already
    signed-in profile is left alone. Returns the outcome; `status` is
    "already-logged-in", "logged-in", or "halted"."""
    spec = _specs().get(platform)
    if spec is None:
        raise LoginError(f"no login spec for {platform!r}")

    browser = Browser.connect(
        endpoint=endpoint, data_dir=data_dir, devtools_port_path=devtools_port_path
    )
    try:
        status = _sign_in(browser, spec)
        log.info("login done", platform=platform, status=status)
        return {"platform": platform, "status": status, "reason": None}
    except LoginError:
        raise  # misconfiguration, not a page state - let the CLI report it
    except LoginHalt as halt:
        return _halted(browser, platform, state_dir, halt.reason)
    except Exception as e:
        # A CdpError from a vanished element, a dropped websocket, anything:
        # it still takes the halt path. Only the exception TYPE is reported -
        # a message can carry a command line or page content.
        return _halted(browser, platform, state_dir, type(e).__name__)
    finally:
        browser.close()


def _specs() -> dict:
    # Imported lazily: login_specs imports LoginSpec from this module.
    from contact_sync.scrape import login_specs as specs

    return specs.SPECS
