import re
from sqlalchemy import select, case, func, text, bindparam
from sqlalchemy.ext.asyncio import AsyncSession
from dictapp.models import Entry
import unicodedata
from pypinyin import lazy_pinyin, Style



def is_valid_hanzi_word(h: str) -> bool:
    if not h:
        return False

    h = h.strip()

    if len(h) < 2:
        return False

    BAD = {
        "的", "了", "那", "这个", "那个",
        "的那", "的是", "的话",
        "个是", "是太", "的是太",
    }

    if h in BAD:
        return False

    return True



_CJK_RE = re.compile(r"[\u3400-\u9FFF]")
_LAT_RE = re.compile(r"[A-Za-z]")
_CYR_RE = re.compile(r"[А-Яа-яЁё]")


def _has_cjk(s: str) -> bool:
    return bool(_CJK_RE.search(s))


def pinyin_bonus(entry: Entry) -> int:
    hanzi = (entry.hanzi or "").strip()
    ru = (entry.ru or "").strip().lower()
    pinyin = normalize_pinyin(entry.pinyin or "")

    bonus = 0

    # базовые короткие слова поднимаем выше
    if len(hanzi) == 2:
        bonus -= 3
    elif len(hanzi) == 3:
        bonus -= 1

    # если перевод короткий и чистый — чаще это базовое значение
    if ru and len(ru) <= 12:
        bonus -= 2

    # приветствия и очень частотные случаи можно поднять ещё чуть выше
    if hanzi in {"你好", "您好"}:
        bonus -= 10

    # если pinyin очень длинный, чуть опускаем
    if len(pinyin) > 8:
        bonus += 1

    return bonus

def pinyin_priority(entry):
    h = (entry.hanzi or "").strip()
    if len(h) == 2:
        return 0
    if len(h) == 3:
        return 1
    return 2

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
    # убрать мусор типа "_" или "_ "
    text = re.sub(r"^_+\s*", "", text)

    return text


def generate_pinyin_from_hanzi(hanzi: str | None) -> str:
    if not hanzi:
        return ""

    hanzi = hanzi.strip()
    if not hanzi:
        return ""

    try:
        return " ".join(lazy_pinyin(hanzi, style=Style.TONE))
    except Exception:
        return ""

def get_effective_pinyin(entry: Entry) -> str:
    existing = (entry.pinyin or "").strip()
    if existing:
        return existing

    return generate_pinyin_from_hanzi(entry.hanzi)

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

def _entry_quality_score(entry: Entry) -> int:
    ru = (entry.ru or "").strip()
    examples = getattr(entry, "examples", None) or ""

    score = 0

    if ru:
        score += 10

    if examples:
        score += 30

    if any("\u3400" <= ch <= "\u9fff" for ch in ru):
        score += 20

    if len(ru) > 80:
        score += 10

    if ";" in ru or "；" in ru:
        score += 5

    return score


def clean_and_deduplicate_entries(entries: list[Entry]) -> list[Entry]:
    by_hanzi: dict[str, Entry] = {}

    for e in entries:
        hanzi = (e.hanzi or "").strip()
        ru = (e.ru or "").strip()
        pinyin = (e.pinyin or "").strip()

        if hanzi in {"-", "—", "_"}:
            continue

        if not hanzi and not ru and not pinyin:
            continue

        if not hanzi:
            continue

        current = by_hanzi.get(hanzi)

        if current is None:
            by_hanzi[hanzi] = e
            continue

        if _entry_quality_score(e) > _entry_quality_score(current):
            by_hanzi[hanzi] = e

    return list(by_hanzi.values())

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
    # 1) Быстрый поиск по иероглифам
    # =========================
    if _has_cjk(q):

        # сначала exact search
        # Это самый быстрый вариант: Entry.hanzi == q
        # Например, для 难听 сначала ищем именно 难听
        exact_stmt = (
            select(Entry)
            .where(Entry.hanzi == q)
            .order_by(Entry.id)
            .limit(limit)
        )

        exact_res = await session.execute(exact_stmt)
        exact_items = clean_and_deduplicate_entries(list(exact_res.scalars().all()))

        if len(exact_items) >= limit:
            return exact_items[:limit]

        # потом prefix search
        # Например: 难听%
        # Это быстрее и полезнее, чем сразу искать %难听%
        prefix_stmt = (
            select(Entry)
            .where(Entry.hanzi.is_not(None))
            .where(func.btrim(Entry.hanzi) != "")
            .where(Entry.hanzi != q)
            .where(Entry.hanzi.ilike(f"{q_like}%", escape="\\"))
            .order_by(func.length(Entry.hanzi), Entry.id)
            .limit(max(50, limit * 3))
        )

        prefix_res = await session.execute(prefix_stmt)
        prefix_items = clean_and_deduplicate_entries(list(prefix_res.scalars().all()))

        combined = clean_and_deduplicate_entries(exact_items + prefix_items)

        if len(combined) >= limit:
            return combined[:limit]

        # contains search делаем только после exact + prefix
        # И только если запрос 2+ иероглифа.
        # Для одного иероглифа %字% может быть очень тяжелым на базе 6M+ строк.
        if len(q) >= 2:
            contains_stmt = (
                select(Entry)
                .where(Entry.hanzi.is_not(None))
                .where(func.btrim(Entry.hanzi) != "")
                .where(Entry.hanzi.ilike(f"%{q_like}%", escape="\\"))
                .order_by(func.length(Entry.hanzi), Entry.id)
                .limit(max(50, limit * 3))
            )

            contains_res = await session.execute(contains_stmt)
            contains_items = clean_and_deduplicate_entries(list(contains_res.scalars().all()))

            combined = clean_and_deduplicate_entries(combined + contains_items)

        return combined[:limit]

    # =========================
    # 2) Быстрый поиск по pinyin
    # =========================
    if _looks_like_pinyin(q):
        q_norm = normalize_pinyin(q)
        q_norm_flat = q_norm.replace(" ", "")
        q_tokens = q_norm.split()

        if not q_norm:
            return []

        # берем первую букву pinyin
        # Например ni hao -> n
        # Это нужно, чтобы не вытаскивать все 6 миллионов записей
        first_letter = _escape_like(q_norm[0])

        # в старой версии тут было:
        # select(Entry).where(Entry.pinyin.is_not(None))
        # То есть backend вытаскивал все pinyin-записи из базы в Python.
        # Это очень медленно.
        #
        # Теперь берем только кандидатов, у которых pinyin начинается с первой буквы.
        stmt = (
            select(Entry)
            .where(Entry.pinyin.is_not(None))
            .where(func.btrim(Entry.pinyin) != "")
            .where(Entry.pinyin.ilike(f"{first_letter}%", escape="\\"))
            .order_by(func.length(Entry.pinyin), Entry.id)
            .limit(max(1000, limit * 100))
        )

        res = await session.execute(stmt)
        candidates = list(res.scalars().all())

        matched_with_rank = []

        for entry in candidates:
            hanzi = (entry.hanzi or "").strip()
            if not hanzi or hanzi in {"-", "—", "_"}:
                continue

            entry_pinyin_norm = normalize_pinyin(get_effective_pinyin(entry))
            ru_text = (entry.ru or "").strip().lower()

            bad_ru_penalty = 0
            if ru_text in {"превед", "браво"}:
                bad_ru_penalty = 50

            if not entry_pinyin_norm:
                continue

            entry_pinyin_flat = entry_pinyin_norm.replace(" ", "")
            entry_tokens = entry_pinyin_norm.split()

            if entry_pinyin_flat == q_norm_flat:
                match_rank = 0
                exact_bonus = -100
            elif entry_pinyin_flat.startswith(q_norm_flat):
                match_rank = 1
                exact_bonus = 0
            elif q_norm_flat in entry_pinyin_flat:
                match_rank = 2
                exact_bonus = 0
            else:
                continue

            if " " in q_norm:
                token_penalty = max(0, len(entry_tokens) - len(q_tokens))
            else:
                token_penalty = 0

            try:
                start_index = entry_tokens.index(q_tokens[0]) if q_tokens else 999
            except ValueError:
                start_index = 999

            hanzi_len = len(hanzi)

            short_word_bonus = 0
            if len(q_tokens) == 1:
                if hanzi_len == 1:
                    short_word_bonus = -2
                elif hanzi_len == 2:
                    short_word_bonus = -1
            else:
                if hanzi_len == 2:
                    short_word_bonus = -1

            common_word_bonus = {
                "你好": -50,
                "您好": -40,
                "好": -30,
                "是": -30,
                "我": -30,
                "你": -30,
                "中国": -35,
                "男人": -35,
                "女人": -35,
                "喜欢": -35,
            }.get(hanzi, 0)

            matched_with_rank.append(
                (
                    match_rank,
                    exact_bonus,
                    common_word_bonus,
                    bad_ru_penalty,
                    token_penalty,
                    start_index,
                    short_word_bonus,
                    hanzi_len,
                    entry,
                )
            )

        matched_with_rank.sort(
            key=lambda item: (
                item[0],
                item[1],
                item[2],
                item[3],
                item[4],
                item[5],
                item[6],
                item[7],
                item[8].id,
            )
        )

        matched = [item[8] for item in matched_with_rank]
        matched = clean_and_deduplicate_entries(matched)

        return matched[:limit]

    # =========================
    # 3) Поиск по русскому
    # =========================
    q_norm = q.strip()
    q_lower = q_norm.lower()
    q_cap = q_lower.capitalize()
    q_title = q_lower.title()

    # exact search оставляем первым
    # Точное совпадение всегда должно быть выше и обычно быстрее
    exact_sql = text("""
        select id
        from entries
        where ru is not null
          and btrim(ru) <> ''
          and btrim(ru) <> '_'
          and (
              btrim(ru) in (:q1, :q2, :q3)
              or lower(btrim(ru)) = :q4
          )
        order by length(ru), id
        limit :limit
    """)

    exact_res = await session.execute(
        exact_sql,
        {
            "q1": q_norm,
            "q2": q_cap,
            "q3": q_title,
            "q4": q_lower,
            "limit": limit,
        },
    )
    exact_ids = [row[0] for row in exact_res.all()]

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

    if len(exact_items) >= limit:
        combined = clean_and_deduplicate_entries(exact_items)
        return combined[:limit]

    # Сначала быстрый prefix search по русскому:
    # lower(ru) like 'слово%'
    # Это намного легче для базы, чем '%слово%'.
    prefix_sql = text("""
        select id
        from entries
        where ru is not null
          and btrim(ru) <> ''
          and btrim(ru) <> '_'
          and lower(ru) like :prefix_lower
          and lower(btrim(ru)) <> :q_lower
        order by
            length(ru),
            id
        limit :prefix_limit
    """)

    prefix_res = await session.execute(
        prefix_sql,
        {
            "q_lower": q_lower,
            "prefix_lower": f"{q_lower}%",
            "prefix_limit": max(60, limit * 3),
        },
    )
    rest_ids = [row[0] for row in prefix_res.all()]

    # Если prefix search дал мало результатов,
    # тогда делаем маленький fallback через contains search:
    # lower(ru) like '%слово%'
    # Но уже с маленьким лимитом, чтобы не грузить базу.
    if len(rest_ids) < limit:
        fallback_sql = text("""
            select id
            from entries
            where ru is not null
              and btrim(ru) <> ''
              and btrim(ru) <> '_'
              and lower(ru) like :contains_lower
              and lower(btrim(ru)) <> :q_lower
              and id not in :existing_ids
            order by
                length(ru),
                id
            limit :fallback_limit
        """).bindparams(
            bindparam("existing_ids", expanding=True)
        )

        fallback_res = await session.execute(
            fallback_sql,
            {
                "q_lower": q_lower,
                "contains_lower": f"%{q_lower}%",
                "existing_ids": rest_ids or [-1],
                "fallback_limit": max(20, limit),
            },
        )

        rest_ids.extend([row[0] for row in fallback_res.all()])

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

    combined = clean_and_deduplicate_entries(exact_items + rest_items)

    # ranking русского оставляем, но чуть чистим
    # Он отвечает за то, какие переводы будут выше
    def ru_rank(entry: Entry) -> tuple[int, int, int, int, int, int, int, int]:
        ru_text = (entry.ru or "").strip().lower()

        ru_exact_word_bonus = 0

        if ru_text == q_lower:
            ru_exact_word_bonus = -50
        elif ru_text.startswith(q_lower + " "):
            ru_exact_word_bonus = -40
        elif ru_text.startswith(q_lower + ","):
            ru_exact_word_bonus = -40
        elif ru_text.startswith(q_lower + ";"):
            ru_exact_word_bonus = -40
        elif ru_text.startswith(q_lower + "."):
            ru_exact_word_bonus = -40

        ru_text = re.sub(r"<.*?>", "", ru_text)
        ru_text = " ".join(ru_text.split())

        penalty = 0

        if ru_text in {"превед", "браво"}:
            penalty += 50

        hanzi_text = (entry.hanzi or "").strip()

        if ru_text == q_lower:
            rank = 0
        elif ru_text.startswith(q_lower):
            rank = 1
        elif q_lower in ru_text:
            rank = 2
        else:
            rank = 3

        if len(ru_text) > 40:
            penalty += 2
        if len(ru_text) > 80:
            penalty += 4
        if len(ru_text) > 140:
            penalty += 6

        if any(char.isdigit() for char in ru_text):
            penalty += 2

        if "(" in ru_text or ")" in ru_text:
            penalty += 1

        if "\n" in ru_text:
            penalty += 2

        if ";" in ru_text:
            penalty += 3

        if "," in ru_text and len(ru_text) > 25:
            penalty += 2

        if any("\u3400" <= ch <= "\u9fff" for ch in ru_text):
            penalty += 1

        if "?" in ru_text or "!" in ru_text:
            penalty += 2

        hanzi_priority = len(hanzi_text)

        common_ru_bonus = 0
        if hanzi_text in {"中国", "男人", "女人", "喜欢"}:
            common_ru_bonus = -20

        examples_bonus = 0
        if getattr(entry, "examples", None):
            examples_bonus = -10

        ru_len_priority = len(ru_text)

        return (
            rank,
            ru_exact_word_bonus,
            common_ru_bonus,
            examples_bonus,
            hanzi_priority,
            penalty,
            ru_len_priority,
            len(entry.pinyin or ""),
        )

    combined.sort(key=ru_rank)

    return combined[:limit]


def split_ru_examples(text: str) -> tuple[str, str]:
    if not text:
        return "", ""

    text = re.sub(r"<.*?>", "", text)
    text = text.replace("\\n", "\n")
    text = re.sub(r"^_+\s*", "", text)
    text = re.sub(r"\s*_\s*", " ", text)
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    text = " ".join(text.split())

    example_pattern = re.compile(r"([\u4e00-\u9fff][^。！？!?]*[。！？!?]?)")

    examples = example_pattern.findall(text)

    translation = example_pattern.sub("", text)
    translation = " ".join(translation.split())

    translation = re.sub(r"\s+", " ", translation).strip()
    examples_text = "\n".join(" ".join(e.split()) for e in examples).strip()

    return translation, examples_text

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