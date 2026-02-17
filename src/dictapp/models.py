from sqlalchemy import Integer, String, Text, Index
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Entry(Base):
    __tablename__ = "entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    hanzi: Mapped[str] = mapped_column(String(64), nullable=False) # 汉字
    pinyin: Mapped[str | None] = mapped_column(String(128), nullable=True) # pinyin
    ru: Mapped[str] = mapped_column(Text, nullable=False) # in russian translation
    pos: Mapped[str | None] = mapped_column(String(32), nullable=True) # части речи
    examples: Mapped[str | None] = mapped_column(Text, nullable=True) # examples

# indexes для поиска в дальнейшем

Index("ix_entries_hanzi", Entry.hanzi)
Index("ix_entries_pinyin", Entry.pinyin)
