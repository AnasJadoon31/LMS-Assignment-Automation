import pytest
from playwright.sync_api import sync_playwright

from lms_worker.lms.cms_login import inspect_login_html, login
from lms_worker.lms.errors import LmsLinkNotFound, LoginError
from lms_worker.lms.session import session_is_valid

from .conftest import CAPTCHA_HTML, ENROLLMENT, LOGIN_HTML, PASSWORD, make_settings


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def test_login_fills_all_four_fields_and_lands_on_lms(browser, portal, urls, tmp_path):
    page = browser.new_context().new_page()
    lms_page = login(page, make_settings(urls, tmp_path).credentials, urls)

    assert portal.posts == [
        {
            "__VIEWSTATE": "fixture",
            "__EVENTVALIDATION": "fixture",
            "ctl00$BodyPH$tbEnrollment": ENROLLMENT,
            "ctl00$BodyPH$tbPassword": PASSWORD,
            "ctl00$BodyPH$ddlInstituteID": "2",
            "ctl00$BodyPH$ddlSubUserType": "None",
        }
    ]
    assert lms_page.url == urls.lms_dashboard
    assert session_is_valid(page.context.storage_state(), urls)
    page.context.close()


@pytest.mark.parametrize(
    "link",
    [
        '<a href="{lms}/sso.php?token=t0k3n">LMS</a>',
        '<a href="{lms}/sso.php?token=t0k3n" target="_blank">Go to LMS</a>',
        # A script link that opens the LMS in a new tab
        '<a href="javascript:void(0)" target="_blank" onclick="window.open(\'{lms}/sso.php?token=t0k3n\')">LMS</a>',
        # A same-tab script link, found by its text alone
        '<a href="#" onclick="location.href=\'{lms}/sso.php?token=t0k3n\'; return false">LMS</a>',
    ],
)
def test_login_follows_the_lms_link_however_it_is_built(browser, portal, urls, tmp_path, link):
    portal.dashboard_link = link
    page = browser.new_context().new_page()
    lms_page = login(page, make_settings(urls, tmp_path).credentials, urls)
    assert lms_page.url == urls.lms_dashboard
    page.context.close()


def test_institute_and_role_match_by_label_ignoring_case(browser, portal, urls, tmp_path):
    creds = make_settings(urls, tmp_path, institute="karachi campus", role="STUDENT").credentials
    page = browser.new_context().new_page()
    login(page, creds, urls)
    assert portal.posts[0]["ctl00$BodyPH$ddlInstituteID"] == "2"
    page.context.close()


def test_unknown_institute_fails_before_submitting(browser, portal, urls, tmp_path):
    creds = make_settings(urls, tmp_path, institute="Karachi").credentials
    page = browser.new_context().new_page()
    with pytest.raises(LoginError, match="Karachi Campus"):
        login(page, creds, urls)
    assert portal.posts == []
    page.context.close()


def test_missing_lms_link_is_reported(browser, portal, urls, tmp_path):
    portal.dashboard_link = '<a href="/Sys/Student/Profile.aspx">Profile</a>'
    page = browser.new_context().new_page()
    with pytest.raises(LmsLinkNotFound):
        login(page, make_settings(urls, tmp_path).credentials, urls)
    page.context.close()


def test_inspect_login_html_reads_fields_options_and_captcha():
    html = LOGIN_HTML.replace("{{ALERT}}", "")
    info = inspect_login_html(html.replace("{{CAPTCHA}}", ""))
    assert info.contract_ok
    assert info.institutes["Karachi Campus"] == "2"
    assert info.roles == {"Student": "None", "Parents": "Parents"}
    assert not info.captcha

    assert inspect_login_html(html.replace("{{CAPTCHA}}", CAPTCHA_HTML)).captcha
    assert not inspect_login_html("<html></html>").contract_ok
