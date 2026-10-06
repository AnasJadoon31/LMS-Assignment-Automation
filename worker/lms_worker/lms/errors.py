class LMSError(Exception):
    """Base for every failure the LMS adapter reports."""


class LoginError(LMSError):
    """CMS login did not produce an LMS session."""


class BadCredentials(LoginError):
    """CMS rejected the login and showed an error. Counts as a failed attempt."""


class CaptchaRequired(LoginError):
    """The login form carries a CAPTCHA. The service never solves one: it
    pauses and waits for a pasted session."""


class LmsLinkNotFound(LoginError):
    """Logged in to CMS but found no way through to the LMS."""


class ContractBroken(LMSError):
    """A page no longer has the elements the adapter expects. Pause, do not guess."""


class SessionExpired(LMSError):
    """The stored LMS session is gone and no re-login is possible right now."""


class ReloginPaused(SessionExpired):
    """Automatic re-login is paused after repeated failures or a CAPTCHA.
    Needs a pasted session or a manual Retry."""


class AlreadySubmitted(LMSError):
    """The row already shows a submission; the service never deletes one to upload again."""

    def __init__(self, filename: str | None):
        super().__init__(f"The LMS already shows a submission ({filename or 'unnamed'})")
        self.filename = filename
