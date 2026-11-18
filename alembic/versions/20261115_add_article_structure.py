"""Preserve original DSL and an optional structured article without replacing IDs.

This migration is additive. It does not backfill or reimport existing entries.
Only the isolated local database has been tested; production rollout is separate.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261115_add_article_structure"
down_revision = "20260531_add_ru_prefix80_index"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("entries", sa.Column("raw_dsl", sa.Text(), nullable=True))
    op.add_column("entries", sa.Column("article", sa.JSON(), nullable=True))


def downgrade():
    # Old application code can run with these nullable columns still present.
    # Dropping them deliberately discards newly preserved article data.
    op.drop_column("entries", "article")
    op.drop_column("entries", "raw_dsl")
