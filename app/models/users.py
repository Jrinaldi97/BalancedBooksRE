# ============================================================================
# Users — the principal model (Server Edition).
#
# One row per person who can sign in. Single-operator installs have
# exactly one admin row (backfilled from the legacy settings-table
# password hash on first login after upgrade) and never see a username
# field. Roles are stored now, enforced by the RBAC layer.
# ============================================================================

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Integer, String

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


ROLE_ADMIN = "admin"
ROLE_BOOKKEEPER = "bookkeeper"
ROLE_READONLY = "readonly"
VALID_ROLES = (ROLE_ADMIN, ROLE_BOOKKEEPER, ROLE_READONLY)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    # Stored lowercase; lookups lowercase their input.
    username = Column(String(100), unique=True, nullable=False, index=True)
    display_name = Column(String(200), nullable=False, default="")
    password_hash = Column(String(512), nullable=False)
    role = Column(String(20), nullable=False, default=ROLE_ADMIN)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    last_login_at = Column(DateTime, nullable=True)

    # A password, role or active-state change ends previously issued sessions.
    session_version = Column(Integer, nullable=False, default=0, server_default="0")
