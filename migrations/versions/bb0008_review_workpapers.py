"""Indexed transaction research and nonposting review workpapers."""

from alembic import op
from app.portfolio.models import ResearchRecord
from app.portfolio.guards import install_research_guards

revision = "bb0008"
down_revision = "bb0007"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    ResearchRecord.__table__.create(bind, checkfirst=True)
    if bind.dialect.name == "sqlite":
        install_research_guards(bind)


def downgrade():
    bind = op.get_bind()
    if bind.exec_driver_sql("SELECT 1 FROM re_research_records LIMIT 1").first():
        raise RuntimeError("Restore a verified backup to preserve research history.")
    ResearchRecord.__table__.drop(bind)
