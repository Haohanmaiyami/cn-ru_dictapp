"""add ru prefix80 index

Revision ID: 20260531_add_ru_prefix80_index
Revises: 20260527_add_trgm_indexes
Create Date: 2026-05-31
"""

from typing import Sequence, Union

from alembic import op


revision: str = "20260531_add_ru_prefix80_index"
down_revision: Union[str, Sequence[str], None] = "20260527_add_trgm_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_entries_ru_prefix80
            ON entries ((left(lower(ru), 80)) text_pattern_ops)
            WHERE ru IS NOT NULL
              AND btrim(ru) <> ''
              AND btrim(ru) <> '_';
        """)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("""
            DROP INDEX CONCURRENTLY IF EXISTS ix_entries_ru_prefix80;
        """)