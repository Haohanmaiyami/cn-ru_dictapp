import re
from sqlalchemy import select, case, func, text
from sqlalchemy.ext.asyncio import AsyncSession
from dictapp.models import Entry
import unicodedata


def is_valid_hanzi_word(h: str) -> bool:
    if not h:
        return False

    h = h.strip()

    if len(h) < 2:
        return False

    BAD = {"的", "了", "那", "这个", "那个", "的那", "的是", "的话"}

    if h in BAD:
        return False

    return True



_CJK_RE = re.compile(r"[\u3400-\u9FFF]")
_LAT_RE = re.compile(r"[A-Za-z]")
_CYR_RE = re.compile(r"[А-Яа-яЁё]")


def _has_cjk(s: str) -> bool:
    return bool(_CJK_RE.search(s))

def normalize_pinyin(text: str) -> str:
    if not text:
        return ""

    text = text.lower()

    #убираем тоны (nǐ → ni)
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")

    # убираем лишние символы
    text = re.sub(r"[^a-z\s]", "", text)

    # убираем двойные пробелы
    text = " ".join(text.split())

    return text


def _has_cyrillic(s: str) -> bool:
    return bool(_CYR_RE.search(s))


def _looks_like_pinyin(s: str) -> bool:
    if not s or _has_cyrillic(s) or _has_cjk(s):
        return False

    return bool(normalize_pinyin(s))


def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def normalize_query(q: str) -> str:
    if not q:
        return ""
    return " ".join(q.strip().split())

def clean_and_deduplicate_entries(entries: list[Entry]) -> list[Entry]:
    seen: set[str] = set()
    cleaned: list[Entry] = []

    for e in entries:
        hanzi = (e.hanzi or "").strip()
        ru = (e.ru or "").strip()
        pinyin = (e.pinyin or "").strip()

        # Совсем пустой мусор не пропускаем
        if not hanzi and not ru and not pinyin:
            continue

        # Для словарной статьи hanzi должен быть
        if not hanzi:
            continue

        # Убираем дубли по hanzi
        if hanzi in seen:
            continue

        seen.add(hanzi)
        cleaned.append(e)

    return cleaned

def _extract_only_cjk(text: str) -> str:
    return "".join(_CJK_RE.findall(text or ""))

def _generate_cjk_ngrams(text: str, max_len: int = 4) -> list[str]:
    chars = _extract_only_cjk(text)
    if not chars:
        return []

    found: list[str] = []
    seen: set[str] = set()

    for length in range(min(max_len, len(chars)), 0, -1):
        for i in range(0, len(chars) - length + 1):
            piece = chars[i:i + length]
            if piece not in seen:
                seen.add(piece)
                found.append(piece)

    return found

async def search_entries(session: AsyncSession, q: str, limit: int = 30) -> list[Entry]:
    q = normalize_query(q)
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
        results = list(res.scalars().all())
        results = clean_and_deduplicate_entries(results)
        return results[:limit]

    # =========================
    # 2) Поиск по pinyin
    # =========================
    if _looks_like_pinyin(q):
        # нормализуем запрос пользователя
        q_norm = normalize_pinyin(q)

        # Если после нормализации ничего не осталось - выходим
        if not q_norm:
            return []

        # Забираем кандидатов, где pinyin не пустой
        stmt = (
            select(Entry)
            .where(Entry.pinyin.is_not(None))
            .where(func.btrim(Entry.pinyin) != "")
        )
        res = await session.execute(stmt)
        candidates = list(res.scalars().all())

        # фильтруем уже в пайтон после normalize_pinyin
        matched = []
        for entry in candidates:
            entry_pinyin_norm = normalize_pinyin(entry.pinyin or "")
            if q_norm in entry_pinyin_norm:
                matched.append(entry)

        # чистим дубли и режем limit
        matched = clean_and_deduplicate_entries(matched)
        matched = [
            r for r in matched
            if r.hanzi and len(r.hanzi) >= 2
        ]
        return matched[:limit]



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
        exact_items = clean_and_deduplicate_entries(exact_items)

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
        rest_items = clean_and_deduplicate_entries(rest_items)

    print("DEBUG rest_items_count =", len(rest_items))
    for item in rest_items[:5]:
        print("DEBUG rest_item =", item.id, item.hanzi, item.pinyin, item.ru)

    combined = clean_and_deduplicate_entries(exact_items + rest_items)
    return combined[:limit]


async def get_entry_by_id(session: AsyncSession, entry_id: int) -> Entry | None:
    stmt = select(Entry).where(Entry.id == entry_id)
    result = await session.execute(stmt)
    return result.scalars().first()

async def find_dictionary_hits_for_text(
    session: AsyncSession,
    text: str,
    *,
    max_ngram_len: int = 4,
    limit: int = 8,
) -> list[Entry]:
    candidates = _generate_cjk_ngrams(text, max_len=max_ngram_len)
    if not candidates:
        return []

    stmt = (
        select(Entry)
        .where(Entry.hanzi.in_(candidates))
        .order_by(func.length(Entry.hanzi).desc(), Entry.id)
    )

    result = await session.execute(stmt)
    rows = list(result.scalars().all())
    rows = clean_and_deduplicate_entries(rows)

    # убираем мусорные hits
    rows = [
        row for row in rows
        if is_valid_hanzi_word(row.hanzi)
    ]

    multi_char: list[Entry] = []
    single_char: list[Entry] = []

    for row in rows:
        hanzi = (row.hanzi or "").strip()

        if len(hanzi) >= 2:
            multi_char.append(row)
        else:
            single_char.append(row)

    # если уже нашли достаточно нормальных многосимвольных слов,
    # односимвольные вообще не добавляем
    if len(multi_char) >= 3:
        return multi_char[:limit]

    chosen = multi_char[:limit]
    if len(chosen) < limit:
        chosen.extend(single_char[: limit - len(chosen)])

    return chosen