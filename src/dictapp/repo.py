from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from dictapp.models import Entry



async def search_entries(session: AsyncSession, q: str, limit: int = 20) -> list[Entry]:
    q = q.strip()
    if not q:
        return []


    stmt = (
        select(Entry)
        .where(
            or_(
                Entry.hanzi.ilike(f"%{q}%"),
                Entry.pinyin.ilike(f"%{q}%"),
                Entry.ru.ilike(f"%{q}%"),
            )
        )
        .order_by(Entry.id.asc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_entry_by_id(session: AsyncSession, entry_id: int) -> Entry | None:
    stmt = select(Entry).where(Entry.id == entry_id)
    result = await session.execute(stmt)
    return result.scalars().first()