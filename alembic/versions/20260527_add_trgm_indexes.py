"""add trigram indexes for faster dictionary search

Revision ID: 20260527_add_trgm_indexes
Revises: b151f9728af6
Create Date: 2026-05-27

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260527_add_trgm_indexes"
down_revision: Union[str, Sequence[str], None] = "b151f9728af6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # >>> CHANGE: включаем PostgreSQL extension pg_trgm
    # Он нужен для ускорения LIKE / ILIKE / contains search
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # >>> CHANGE: создаем индексы CONCURRENTLY
    # Так безопаснее для большой таблицы, потому что таблица меньше блокируется
    with op.get_context().autocommit_block():

        # >>> CHANGE: ускоряет поиск по hanzi, особенно contains-search
        # Например: WHERE hanzi ILIKE '%难听%'
        op.execute("""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_entries_hanzi_trgm
            ON entries
            USING gin (hanzi gin_trgm_ops)
        """)

        # >>> CHANGE: ускоряет поиск по ru
        # Например: WHERE ru LIKE '%слово%'
        op.execute("""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_entries_ru_trgm
            ON entries
            USING gin (ru gin_trgm_ops)
        """)

        # >>> CHANGE: ускоряет lower(ru) LIKE ...
        # Потому что в коде русского поиска используется lower(ru)
        op.execute("""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_entries_lower_ru_trgm
            ON entries
            USING gin ((lower(ru)) gin_trgm_ops)
        """)


def downgrade() -> None:
    # >>> CHANGE: откат индексов, если вдруг надо будет rollback
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_entries_lower_ru_trgm")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_entries_ru_trgm")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_entries_hanzi_trgm")