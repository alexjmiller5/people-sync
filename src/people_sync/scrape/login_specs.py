"""Per-platform login specs - the ONE place a selector changes.

Every value here is public knowledge about a public login page: no
credentials, no account identifiers, nothing personal. Selectors are the
best-known shape of each site's login page and are expected to drift; recon
(R6) corrects them, and correcting one is a one-line edit in this table.

A spec that cannot find its username field halts rather than typing into
whatever else is on the page, so a stale selector is loud, not dangerous.

`username` is whatever the site's first field takes: an email, a handle, or
(Partiful) a phone number. Specs with `password_selector = None` are
one-time-code flows - the credential command's `password` is ignored and the
code step does the signing in.
"""

from people_sync.scrape.login import LoginSpec

SPECS: dict[str, LoginSpec] = {
    "instagram": LoginSpec(
        platform="instagram",
        url="https://www.instagram.com/accounts/login/",
        # Meta's current form: name="email" / name="pass", a div[role=button]
        # submit; the older button[type=submit] form is kept as a fallback.
        username_selector='input[name="email"], input[name="username"]',
        password_selector='input[name="pass"], input[name="password"]',
        submit_selector='[role="button"][aria-label="Log In"], button[type="submit"]',
        logged_in_js=(
            '!!document.querySelector(\'svg[aria-label="Home"], a[href="/direct/inbox/"]\')'
        ),
        # Instagram shows one field for both the authenticator code and an
        # SMS code, so both kinds point at it; the dispatcher prefers the
        # TOTP when the credential carries one.
        totp_selector='input[name="verificationCode"]',
        sms_code_selector='input[name="verificationCode"]',
        remember_selector='input[name="rememberDevice"]',
    ),
    "facebook": LoginSpec(
        platform="facebook",
        url="https://www.facebook.com/login/",
        username_selector='input[name="email"]',
        password_selector='input[name="pass"]',
        submit_selector='[role="button"][aria-label="Log In"], button[name="login"]',
        logged_in_js=(
            '!!document.querySelector(\'[aria-label="Your profile"], '
            'div[role="navigation"] a[aria-label="Home"]\')'
        ),
        totp_selector='input[name="approvals_code"]',
        sms_code_selector='input[name="approvals_code"]',
        email_code_selector='input[name="approvals_code"]',
        code_submit_selector='button#checkpointSubmitButton, button[type="submit"]',
        remember_selector='input[value="save_device"]',
    ),
    "linkedin": LoginSpec(
        platform="linkedin",
        url="https://www.linkedin.com/login",
        # LinkedIn's form ships hashed class names and a type="button" Sign in
        # with no other attribute, so Enter in the password field submits.
        username_selector='input#username, input[type="email"]',
        password_selector='input#password, input[type="password"]',
        submit_selector=None,
        logged_in_js="!!document.querySelector('#global-nav, .global-nav')",
        totp_selector='input[name="pin"]',
        email_code_selector="input#input__email_verification_pin",
        sms_code_selector="input#input__phone_verification_pin",
        code_submit_selector='button#two-step-submit-button, button[type="submit"]',
        remember_selector="input#rememberMeOptIn-checkbox",
    ),
    "venmo": LoginSpec(
        platform="venmo",
        # venmo.com/account/sign-in redirects to id.venmo.com: a two-page
        # form (email, Next, then password).
        url="https://venmo.com/account/sign-in",
        username_selector='input[name="login_email"], input#email',
        username_submit_selector="button#btnNext",
        password_selector='input[name="login_password"], input#password, input[type="password"]',
        submit_selector='button#btnLogin, button[type="submit"]',
        logged_in_js=(
            '!!document.querySelector(\'a[href*="/account/profile"], '
            '[data-testid="profile-menu"]\')'
        ),
        sms_code_selector='input[name="code"], input[autocomplete="one-time-code"]',
        email_code_selector='input[name="code"]',
        remember_selector='input[type="checkbox"][name="rememberDevice"]',
    ),
    "spotify": LoginSpec(
        platform="spotify",
        url="https://accounts.spotify.com/en/login",
        # Spotify's current flow: username, Continue, then a one-time code
        # mailed to the account (no password page by default).
        username_selector='input#username, input[name="username"]',
        password_selector=None,
        submit_selector='button[type="submit"], button#login-button',
        logged_in_js=(
            '!!document.querySelector(\'[data-testid="user-widget-link"], '
            '[data-testid="user-widget-name"]\')'
        ),
        email_code_selector='input[name="code"], input[autocomplete="one-time-code"]',
        sms_code_selector='input[autocomplete="one-time-code"]',
    ),
    # Partiful signs in with a phone number and an SMS code - no password.
    "partiful": LoginSpec(
        platform="partiful",
        url="https://partiful.com/login",
        username_selector='input[type="tel"]',
        password_selector=None,
        submit_selector=None,  # no submit button: Enter in the phone field
        logged_in_js='!!document.querySelector(\'a[href^="/u/"], a[href="/me"]\')',
        sms_code_selector='input[autocomplete="one-time-code"], input[name="code"]',
    ),
    # For sites offering only "Continue with Google": the same profile, the
    # same human typing, on Google's own two-page form.
    "google": LoginSpec(
        platform="google",
        url="https://accounts.google.com/ServiceLogin",
        username_selector='input[type="email"]',
        username_submit_selector="#identifierNext",
        password_selector='input[type="password"]',
        submit_selector="#passwordNext",
        logged_in_js="!!document.querySelector('a[aria-label*=\"Google Account\"]')",
        totp_selector='input[name="totpPin"]',
        email_code_selector="input#idvPreregisteredEmailPin",
        sms_code_selector="input#idvPreregisteredPhonePin",
    ),
}
