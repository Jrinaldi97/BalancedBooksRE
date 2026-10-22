"""Append-only effective allocation history. No accounting backfill."""

from alembic import op
from app.portfolio.models import LineAllocation

revision = "bb0002"
down_revision = "bb0001"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    LineAllocation.__table__.create(bind, checkfirst=True)
    if bind.dialect.name == "sqlite":
        from app.portfolio.guards import install

        install(bind)


def downgrade():
    from sqlalchemy import select, func

    bind = op.get_bind()
    if bind.execute(
        select(func.count()).select_from(LineAllocation.__table__)
    ).scalar():
        raise RuntimeError(
            "Restore a verified pre-upgrade backup; allocation history cannot be discarded."
        )
    LineAllocation.__table__.drop(bind)
