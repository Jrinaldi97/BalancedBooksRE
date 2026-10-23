"""Append-only reviewed reconciliation supersession; no accounting backfill."""

from alembic import op
from app.portfolio.models import ReconciliationRevision

revision = "bb0003"
down_revision = "bb0002"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    ReconciliationRevision.__table__.create(bind, checkfirst=True)
    if bind.dialect.name == "sqlite":
        from app.portfolio.guards import install

        install(bind)


def downgrade():
    from sqlalchemy import select, func

    bind = op.get_bind()
    if bind.execute(
        select(func.count()).select_from(ReconciliationRevision.__table__)
    ).scalar():
        raise RuntimeError(
            "Restore a verified backup; reviewed reconciliation history cannot be discarded."
        )
    ReconciliationRevision.__table__.drop(bind)
