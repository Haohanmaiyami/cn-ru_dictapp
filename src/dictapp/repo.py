import re
from sqlalchemy import select, case, literal, or_, func
from sqlalchemy.ext.asyncio import AsyncSession

from dictapp.models import Entry


_CJK_RE = re.compile(r"[\u3400-\u9FFF]")
_LAT_RE = re.compile(r"[A-Za-z]")
_CYR_RE = re.compile(r"[А-Яа-яЁё]")


def _has_cjk(s: str) -> bool:
    return bool(_CJK_RE.search(s))


def _has_cyrillic(s: str) -> bool:
    return bool(_CYR_RE.search(s))


def _looks_like_pinyin(s: str) -> bool:
    # латиница + (возможные диакритики pinyin) + пробелы/апостроф/дефис
    return bool(_LAT_RE.search(s)) and not _has_cyrillic(s) and not _has_cjk(s)


def _escape_like(s: str) -> str:
    # для ILIKE, чтобы % и _ не ломали поиск
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _escape_regex(s: str) -> str:
    return re.escape(s)


async def search_entries(session: AsyncSession, q: str, limit: int = 30) -> list[Entry]:
    q = (q or "").strip()
    if not q:
        return []

    q_like = _escape_like(q)
    q_re = _escape_regex(q)

    # --- 1) Поиск по иероглифам ---
    if _has_cjk(q):
        rank = case(
            (Entry.hanzi == q, 0),
            (Entry.hanzi.ilike(f"{q_like}%"), 1),
            (Entry.hanzi.ilike(f"%{q_like}%"), 2),
            else_=100,
        ).label("rank")

        stmt = (
            select(Entry)
            .where(Entry.hanzi.is_not(None))
            .where(Entry.hanzi.ilike(f"%{q_like}%"))
            .order_by(rank, func.length(Entry.hanzi), Entry.id)
            .limit(limit)
        )
        res = await session.execute(stmt)
        return list(res.scalars().all())

    # --- 2) Поиск по pinyin ---
    if _looks_like_pinyin(q):
        rank = case(
            (Entry.pinyin == q, 0),
            (Entry.pinyin.ilike(f"{q_like}%"), 1),
            (Entry.pinyin.ilike(f"%{q_like}%"), 2),
            else_=100,
        ).label("rank")

        stmt = (
            select(Entry)
            .where(Entry.pinyin.is_not(None))
            .where(Entry.pinyin.ilike(f"%{q_like}%"))
            .order_by(rank, func.length(Entry.pinyin), Entry.id)
            .limit(limit)
        )
        res = await session.execute(stmt)
        return list(res.scalars().all())

    # --- 3) Поиск по русскому (важно: “как в словаре”) ---
    # regex “слово целиком”:
    # \m и \M — границы слова в Postgres (лучше, чем \b для кириллицы)
    whole_word_pattern = rf"\m{q_re}\M"

    rank = case(
        (Entry.ru == q, 0),
        (Entry.ru.ilike(f"{q_like}%"), 1),
        (Entry.ru.op("~*")(whole_word_pattern), 2),  # слово целиком
        (Entry.ru.ilike(f"%{q_like}%"), 3),
        else_=100,
    ).label("rank")

    stmt = (
        select(Entry)
        .where(Entry.ru.is_not(None))
        .where(
            or_(
                Entry.ru.ilike(f"%{q_like}%"),
                Entry.ru.op("~*")(whole_word_pattern),
            )
        )
        .order_by(rank, func.length(Entry.ru), Entry.id)
        .limit(limit)
    )
    res = await session.execute(stmt)
    return list(res.scalars().all())


async def get_entry_by_id(session: AsyncSession, entry_id: int) -> Entry | None:
    stmt = select(Entry).where(Entry.id == entry_id)
    result = await session.execute(stmt)
    return result.scalars().first()