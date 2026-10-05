"""CMS login and the hop from CMS to the LMS.

The CMS student login (ASP.NET WebForms) asks for four fields: enrollment,
password, institute and role. Filling the real form in a browser carries
__VIEWSTATE and __EVENTVALIDATION for us.

The service never solves a CAPTCHA. If the form carries one, login stops
before anything is submitted and the caller pauses for a pasted session.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from ..config import CMSCredentials, PortalUrls
from .errors import BadCredentials, CaptchaRequired, ContractBroken, LmsLinkNotFound, LoginError

FIELDS = {
    "enrollment": "#BodyPH_tbEnrollment",
    "password": "#BodyPH_tbPassword",
    "institute": "#BodyPH_ddlInstituteID",
    "role": "#BodyPH_ddlSubUserType",
    "sign_in": "#BodyPH_btnLogin",
}
CAPTCHA = "#BodyPH_divRecaptcha, .g-recaptcha"
ALERTS = "#pageAlerts_up"

NAV_TIMEOUT_MS = 30_000


def login(page: Page, creds: CMSCredentials, urls: PortalUrls) -> Page:
    """Log in to CMS, follow its LMS link, and return the page now on the LMS.

    The caller saves page.context.storage_state() as the new session.
    """
    login_to_cms(page, creds, urls)
    return open_lms(page, urls)


def login_to_cms(page: Page, creds: CMSCredentials, urls: PortalUrls) -> None:
    page.goto(urls.cms_login, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    check_login_contract(page)
    if captcha_present(page):
        raise CaptchaRequired("CMS login page is showing a CAPTCHA; nothing was submitted")

    page.fill(FIELDS["enrollment"], creds.enrollment)
    page.fill(FIELDS["password"], creds.password)
    page.select_option(FIELDS["institute"], value=option_value(page, "institute", creds.institute))
    page.select_option(FIELDS["role"], value=option_value(page, "role", creds.role))

    try:
        with page.expect_navigation(wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS):
            page.click(FIELDS["sign_in"])
    except PlaywrightTimeout as e:
        if captcha_present(page):
            raise CaptchaRequired("CMS asked for a CAPTCHA on Sign In") from e
        raise LoginError("Sign In did not submit the form; client-side validation may have blocked it") from e

    if on_login_page(page, urls):
        if captcha_present(page):
            raise CaptchaRequired("CMS returned the login page with a CAPTCHA")
        message = page.locator(ALERTS).inner_text().strip() if page.locator(ALERTS).count() else ""
        raise BadCredentials(message or "CMS returned to the login page without an error message")


def open_lms(page: Page, urls: PortalUrls) -> Page:
    """Follow the LMS link from the logged-in CMS page and confirm the LMS accepts it."""
    link = find_lms_link(page, urls)
    if link is None:
        raise LmsLinkNotFound(f"No LMS link on {page.url}; record the CMS dashboard during the spike")

    href = link.get_attribute("href") or ""
    if href and not href.lower().startswith("javascript:") and href != "#":
        # A real URL: go there in this tab. Avoids popups and links hidden in collapsed menus.
        lms_page = page
        lms_page.goto(urljoin(page.url, href), wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    elif (link.get_attribute("target") or "_self") != "_self":
        with page.expect_popup(timeout=NAV_TIMEOUT_MS) as popup:
            link.click()
        lms_page = popup.value
        lms_page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
    else:
        with page.expect_navigation(wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS):
            link.click()
        lms_page = page

    if urlsplit(lms_page.url).hostname != urls.lms_host:
        raise LoginError(f"The LMS link led to {lms_page.url}, not {urls.lms_host}")

    # Confirm the session holds: the dashboard bounces to CMS when it does not.
    lms_page.goto(urls.lms_dashboard, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    if urlsplit(lms_page.url).hostname != urls.lms_host:
        raise LoginError("The LMS did not accept the session from CMS; its dashboard redirected away")
    return lms_page


def check_login_contract(page: Page) -> None:
    missing = [name for name, sel in FIELDS.items() if page.locator(sel).count() == 0]
    if missing:
        raise ContractBroken(f"CMS login page at {page.url} is missing: {', '.join(missing)}")


def captcha_present(page: Page) -> bool:
    # Presence, not visibility: an unloaded reCAPTCHA widget has no size but still blocks Sign In.
    return page.locator(CAPTCHA).count() > 0


def on_login_page(page: Page, urls: PortalUrls) -> bool:
    same_path = urlsplit(page.url).path.lower() == urlsplit(urls.cms_login).path.lower()
    return same_path or page.locator(FIELDS["enrollment"]).count() > 0


def option_value(page: Page, field_name: str, label: str) -> str:
    """Value of the dropdown option whose visible text matches label, ignoring case."""
    options = page.locator(f"{FIELDS[field_name]} option").evaluate_all(
        "els => els.map(e => [e.value, e.textContent.trim()])"
    )
    for value, text in options:
        if text.casefold() == label.strip().casefold():
            return value
    names = ", ".join(text for value, text in options if value)
    raise LoginError(f"No {field_name} named {label!r} on the CMS login form. Options: {names}")


def find_lms_link(page: Page, urls: PortalUrls) -> Locator | None:
    by_href = page.locator(f'a[href*="{urls.lms_host}"]')
    if by_href.count():
        return by_href.first
    by_text = page.get_by_role("link", name=re.compile(r"\bLMS\b", re.IGNORECASE))
    if by_text.count():
        return by_text.first
    return None


# --- Probe: read the login form without a browser and without submitting anything ---


@dataclass
class LoginPageInfo:
    fields_found: dict[str, bool]
    institutes: dict[str, str] = field(default_factory=dict)  # label -> value
    roles: dict[str, str] = field(default_factory=dict)
    captcha: bool = False

    @property
    def contract_ok(self) -> bool:
        return all(self.fields_found.values())


class _LoginFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.classes: set[str] = set()
        self.options: dict[str, dict[str, str]] = {}
        self._select: str | None = None
        self._option_value: str | None = None
        self._option_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id"):
            self.ids.add(a["id"])
        self.classes.update((a.get("class") or "").split())
        if tag == "select":
            self._select = a.get("id")
            self.options.setdefault(self._select or "", {})
        elif tag == "option" and self._select is not None:
            self._option_value = a.get("value", "")
            self._option_text = []

    def handle_data(self, data):
        if self._option_value is not None:
            self._option_text.append(data)

    def handle_endtag(self, tag):
        if tag == "option" and self._option_value is not None:
            label = "".join(self._option_text).strip()
            self.options[self._select or ""][label] = self._option_value
            self._option_value = None
        elif tag == "select":
            self._select = None


def inspect_login_html(html: str) -> LoginPageInfo:
    p = _LoginFormParser()
    p.feed(html)
    return LoginPageInfo(
        fields_found={name: sel.lstrip("#") in p.ids for name, sel in FIELDS.items()},
        institutes=p.options.get(FIELDS["institute"].lstrip("#"), {}),
        roles=p.options.get(FIELDS["role"].lstrip("#"), {}),
        captcha="BodyPH_divRecaptcha" in p.ids or "g-recaptcha" in p.classes,
    )
