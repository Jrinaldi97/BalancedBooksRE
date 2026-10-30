"""Adapted upstream secret redaction and permanent revocation contracts."""

from app.models.users import User
from app.models.settings import Settings
from app.models.audit import AuditLog
from app.services.audit import log_event
from app.services.audit_redaction import redact_values


def test_new_audit_snapshots_redact_password_updates_and_setting_values(db_session):
    db = db_session
    user = User(
        username="audit-user",
        display_name="Audit",
        password_hash="initial-secret-hash",
        role="readonly",
        is_active=True,
    )
    setting = Settings(key="company_session_id", value="initial-session-secret")
    db.add_all([user, setting])
    db.flush()
    user.password_hash = "updated-secret-hash"
    setting.value = "updated-session-secret"
    db.flush()
    db.flush()  # Persist after_flush-generated audit entries too.
    logs = (
        db.query(AuditLog).filter(AuditLog.table_name.in_(["users", "settings"])).all()
    )
    raw = str([(r.old_values, r.new_values) for r in logs])
    for secret in (
        "initial-secret-hash",
        "updated-secret-hash",
        "initial-session-secret",
        "updated-session-secret",
    ):
        assert secret not in raw
    assert any(
        r.action == "UPDATE" and "password_hash" in r.changed_fields for r in logs
    )
    log_event(
        db, "api_tokens", 1, "UPDATE", new_values={"token_hash": "never-show-this"}
    )
    db.flush()
    assert (
        db.query(AuditLog)
        .filter_by(table_name="api_tokens")
        .one()
        .new_values["token_hash"]
        == "***"
    )


def test_historical_secret_read_redacts_without_mutating_audit(client, db_session):
    row = AuditLog(
        table_name="settings",
        record_id=123,
        action="UPDATE",
        old_values={"value": "historical-secret"},
        new_values={"value": "also-secret"},
        source="api",
        username="operator",
    )
    db_session.add(row)
    db_session.commit()
    result = client.get("/api/audit", params={"record_id": 123}).json()
    assert result[0]["old_values"]["value"] == "***"
    assert result[0]["new_values"]["value"] == "***"
    assert db_session.get(AuditLog, row.id).old_values["value"] == "historical-secret"
    evidence = {"sha256": "a" * 64, "source_hash": "b" * 64, "digest": "c" * 64}
    assert redact_values("re_documents", evidence) == evidence


def test_revoked_api_token_cannot_be_reenabled(client):
    created = client.post(
        "/api/tokens", json={"label": "permanent-revoke", "role": "readonly"}
    ).json()
    assert (
        client.put(
            f"/api/tokens/{created['id']}", json={"is_active": False}
        ).status_code
        == 200
    )
    assert (
        client.put(f"/api/tokens/{created['id']}", json={"is_active": True}).status_code
        == 409
    )
