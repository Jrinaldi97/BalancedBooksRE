"""Durable execution identity and separate append-only verification records."""

from sqlalchemy import (
    Column,
    String,
    Integer,
    JSON,
    DateTime,
    ForeignKey,
    CheckConstraint,
    UniqueConstraint,
    func,
)
from app.database import Base


class AgentOperation(Base):
    __tablename__ = "re_agent_operations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('accepted','running','committed','rejected','cancelled')"
        ),
    )
    key = Column(String(120), primary_key=True)
    digest = Column(String(64), nullable=False)
    request = Column(JSON, nullable=False)
    binding = Column(JSON, nullable=False)
    authorization = Column(JSON, nullable=False)
    actor = Column(String(200), nullable=False)
    status = Column(String(20), nullable=False)
    attempt_id = Column(String(32))
    attempt_count = Column(Integer, nullable=False, default=0)
    error = Column(JSON)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class OperationTransition(Base):
    __tablename__ = "re_operation_transitions"
    id = Column(Integer, primary_key=True)
    key = Column(
        String(120), ForeignKey("re_agent_operations.key"), nullable=False, index=True
    )
    status = Column(String(20), nullable=False)
    attempt_id = Column(String(32))
    actor = Column(String(200), nullable=False)
    detail = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class OperationVerification(Base):
    __tablename__ = "re_operation_verifications"
    __table_args__ = (
        UniqueConstraint("supersedes_id"),
        CheckConstraint("status IN ('pending','passed','failed')"),
    )
    id = Column(Integer, primary_key=True)
    key = Column(String(120), ForeignKey("re_requests.key"), nullable=False, index=True)
    effect_digest = Column(String(64), nullable=False)
    scope = Column(String(40), nullable=False)
    status = Column(String(20), nullable=False)
    source_documents = Column(JSON, nullable=False)
    proof_reference = Column(String(2000), nullable=False)
    summary = Column(String(4000), nullable=False)
    supersedes_id = Column(Integer, ForeignKey("re_operation_verifications.id"))
    actor = Column(String(200), nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


def install_guards(connection):
    from sqlalchemy import inspect

    if connection.dialect.name != "sqlite":
        return
    existing = set(inspect(connection).get_table_names())
    for model in (OperationTransition, OperationVerification):
        table = model.__tablename__
        if table not in existing:
            continue
        for action in ("UPDATE", "DELETE"):
            connection.exec_driver_sql(
                f"CREATE TRIGGER IF NOT EXISTS re_guard_{table}_{action} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'Operation history is immutable'); END"
            )
        conflict = "id=NEW.id"
        if model is OperationVerification:
            conflict += " OR (NEW.supersedes_id IS NOT NULL AND supersedes_id=NEW.supersedes_id)"
        connection.exec_driver_sql(
            f"CREATE TRIGGER IF NOT EXISTS re_replace_{table} BEFORE INSERT ON {table} WHEN EXISTS(SELECT 1 FROM {table} WHERE {conflict}) BEGIN SELECT RAISE(ABORT,'Operation history is immutable'); END"
        )
    if AgentOperation.__tablename__ in existing:
        connection.exec_driver_sql(
            "CREATE TRIGGER IF NOT EXISTS re_agent_operation_identity BEFORE UPDATE OF key,digest,request,binding,authorization,actor,created_at ON re_agent_operations BEGIN SELECT RAISE(ABORT,'Accepted operation identity is immutable'); END"
        )
        connection.exec_driver_sql(
            "CREATE TRIGGER IF NOT EXISTS re_agent_operation_delete BEFORE DELETE ON re_agent_operations BEGIN SELECT RAISE(ABORT,'Operation history is immutable'); END"
        )
        connection.exec_driver_sql(
            "CREATE TRIGGER IF NOT EXISTS re_agent_operation_replace BEFORE INSERT ON re_agent_operations WHEN EXISTS(SELECT 1 FROM re_agent_operations WHERE key=NEW.key) BEGIN SELECT RAISE(ABORT,'Accepted operation identity is immutable'); END"
        )
