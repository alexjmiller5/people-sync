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

from contact_sync.scrape.cdp import Browser

log = structlog.get_logger(__name__)

_sleep = time.sleep
_now = time.monotonic

CREDENTIAL_COMMAND_ENV = "CONTACT_SYNC_CREDENTIAL_COMMAND"
EMAIL_CODE_COMMAND_ENV = "CONTACT_SYNC_EMAIL_CODE_COMMAND"
SMS_CODE_COMMAND_ENV = "CONTACT_SYNC_SMS_CODE_COMMAND"
STATE_DIR_ENV = "CONTACT_SYNC_STATE_DIR"
DEFAULT_STATE_DIR = "data"

NAV_WAIT_MS = 15000
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
HALT_MARKERS = (
    "captcha",
    "verify you are human",
    "verify you're human",
    "solve the puzzle",
    "confirm on your phone",
    "approve this login",
    "check your notifications",
    "unusual login",
    "suspicious login",
    "unusual activity",
    "your account has been locked",
    "too many attempts",
    "try again later",
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


def _present_js(selector: str) -> str:
    return f"!!document.querySelector({json.dumps(selector)})"


def _present(browser: Browser, selector: str | None) -> bool:
    return bool(selector) and bool(browser.eval(_present_js(selector)))


def _pause() -> None:
    _sleep(random.uniform(*FIELD_PAUSE_S))


def _run_command(command: str, platform: str) -> str:
    """Run a caller-supplied command with the platform as `$1`. Output is a
    secret: it is returned, never logged."""
    result = subprocess.run(
        ["sh", "-c", command, "contact-sync-login", platform],
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT_S,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _credential(platform: str) -> dict:
    command = os.environ.get(CREDENTIAL_COMMAND_ENV)
    if not command:
        raise LoginError(f"{CREDENTIAL_COMMAND_ENV} is not set - nothing can supply the login")
    output = _run_command(command, platform)
    try:
        credential = json.loads(output)
    except json.JSONDecodeError as e:
        raise LoginError(
            f"{CREDENTIAL_COMMAND_ENV} did not print JSON "
            '({"username": ..., "password": ..., "totp": ...})'
        ) from e
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
    browser.click(spec.username_selector)
    browser.type_text(credential["username"])
    _pause()

    if spec.username_submit_selector:
        browser.click(spec.username_submit_selector)
        if spec.password_selector and not browser.wait_for(
            _present_js(spec.password_selector), FORM_TIMEOUT_S
        ):
            raise LoginHalt("password field never appeared")
        _pause()

    if spec.password_selector:
        browser.click(spec.password_selector)
        browser.type_text(credential.get("password") or "")
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
        code = _run_command(command, platform)
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

    browser.click(selector)
    browser.type_text(code)
    _pause()

    if _present(browser, spec.remember_selector):
        browser.click(spec.remember_selector)
        _pause()

    browser.click(spec.code_submit_selector or spec.submit_selector)
    if not browser.wait_for(spec.logged_in_js, STEP_TIMEOUT_S):
        raise LoginHalt(f"{kind} code was not accepted")


def _sign_in(browser: Browser, spec: LoginSpec) -> str:
    browser.navigate(spec.url, NAV_WAIT_MS)
    if browser.eval(spec.logged_in_js):
        return "already-logged-in"

    # Read only once we know we need it, so a no-op run never touches the
    # credential command at all.
    credential = _credential(spec.platform)
    try:
        for attempt in range(1, MAX_PASSWORD_ATTEMPTS + 1):
            if attempt > 1:
                log.info("retrying login", platform=spec.platform, attempt=attempt)
                browser.navigate(spec.url, NAV_WAIT_MS)
                if browser.eval(spec.logged_in_js):
                    return "logged-in"
            _guard(browser)
            if not browser.wait_for(_present_js(spec.username_selector), FORM_TIMEOUT_S):
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
    except LoginHalt as halt:
        shot = _screenshot(browser, platform, state_dir)
        log.error("login halted", platform=platform, reason=halt.reason, screenshot=shot)
        return {"platform": platform, "status": "halted", "reason": halt.reason}
    finally:
        browser.close()


def _specs() -> dict:
    # Imported lazily: login_specs imports LoginSpec from this module.
    from contact_sync.scrape import login_specs as specs

    return specs.SPECS
