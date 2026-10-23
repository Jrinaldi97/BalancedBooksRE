"""Add durable agent execution, reviewer verification and card source history."""

from alembic import op
from sqlalchemy import select, func
from app.portfolio.operation_models import (
    AgentOperation,
    OperationTransition,
    OperationVerification,
)
from app.portfolio.card_models import CARD_TABLES

revision = "bb0004"
down_revision = "bb0003"
branch_labels = None
depends_on = None


def tables():
    return [
        AgentOperation.__table__,
        OperationTransition.__table__,
        OperationVerification.__table__,
        *CARD_TABLES,
    ]


def upgrade():
    bind = op.get_bind()
    for table in tables():
        table.create(bind, checkfirst=True)
    if bind.dialect.name == "sqlite":
        from app.portfolio.guards import install

        install(bind)


def downgrade():
    bind = op.get_bind()
    if any(
        bind.execute(select(func.count()).select_from(table)).scalar()
        for table in tables()
    ):
        raise RuntimeError(
            "Restore a verified backup; durable operation, verification and card history cannot be discarded."
        )
    for table in reversed(tables()):
        table.drop(bind)
