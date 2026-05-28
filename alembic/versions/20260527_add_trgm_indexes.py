"""skip heavy trigram indexes for current small disk server

Revision ID: 20260527_add_trgm_indexes
Revises: b151f9728af6
Create Date: 2026-05-27

"""

from typing import Sequence, Union

from alembic import op


revision: str = "20260527_add_trgm_indexes"
down_revision: Union[str, Sequence[str], None] = "b151f9728af6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # >>> CHANGE:
    # Тяжёлые GIN/TRGM индексы временно отключены.
    # На текущем сервере диск 50GB почти заполнен,
    # поэтому CREATE INDEX ... USING gin (...) падает с No space left on device.
    #
    # Код поиска уже ускорен в repo.py:
    # exact -> prefix -> contains, limit=20, pinyin больше не тянет все строки.
    #
    # Когда будет больше диска, можно будет создать отдельную новую миграцию
    # для trgm-индексов.
    pass


def downgrade() -> None:
    pass