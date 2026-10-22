"""A class's own LMS session, logged in with its CR's enrollment.

The password is kept (encrypted) only when the CR chose "remember my password"; otherwise only
the session is kept, and when the LMS ends it the class waits for the CR to sign in again.
The class's adapter is only ever used to read: list courses and assignments, download question
files, lectures, outlines and plans.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass

from ..config import CMSCredentials, Settings
from ..db.models import SchoolClass
from ..kv import KeyValue, SecretBox
from ..lms.bahria import BahriaAdapter
from ..lms.errors import BadCredentials, CaptchaRequired, ContractBroken, LMSError, LoginError, ReloginPaused
from ..lms.login_guard import LoginGuard
from ..lms.models import Course
from ..lms.session import SessionStore
from .store import guard_key, session_key


class MemoryKV:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def set(self, key: str, value: str) -> None:
        self.data[key] = value

    def delete(self, key: str) -> None:
        self.data.pop(key, None)


class SignInError(Exception):
    """Why a CR's LMS sign-in did not work, in words for the CR."""


@dataclass
class SignedIn:
    state: dict  # the LMS session (cookies)
    courses: list[Course]


def _role(settings: Settings) -> str:
    return settings.credentials.role if settings.credentials else "Student"


def sign_in(settings: Settings, enrollment: str, password: str, institute: str, *, headless: bool = True,
            adapter_cls=BahriaAdapter) -> SignedIn:
    """Log in once with these details and read the course list. Nothing is stored here."""
    enrollment, institute = enrollment.strip(), (institute or "Karachi Campus").strip()
    if not enrollment or not password:
        raise SignInError("Enter your enrollment and password")
    kv = MemoryKV()
    creds = CMSCredentials(enrollment=enrollment, password=password, institute=institute, role=_role(settings))
    adapter = adapter_cls(dataclasses.replace(settings, credentials=creds), store=SessionStore(kv),
                          guard=LoginGuard(kv, max_failures=1), headless=headless)
    try:
        adapter.relogin()
        courses = adapter.list_courses()
    except BadCredentials as e:
        raise SignInError(f"The LMS did not accept that enrollment and password ({e})") from e
    except (CaptchaRequired, ReloginPaused) as e:
        raise SignInError("The university's login page is asking for a CAPTCHA right now. Try again in a while.") from e
    except ContractBroken as e:
        raise SignInError(f"The LMS looks different than expected: {e}") from e
    except (LoginError, LMSError) as e:
        raise SignInError(f"Could not sign in to the LMS: {e}") from e
    finally:
        adapter.close()
    if not courses:
        raise SignInError("Signed in, but the LMS lists no courses for this semester")
    return SignedIn(state=json.loads(kv.get(SessionStore.KEY) or "{}"), courses=courses)


def class_store(kv: KeyValue, box: SecretBox | None, class_id: int) -> SessionStore:
    store = SessionStore(kv, box)
    store.KEY = session_key(class_id)
    return store


def class_guard(kv: KeyValue, class_id: int) -> LoginGuard:
    guard = LoginGuard(kv)
    guard.KEY = guard_key(class_id)
    return guard


def adapter(settings: Settings, kv: KeyValue, box: SecretBox | None, klass: SchoolClass, *,
            headless: bool = True, adapter_cls=BahriaAdapter) -> BahriaAdapter:
    """The class's adapter: logs in again by itself only if its password is kept."""
    creds = None
    if klass.password_enc and box is not None:
        try:
            creds = CMSCredentials(enrollment=klass.enrollment, password=box.decrypt(klass.password_enc),
                                   institute=klass.institute, role=_role(settings))
        except ValueError:
            creds = None
    return adapter_cls(dataclasses.replace(settings, credentials=creds), store=class_store(kv, box, klass.id),
                       guard=class_guard(kv, klass.id), headless=headless)
