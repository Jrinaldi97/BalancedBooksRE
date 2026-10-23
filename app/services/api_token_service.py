# ============================================================================
# API token service — issue, resolve, and revoke bearer tokens.
#
# resolve() is called from the session middleware for requests carrying
# `Authorization: Bearer sbp_...` and no authenticated session. It opens
# its own short-lived DB session (middleware runs before dependencies).
# ============================================================================

import hashlib
import secrets
import sqlite3
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError

from app.models.api_tokens import TOKEN_PREFIX, ApiToken
from app.models.users import VALID_ROLES  # noqa: F401 — re-exported for routes

# last_used_at is best-effort telemetry; throttle writes so a chatty agent
# doesn't turn every GET into a DB write.
_LAST_USED_RESOLUTION = timedelta(minutes=5)


def generate_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _usage_due(last, now):
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return last is None or (now - last) > _LAST_USED_RESOLUTION


def _record_sqlite_usage(factory, bind, token_id, now):
    """Skip telemetry contention without delaying an already authenticated read."""
    # Hold the connection until its timeout is restored. A Session commit can
    # otherwise return it to the pool while busy_timeout is temporarily zero.
    context = nullcontext(bind) if isinstance(bind, Connection) else bind.connect()
    with context as connection:
        cursor = connection.connection.driver_connection.cursor()
        timeout = cursor.execute("PRAGMA busy_timeout").fetchone()[0]
        try:
            cursor.execute("PRAGMA busy_timeout=0")
            try:
                with factory(bind=connection) as db:
                    row = (
                        db.query(ApiToken)
                        .filter_by(id=token_id, is_active=True)
                        .first()
                    )
                    if row is not None and _usage_due(row.last_used_at, now):
                        row.last_used_at = now
                        db.commit()  # Preserve the normal ORM audit event.
            except OperationalError as error:
                code = getattr(error.orig, "sqlite_errorcode", None)
                if code is None or (code & 0xFF) not in (
                    sqlite3.SQLITE_BUSY,
                    sqlite3.SQLITE_LOCKED,
                ):
                    raise
        finally:
            cursor.execute(f"PRAGMA busy_timeout={int(timeout)}")
            cursor.close()


def resolve(bearer: str) -> dict | None:
    """Bearer secret → {"label", "role"} for an active token, else None."""
    if not bearer or not bearer.startswith(TOKEN_PREFIX):
        return None
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        row = (
            db.query(ApiToken)
            .filter(ApiToken.token_hash == hash_token(bearer), ApiToken.is_active)
            .first()
        )
        if row is None:
            return None
        principal = {"label": row.label, "role": row.role}
        now = datetime.now(timezone.utc)
        if not _usage_due(row.last_used_at, now):
            return principal
        bind = db.get_bind()
        if bind.dialect.name != "sqlite":
            row.last_used_at = now
            db.commit()
            return principal
        token_id = row.id
    finally:
        db.close()
    _record_sqlite_usage(SessionLocal, bind, token_id, now)
    return principal
