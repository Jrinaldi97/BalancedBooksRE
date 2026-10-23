"""Append-only issuer identities, evidence and card activity applications."""

from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    Date,
    DateTime,
    ForeignKey,
    JSON,
    UniqueConstraint,
    func,
    inspect,
)
from app.database import Base


class CardIdentity(Base):
    __tablename__ = "re_card_identities"
    id = Column(Integer, primary_key=True)
    code = Column(String(64), unique=True, nullable=False)
    account_id = Column(Integer, ForeignKey("accounts.id"), unique=True, nullable=False)
    entity_id = Column(Integer, ForeignKey("re_entities.id"), nullable=False)
    issuer = Column(String(100), nullable=False)
    evidence = Column(Text, nullable=False)
    actor = Column(String(200), nullable=False)
    created_at = Column(DateTime, server_default=func.now())


class CardAlias(Base):
    __tablename__ = "re_card_aliases"
    __table_args__ = (UniqueConstraint("issuer", "alias"),)
    id = Column(Integer, primary_key=True)
    card_id = Column(Integer, ForeignKey("re_card_identities.id"), nullable=False)
    issuer = Column(String(100), nullable=False)
    alias = Column(String(100), nullable=False)
    start = Column(Date, nullable=False)
    end = Column(Date)
    evidence = Column(Text, nullable=False)
    actor = Column(String(200), nullable=False)
    created_at = Column(DateTime, server_default=func.now())


class CardCoverage(Base):
    __tablename__ = "re_card_coverage"
    id = Column(Integer, primary_key=True)
    card_id = Column(Integer, ForeignKey("re_card_identities.id"), nullable=False)
    start = Column(Date, nullable=False)
    end = Column(Date, nullable=False)
    opening_liability_cents = Column(Integer, nullable=False)
    ending_liability_cents = Column(Integer, nullable=False)
    observation_ids = Column(JSON, nullable=False)
    document_id = Column(Integer, ForeignKey("re_documents.id"), nullable=False)
    sha256 = Column(String(64), nullable=False)
    supersedes_id = Column(Integer, ForeignKey("re_card_coverage.id"), unique=True)
    evidence = Column(Text, nullable=False)
    actor = Column(String(200), nullable=False)
    created_at = Column(DateTime, server_default=func.now())


class CardActivity(Base):
    __tablename__ = "re_card_activities"
    id = Column(Integer, primary_key=True)
    code = Column(String(64), unique=True, nullable=False)
    card_id = Column(Integer, ForeignKey("re_card_identities.id"), nullable=False)
    kind = Column(String(32), nullable=False)
    observation_id = Column(
        Integer, ForeignKey("re_bank_observations.id"), unique=True, nullable=False
    )
    event_id = Column(Integer, ForeignKey("re_events.id"), nullable=False)
    bank_observation_id = Column(
        Integer, ForeignKey("re_bank_observations.id"), unique=True
    )
    bank_event_id = Column(Integer, ForeignKey("re_events.id"))
    parent_id = Column(Integer, ForeignKey("re_card_activities.id"))
    retry_of_id = Column(Integer, ForeignKey("re_card_activities.id"))
    item = Column(String(100), nullable=False)
    amount_cents = Column(Integer, nullable=False)
    components = Column(JSON, nullable=False)
    evidence = Column(Text, nullable=False)
    actor = Column(String(200), nullable=False)
    created_at = Column(DateTime, server_default=func.now())


CARD_TABLES = [
    CardIdentity.__table__,
    CardAlias.__table__,
    CardCoverage.__table__,
    CardActivity.__table__,
]


def install_card_guards(connection):
    if connection.dialect.name != "sqlite":
        return
    keys = {
        "re_card_identities": "id=NEW.id OR account_id=NEW.account_id OR code=NEW.code",
        "re_card_aliases": "id=NEW.id OR (issuer=NEW.issuer AND alias=NEW.alias)",
        "re_card_coverage": "id=NEW.id OR (supersedes_id IS NOT NULL AND supersedes_id=NEW.supersedes_id)",
        "re_card_activities": "id=NEW.id OR code=NEW.code OR observation_id=NEW.observation_id OR (bank_observation_id IS NOT NULL AND bank_observation_id=NEW.bank_observation_id)",
    }
    for table in CARD_TABLES:
        if not inspect(connection).has_table(table.name):
            continue
        for operation in ("UPDATE", "DELETE"):
            connection.exec_driver_sql(
                f"CREATE TRIGGER IF NOT EXISTS re_guard_{table.name}_{operation} BEFORE {operation} ON {table.name} BEGIN SELECT RAISE(ABORT,'Card source history is immutable'); END"
            )
        connection.exec_driver_sql(
            f"CREATE TRIGGER IF NOT EXISTS re_guard_{table.name}_replace BEFORE INSERT ON {table.name} WHEN EXISTS(SELECT 1 FROM {table.name} WHERE {keys[table.name]}) BEGIN SELECT RAISE(ABORT,'Card source history is immutable'); END"
        )
