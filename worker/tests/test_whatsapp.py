import httpx
import pytest

from lms_worker.config import EvolutionConfig
from lms_worker.whatsapp import EvolutionAdmin, WhatsAppError

CFG = EvolutionConfig(base_url="http://evolution-api:8080", api_key="k3y", instance="lms")


class FakeEvolution:
    """Just enough of Evolution v2's instance endpoints, with its connect semantics:
    a pairing code is only issued when a connection starts with a number."""

    def __init__(self, exists=False, state="close", code_after=0):
        self.exists = exists
        self.state = state
        self.code_after = code_after  # connect calls before a code is ready
        self.phone = None
        self.calls: list[str] = []
        self.qr_version = 1

    def qrcode(self):
        if self.code_after > 0:
            self.code_after -= 1
            return {"pairingCode": None, "code": None, "base64": None, "count": 0}
        return {
            "pairingCode": "ABCD1234" if self.phone else None,
            "code": f"2@qr-{self.qr_version}",
            "base64": "data:image/png;base64,iVBOR",
            "count": self.qr_version,
        }

    def __call__(self, req: httpx.Request) -> httpx.Response:
        assert req.headers["apikey"] == "k3y"
        path, method = req.url.path, req.method
        self.calls.append(f"{method} {path}" + (f"?{req.url.query.decode()}" if req.url.query else ""))
        if path == "/instance/create":
            self.exists = True
            return httpx.Response(201, json={"instance": {"instanceName": "lms", "status": "created"}})
        if not self.exists:
            return httpx.Response(404, json={"error": 'The "lms" instance does not exist'})
        if path == "/instance/connectionState/lms":
            return httpx.Response(200, json={"instance": {"instanceName": "lms", "state": self.state}})
        if path == "/instance/connect/lms":
            if self.state == "open":
                return httpx.Response(200, json={"instance": {"instanceName": "lms", "state": "open"}})
            if self.state == "close":
                self.phone = req.url.params.get("number")
                self.state = "connecting"
            return httpx.Response(200, json=self.qrcode())
        if path == "/instance/logout/lms" and method == "DELETE":
            self.state, self.phone = "close", None
            return httpx.Response(200, json={"status": "SUCCESS"})
        if path == "/instance/fetchInstances":
            return httpx.Response(200, json=[{"name": "lms", "ownerJid": "923111222333@s.whatsapp.net", "profileName": "LMS Bot"}])
        return httpx.Response(404)


def admin(fake):
    return EvolutionAdmin(CFG, transport=httpx.MockTransport(fake), sleep=lambda s: None)


def test_qr_link_creates_the_instance_first():
    fake = FakeEvolution()
    ch = admin(fake).start_link()
    assert ch.qr_text == "2@qr-1" and ch.qr_png_base64.startswith("data:image/png")
    assert ch.pairing_code is None
    assert fake.calls[:3] == ["GET /instance/connectionState/lms", "POST /instance/create", "GET /instance/connectionState/lms"]
    assert fake.calls[-1] == "GET /instance/connect/lms"


def test_phone_link_returns_a_pairing_code():
    fake = FakeEvolution(exists=True)
    ch = admin(fake).start_link("+92 311 1222333")
    assert ch.pairing_code_display == "ABCD-1234"
    assert "GET /instance/connect/lms?number=923111222333" in fake.calls


def test_phone_link_restarts_a_pending_qr_session():
    fake = FakeEvolution(exists=True, state="connecting")
    ch = admin(fake).start_link("923111222333")
    assert ch.pairing_code == "ABCD1234"
    assert "DELETE /instance/logout/lms" in fake.calls


def test_waits_for_the_code_to_be_ready():
    fake = FakeEvolution(exists=True, code_after=2)
    ch = admin(fake).start_link("923111222333")
    assert ch.pairing_code == "ABCD1234"
    connects = [c for c in fake.calls if "/instance/connect/" in c]
    assert connects == [
        "GET /instance/connect/lms?number=923111222333",
        "GET /instance/connect/lms",
        "GET /instance/connect/lms",
    ]


def test_gives_up_when_no_code_comes():
    fake = FakeEvolution(exists=True, code_after=100)
    with pytest.raises(WhatsAppError, match="did not produce a code"):
        admin(fake).start_link(wait_seconds=6)


def test_already_linked_is_reported_and_status_shows_the_bot():
    fake = FakeEvolution(exists=True, state="open")
    a = admin(fake)
    assert a.start_link().already_linked
    st = a.state()
    assert (st.linked, st.number, st.name) == (True, "923111222333", "LMS Bot")


def test_missing_instance_and_unlink():
    fake = FakeEvolution()
    a = admin(fake)
    assert a.state().state == "missing"
    fake.exists, fake.state = True, "open"
    a.unlink()
    assert fake.state == "close"


def test_bad_number_and_bad_key_are_explained():
    with pytest.raises(WhatsAppError, match="country code"):
        admin(FakeEvolution()).start_link("12345")
    unauthorized = httpx.MockTransport(lambda req: httpx.Response(401, json={"error": "Unauthorized"}))
    with pytest.raises(WhatsAppError, match="EVOLUTION_API_KEY"):
        EvolutionAdmin(CFG, transport=unauthorized).state()
