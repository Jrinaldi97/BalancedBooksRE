"""Bank-only Payment Rail metadata; historical rails remain unidentified."""

from alembic import op
import sqlalchemy as sa

revision = "bb0007"
down_revision = "bb0006"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("re_bank_observations"):
        return
    if "payment_rail" not in {
        c["name"] for c in sa.inspect(bind).get_columns("re_bank_observations")
    }:
        op.add_column(
            "re_bank_observations",
            sa.Column("payment_rail", sa.String(30), nullable=True),
        )
    bind.exec_driver_sql("""UPDATE re_bank_observations SET payment_rail='Other/Unknown'
        WHERE payment_rail IS NULL AND account_id IN (
            SELECT account_id FROM re_account_scopes WHERE role='bank')""")
    if bind.dialect.name == "sqlite":
        from app.portfolio.payment_rails import install_guards

        install_guards(bind)


def downgrade():
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("re_bank_observations"):
        return
    if bind.exec_driver_sql("SELECT 1 FROM re_bank_observations LIMIT 1").first():
        raise RuntimeError(
            "Restore a verified backup; Payment Rail evidence must not be discarded."
        )
    if bind.dialect.name == "sqlite":
        for suffix in ("default", "INSERT", "UPDATE"):
            bind.exec_driver_sql(f"DROP TRIGGER IF EXISTS re_payment_rail_{suffix}")
    op.drop_column("re_bank_observations", "payment_rail")
