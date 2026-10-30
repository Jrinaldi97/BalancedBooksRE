"""Preserve versioned, capacity-checked bank transfer component evidence."""

from alembic import op
from sqlalchemy import select, func, Column, Integer, JSON, inspect
from app.portfolio.models import TransferAssociation

revision = "bb0005"
down_revision = "bb0004"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    TransferAssociation.__table__.create(bind, checkfirst=True)
    columns = {
        column["name"] for column in inspect(bind).get_columns("re_card_aliases")
    }
    if "replaces_alias_id" not in columns:
        op.add_column("re_card_aliases", Column("replaces_alias_id", Integer))
        op.add_column("re_card_aliases", Column("replacement_evidence", JSON))
    from app.portfolio import models as m

    # Model indexes are additive and deterministic; skip existing ones.
    for table in m.Base.metadata.sorted_tables:
        if table.name.startswith("re_"):
            for index in table.indexes:
                index.create(bind, checkfirst=True)
    if bind.dialect.name == "sqlite":
        bind.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_re_events_source_observation ON re_events(json_extract(source, '$.observation_id'))"
        )
        bind.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_re_events_source_observations_kind ON re_events(json_type(source, '$.observation_ids'))"
        )
        from app.portfolio.guards import install

        install(bind)


def downgrade():
    bind = op.get_bind()
    if bind.execute(
        select(func.count()).select_from(TransferAssociation.__table__)
    ).scalar():
        raise RuntimeError(
            "Restore a verified backup; transfer association evidence cannot be discarded."
        )
    if bind.exec_driver_sql(
        "SELECT count(*) FROM re_card_aliases WHERE replaces_alias_id IS NOT NULL"
    ).scalar():
        raise RuntimeError(
            "Restore a verified backup; issuer replacement evidence cannot be discarded."
        )
    TransferAssociation.__table__.drop(bind)
    # Additive nullable columns and performance indexes remain harmless to older readers.
