"""CORS and the security-header middleware must sit OUTSIDE the auth gate.

Starlette makes the LAST-registered middleware the OUTERMOST layer. When
CORSMiddleware and the security-header middleware were registered before
require_session they became the INNER layers, so the gate wrapped them:

  - an unauthenticated CORS preflight (OPTIONS, sent without credentials)
    reached the gate and was answered 401 with no Access-Control-Allow-*
    headers, breaking every cross-origin call after a successful login; and
  - every 401 / 403 / 503 the gate itself produced carried no CSP,
    X-Frame-Options, nosniff or Referrer-Policy.

These tests pin the corrected order: the gate runs INSIDE CORS and the
security headers, so a preflight is answered before auth and the gate's own
refusals still carry the headers. A route that chose a stricter value of its
own (the portal's Referrer-Policy: no-referrer) still wins, because routes
run inside the header layer and it only fills a header the route left unset.
"""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.models.payroll import Employee

# One of the allow-listed origins set in conftest.py; evil.example.com is not.
ALLOWED_ORIGIN = "http://localhost:3001"
DISALLOWED_ORIGIN = "https://evil.example.com"

_REQUIRED_HEADERS = (
    "content-security-policy",
    "x-frame-options",
    "x-content-type-options",
    "referrer-policy",
)


def _assert_security_headers(resp):
    for name in _REQUIRED_HEADERS:
        assert name in resp.headers, f"{name} missing on a {resp.status_code}"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]


# ---------------------------------------------------------------------------
# CORS preflight is answered before auth
# ---------------------------------------------------------------------------


def test_unauthenticated_preflight_from_allowed_origin_is_answered(unauthed_client):
    """A browser sends a preflight WITHOUT credentials; it must be answered
    by CORS, not refused 401 by the gate."""
    r = unauthed_client.options(
        "/api/invoices",
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "GET",
        },
    )
    assert r.status_code in (200, 204), r.text
    assert r.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN
    assert "GET" in r.headers.get("access-control-allow-methods", "")


def test_unauthenticated_preflight_from_disallowed_origin_gets_no_acao(unauthed_client):
    """An unlisted origin is refused: never echoed, never wildcarded."""
    r = unauthed_client.options(
        "/api/invoices",
        headers={
            "Origin": DISALLOWED_ORIGIN,
            "Access-Control-Request-Method": "GET",
        },
    )
    allow_origin = r.headers.get("access-control-allow-origin")
    assert allow_origin != DISALLOWED_ORIGIN
    assert allow_origin != "*"


# ---------------------------------------------------------------------------
# The gate's own refusals carry the security headers
# ---------------------------------------------------------------------------


def test_unauthenticated_api_401_carries_security_headers(unauthed_client):
    r = unauthed_client.get("/api/invoices")
    assert r.status_code == 401
    _assert_security_headers(r)


def test_role_refusal_403_carries_security_headers(client):
    """A readonly token writing is refused 403 by the gate; the refusal is
    still a hardened response."""
    created = client.post("/api/tokens", json={"label": "ro", "role": "readonly"})
    assert created.status_code == 201, created.text
    token = created.json()["token"]
    api = TestClient(app)  # cookie-less, so the Bearer path authenticates
    api.headers["Authorization"] = f"Bearer {token}"
    r = api.post("/api/customers", json={"name": "Nope"})
    assert r.status_code == 403, r.text
    _assert_security_headers(r)


# ---------------------------------------------------------------------------
# Route-specific header opt-in still wins
# ---------------------------------------------------------------------------


def test_portal_route_specific_referrer_policy_still_applies(
    client, db_session: Session
):
    """The portal page opts into Referrer-Policy: no-referrer so its URL token
    cannot leak via Referer. The shared header layer must not clobber it with
    the default strict-origin-when-cross-origin."""
    emp = Employee(
        first_name="Refer",
        last_name="Gate",
        pay_type="hourly",
        pay_rate="20",
        pay_frequency="biweekly",
        filing_status="single",
        is_active=True,
    )
    db_session.add(emp)
    db_session.commit()

    token = client.get(f"/api/employees/{emp.id}/portal-token").json()["portal_token"]
    r = client.get(f"/portal/{token}")
    assert r.status_code == 200
    assert r.headers.get("referrer-policy") == "no-referrer"
