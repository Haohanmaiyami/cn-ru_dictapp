import re
from sqlalchemy import select, case, func, text
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
    return bool(_LAT_RE.search(s)) and not _has_cyrillic(s) and not _has_cjk(s)


def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def search_entries(session: AsyncSession, q: str, limit: int = 30) -> list[Entry]:
    q = (q or "").strip()
    if not q:
        return []

    q_like = _escape_like(q)

    # =========================
    # 1) Поиск по иероглифам
    # =========================
    if _has_cjk(q):
        hanzi_rank = case(
            (Entry.hanzi == q, 0),
            (Entry.hanzi.ilike(f"{q_like}%", escape="\\"), 1),
            (Entry.hanzi.ilike(f"%{q_like}%", escape="\\"), 2),
            else_=100,
        ).label("hanzi_rank")

        stmt = (
            select(Entry)
            .where(Entry.hanzi.is_not(None))
            .where(func.btrim(Entry.hanzi) != "")
            .where(Entry.hanzi.ilike(f"%{q_like}%", escape="\\"))
            .order_by(hanzi_rank, func.length(Entry.hanzi), Entry.id)
            .limit(limit)
        )

        res = await session.execute(stmt)
        return list(res.scalars().all())

    # =========================
    # 2) Поиск по pinyin
    # =========================
    if _looks_like_pinyin(q):
        rank = case(
            (Entry.pinyin == q, 0),
            (Entry.pinyin.ilike(f"{q_like}%", escape="\\"), 1),
            (Entry.pinyin.ilike(f"%{q_like}%", escape="\\"), 2),
            else_=100,
        ).label("rank")

        stmt = (
            select(Entry)
            .where(Entry.pinyin.is_not(None))
            .where(func.btrim(Entry.pinyin) != "")
            .where(Entry.pinyin.ilike(f"%{q_like}%", escape="\\"))
            .order_by(rank, func.length(Entry.pinyin), Entry.id)
            .limit(limit)
        )

        res = await session.execute(stmt)
        return list(res.scalars().all())

    # =========================
    # 3) Поиск по русскому
    # =========================
    q_norm = q.strip()
    q_lower = q_norm.lower()
    q_cap = q_lower.capitalize()
    q_title = q_lower.title()

    print("DEBUG SEARCH MODE = RU")
    print("DEBUG q_norm =", q_norm)
    print("DEBUG q_lower =", q_lower)
    print("DEBUG q_cap =", q_cap)
    print("DEBUG q_title =", q_title)

    # exact search без lower() в SQL
    exact_sql = text("""
        select id
        from entries
        where ru is not null
          and btrim(ru) <> ''
          and btrim(ru) <> '_'
          and btrim(ru) in (:q1, :q2, :q3)
        order by length(ru), id
        limit :limit
    """)

    exact_res = await session.execute(
        exact_sql,
        {
            "q1": q_norm,
            "q2": q_cap,
            "q3": q_title,
            "limit": limit,
        },
    )
    exact_ids = [row[0] for row in exact_res.all()]
    print("DEBUG exact_ids =", exact_ids)

    exact_items = []
    if exact_ids:
        exact_order = case(
            *[(Entry.id == entry_id, pos) for pos, entry_id in enumerate(exact_ids)],
            else_=999999,
        )

        exact_stmt = (
            select(Entry)
            .where(Entry.id.in_(exact_ids))
            .order_by(exact_order)
        )
        exact_orm_res = await session.execute(exact_stmt)
        exact_items = list(exact_orm_res.scalars().all())

    print("DEBUG exact_items_count =", len(exact_items))
    for item in exact_items[:5]:
        print("DEBUG exact_item =", item.id, item.hanzi, item.pinyin, item.ru)

    if len(exact_items) >= limit:
        return exact_items

    # partial search тоже без lower(ru), а через варианты q
    rest_sql = text("""
        select id
        from entries
        where ru is not null
          and btrim(ru) <> ''
          and btrim(ru) <> '_'
          and (
                ru like :like1
             or ru like :like2
             or ru like :like3
          )
          and btrim(ru) not in (:q1, :q2, :q3)
        order by
            case
                when ru like :prefix1 then 0
                when ru like :prefix2 then 0
                when ru like :prefix3 then 0
                else 1
            end,
            length(ru),
            id
        limit :rest_limit
    """)

    rest_res = await session.execute(
        rest_sql,
        {
            "q1": q_norm,
            "q2": q_cap,
            "q3": q_title,
            "like1": f"%{q_norm}%",
            "like2": f"%{q_cap}%",
            "like3": f"%{q_title}%",
            "prefix1": f"{q_norm}%",
            "prefix2": f"{q_cap}%",
            "prefix3": f"{q_title}%",
            "rest_limit": limit - len(exact_items),
        },
    )
    rest_ids = [row[0] for row in rest_res.all()]
    print("DEBUG rest_ids =", rest_ids)

    rest_items = []
    if rest_ids:
        rest_order = case(
            *[(Entry.id == entry_id, pos) for pos, entry_id in enumerate(rest_ids)],
            else_=999999,
        )

        rest_stmt = (
            select(Entry)
            .where(Entry.id.in_(rest_ids))
            .order_by(rest_order)
        )
        rest_orm_res = await session.execute(rest_stmt)
        rest_items = list(rest_orm_res.scalars().all())

    print("DEBUG rest_items_count =", len(rest_items))
    for item in rest_items[:5]:
        print("DEBUG rest_item =", item.id, item.hanzi, item.pinyin, item.ru)

    return exact_items + rest_items


async def get_entry_by_id(session: AsyncSession, entry_id: int) -> Entry | None:
    stmt = select(Entry).where(Entry.id == entry_id)
    result = await session.execute(stmt)
    return result.scalars().first()