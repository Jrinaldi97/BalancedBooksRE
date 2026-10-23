"""Scoped API tokens (v2.5.1): issuance, bearer auth, role enforcement,
no-self-escalation, revocation, and audit attribution."""

from datetime import datetime, timedelta, timezone
from time import monotonic

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.models.api_tokens import ApiToken
from app.models.audit import AuditLog


def _mint(client, label="agent", role="readonly"):
    r = client.post("/api/tokens", json={"label": label, "role": role})
    assert r.status_code == 201, r.text
    return r.json()


def _bearer(token):
    """Fresh cookie-less client so the Bearer path (not the session) auths."""
    c = TestClient(app)
    c.headers["Authorization"] = f"Bearer {token}"
    return c


# ---------------------------------------------------------------------------
# Issuance + management (admin sessions only)
# ---------------------------------------------------------------------------


def test_create_returns_secret_once(client, db_session):
    created = _mint(client, "claude-code", "readonly")
    assert created["token"].startswith("sbp_")
    assert created["token_hint"] == created["token"][:10]

    listed = client.get("/api/tokens").json()
    assert len(listed) == 1
    assert "token" not in listed[0]  # never again
    row = db_session.query(ApiToken).one()
    assert created["token"] not in (row.token_hash, row.token_hint)  # hashed at rest


def test_validation_and_dupes(client):
    r = client.post("/api/tokens", json={"label": "x1", "role": "boss"})
    assert r.status_code == 400
    _mint(client, "dupe", "readonly")
    r = client.post("/api/tokens", json={"label": "dupe", "role": "readonly"})
    assert r.status_code == 409


def test_non_admin_sessions_cannot_manage(client, db_session):
    from app.models.users import ROLE_BOOKKEEPER, User
    from app.services import auth as auth_service

    db_session.add(
        User(
            username="keeper",
            display_name="Keeper",
            password_hash=auth_service.hash_password("keeper-password-1"),
            role=ROLE_BOOKKEEPER,
            is_active=True,
        )
    )
    db_session.commit()
    client.post("/api/auth/logout")
    client.post(
        "/api/auth/login", json={"username": "keeper", "password": "keeper-password-1"}
    )
    assert client.get("/api/tokens").status_code == 403
    assert (
        client.post("/api/tokens", json={"label": "sneak", "role": "admin"}).status_code
        == 403
    )


# ---------------------------------------------------------------------------
# Bearer authentication + role enforcement
# ---------------------------------------------------------------------------


def test_readonly_token_reads_but_never_writes(client):
    tok = _mint(client, "reporter", "readonly")["token"]
    api = _bearer(tok)
    assert api.get("/api/customers").status_code == 200
    assert api.post("/api/customers", json={"name": "Nope"}).status_code == 403


def test_bookkeeper_token_daily_writes_only(client):
    tok = _mint(client, "keeper-bot", "bookkeeper")["token"]
    api = _bearer(tok)
    assert api.post("/api/customers", json={"name": "Bot Corp"}).status_code in (
        200,
        201,
    )
    assert api.put("/api/settings", json={"invoice_prefix": "T-"}).status_code == 403


def test_tokens_cannot_manage_identities_even_as_admin(client):
    tok = _mint(client, "root-bot", "admin")["token"]
    api = _bearer(tok)
    assert api.put("/api/settings", json={"invoice_prefix": "A-"}).status_code == 200
    # The no-self-escalation wall
    assert api.get("/api/users").status_code == 403
    assert api.get("/api/tokens").status_code == 403
    assert (
        api.post("/api/tokens", json={"label": "evil", "role": "admin"}).status_code
        == 403
    )


def test_bad_and_revoked_tokens_rejected(client):
    assert _bearer("sbp_totally-fake").get("/api/customers").status_code == 401
    created = _mint(client, "shortlived", "readonly")
    api = _bearer(created["token"])
    assert api.get("/api/customers").status_code == 200
    client.put(f"/api/tokens/{created['id']}", json={"is_active": False})
    assert api.get("/api/customers").status_code == 401


def test_no_credentials_still_401(client):
    assert TestClient(app).get("/api/customers").status_code == 401


# ---------------------------------------------------------------------------
# Attribution + telemetry
# ---------------------------------------------------------------------------


def test_token_writes_attributed_in_audit(client, db_session):
    tok = _mint(client, "receipt-service", "bookkeeper")["token"]
    _bearer(tok).post("/api/customers", json={"name": "Attributed Bot Inc"})
    row = (
        db_session.query(AuditLog)
        .filter(AuditLog.table_name == "customers")
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert row is not None
    assert row.username == "token:receipt-service"


def test_last_used_recorded(client, db_session):
    created = _mint(client, "tracked", "readonly")
    assert (
        db_session.query(ApiToken).filter_by(label="tracked").one().last_used_at is None
    )
    _bearer(created["token"]).get("/api/customers")
    db_session.expire_all()
    assert (
        db_session.query(ApiToken).filter_by(label="tracked").one().last_used_at
        is not None
    )


@pytest.fixture
def wal_token_store(tmp_path, monkeypatch):
    import app.database as database
    from app.services.api_token_service import generate_token, hash_token
    from app.services.audit import register_audit_hooks

    engine = create_engine(f"sqlite:///{tmp_path / 'token-reads.db'}")
    database.enable_sqlite_tuning(engine)
    ApiToken.__table__.create(engine)
    AuditLog.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    register_audit_hooks(factory)
    monkeypatch.setattr(database, "SessionLocal", factory)
    bearer = generate_token()
    with factory() as db:
        db.add(
            ApiToken(
                label="polling",
                role="readonly",
                token_hash=hash_token(bearer),
                token_hint=bearer[:10],
            )
        )
        db.commit()

    # Even commit/rollback must not expose a temporarily changed timeout to
    # another request taking this connection from the pool.
    @event.listens_for(engine, "checkin")
    def check_timeout(connection, _record):
        if connection is not None:
            assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000

    try:
        yield engine, factory, bearer
    finally:
        engine.dispose()


@pytest.mark.parametrize("previously_used", [False, True])
def test_token_polling_skips_busy_telemetry_and_recovers(
    wal_token_store, previously_used
):
    from app.services.api_token_service import resolve

    engine, factory, bearer = wal_token_store
    with factory() as db:
        row = db.query(ApiToken).one()
        if previously_used:
            row.last_used_at = datetime.now(timezone.utc) - timedelta(minutes=10)
            db.commit()
        previous = row.last_used_at
        audit_count = db.query(AuditLog).count()

    with engine.connect() as writer:
        writer.exec_driver_sql("BEGIN IMMEDIATE")
        started = monotonic()
        assert resolve(bearer) == {"label": "polling", "role": "readonly"}
        assert monotonic() - started < 1.5  # The normal busy timeout is five seconds.
        with factory() as db:
            assert db.query(ApiToken).one().last_used_at == previous
            assert db.query(AuditLog).count() == audit_count
        writer.rollback()

    assert resolve(bearer) == {"label": "polling", "role": "readonly"}
    with factory() as db:
        recorded = db.query(ApiToken).one().last_used_at
        assert recorded is not None and recorded != previous
        update = db.query(AuditLog).order_by(AuditLog.id.desc()).first()
        assert update.action == "UPDATE"
        assert update.changed_fields == ["last_used_at"]
        assert db.query(AuditLog).count() == audit_count + 1
    resolve(bearer)
    with factory() as db:
        assert db.query(ApiToken).one().last_used_at == recorded
        assert db.query(AuditLog).count() == audit_count + 1
        db.query(ApiToken).one().is_active = False
        db.commit()
    assert resolve(bearer) is None


def test_token_telemetry_propagates_non_lock_errors(wal_token_store):
    from app.services.api_token_service import resolve

    engine, factory, bearer = wal_token_store
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER fail_usage BEFORE UPDATE OF last_used_at ON api_tokens "
            "BEGIN SELECT missing_sqlite_function(); END"
        )
    with pytest.raises(OperationalError, match="no such function"):
        resolve(bearer)
    with factory() as db:
        assert db.query(ApiToken).one().last_used_at is None
        assert db.query(AuditLog).count() == 1
