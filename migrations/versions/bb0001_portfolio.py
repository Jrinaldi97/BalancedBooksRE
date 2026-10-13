"""BalancedBooksRE portfolio dimensions over the existing ledger.

Revision ID: bb0001
Revises: c5e7a9b1d3f5
"""

from alembic import op
from app.database import Base
from app.portfolio import models  # noqa: F401

revision = "bb0001"
down_revision = "c5e7a9b1d3f5"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    for table in Base.metadata.sorted_tables:
        if table.name.startswith("re_"):
            table.create(bind, checkfirst=True)
    if bind.dialect.name == "sqlite":
        from app.portfolio.guards import install

        install(bind)


def downgrade():
    from sqlalchemy import select, func

    bind = op.get_bind()
    tables = [t for t in Base.metadata.sorted_tables if t.name.startswith("re_")]
    if any(bind.execute(select(func.count()).select_from(t)).scalar() for t in tables):
        raise RuntimeError(
            "Portfolio records exist; restore a verified pre-upgrade backup instead of discarding accounting history."
        )
    if bind.dialect.name == "sqlite":
        names = (
            bind.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 're_%'"
            )
            .scalars()
            .all()
        )
        for name in names:
            bind.exec_driver_sql('DROP TRIGGER "' + name.replace('"', '""') + '"')
    for table in reversed(tables):
        table.drop(bind)
