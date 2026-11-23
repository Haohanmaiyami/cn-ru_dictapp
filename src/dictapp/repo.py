import json
import re
from functools import lru_cache
from sqlalchemy import select, case, func, and_, or_, cast, String, literal_column
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from dictapp.models import Entry
from dictapp.reverse_article import russian_headword
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

# Editorial preference for the standalone country name, verified in BKRS.
# Only reorders matching dictionary records; never fabricates a translation.
_PREFERRED_RUSSIAN_HEADWORDS = {"россия": "俄罗斯"}


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


def clean_and_deduplicate_entries(entries: list[Entry], *, preserve_russian_articles: bool = False) -> list[Entry]:
    """Chinese homographs share a card; Russian search can preserve source bodies.

    BRuKS stores its first Chinese translation in ``hanzi`` for compatibility.
    That translation is not the identity of the original Russian article.
    """
    unique: dict[tuple, Entry] = {}
    for entry in entries:
        headword = russian_headword(entry)
        hanzi = (entry.hanzi or "").strip()
        if headword and preserve_russian_articles:
            # Reimports may duplicate the same article under another ID. Keep
            # different source bodies, even when headword/first hanzi coincide.
            body = entry.raw_dsl or json.dumps(entry.article, sort_keys=True, ensure_ascii=False)
            key = ("ru", headword, body)
        elif hanzi and hanzi not in {"-", "—", "_"}:
            key = ("zh", hanzi)
        else:
            continue
        current = unique.get(key)
        current_rank = (bool(russian_headword(current)), -_entry_quality_score(current)) if current else None
        entry_rank = (bool(headword), -_entry_quality_score(entry))
        if current is None or entry_rank < current_rank:
            unique[key] = entry
    return list(unique.values())


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

def _normalized_headword_sql(value):
    # Match reverse_article.normalized_headword without changing the prefix index.
    value = func.replace(func.replace(value, "\ufeff", ""), "\u00a0", " ")
    return func.btrim(func.regexp_replace(value, r"\s+", " ", "g"))


def _russian_source_sql():
    source = Entry.article["source"]
    headword = source["headword"].as_string()
    return and_(
        source["dictionary"].as_string() == "bruks",
        source["language"].as_string() == "ru",
        func.json_typeof(source["headword"]) == "string",
        headword.op("~")("[А-Яа-яЁё]"),
        _normalized_headword_sql(headword) == _normalized_headword_sql(Entry.ru),
    )


def _quality_sql():
    """Prefer complete copies of one article, never penalize a rich definition."""
    return (
        case((func.btrim(Entry.ru) != "", 10), else_=0)
        + case((func.coalesce(Entry.examples, "") != "", 30), else_=0)
        + case((Entry.ru.op("~")("[㐀-鿿]"), 20), else_=0)
        + case((func.length(Entry.ru) > 80, 10), else_=0)
        + case((Entry.ru.op("~")("[;；]"), 5), else_=0)
    )


async def _ranked_entries(session: AsyncSession, condition, ordering, limit: int,
                          *, preserve_russian_articles: bool = False) -> list[Entry]:
    """Rank/deduplicate matched rows before LIMIT, then fetch only winning bodies.

    There is no arbitrary first-N-ID candidate cutoff. In particular, a pinyin
    match after the former 50,000-row cutoff remains eligible, without loading
    those unrelated article bodies into Python.
    """
    source = _russian_source_sql()
    identity = func.btrim(Entry.hanzi)
    valid = and_(func.btrim(Entry.hanzi) != "", func.btrim(Entry.hanzi).not_in(["-", "—", "_"]))
    if preserve_russian_articles:
        body = func.coalesce(func.nullif(Entry.raw_dsl, ""), cast(cast(Entry.article, JSONB), String))
        identity = [
            case((source, _normalized_headword_sql(Entry.ru)), else_=None),
            case((source, body), else_=None),
            case((source, None), else_=func.btrim(Entry.hanzi)),
        ]
        valid = or_(source, valid)
    # Labels let the outer query reuse exactly the same ordering as the window.
    ranks = [expression.label(f"search_rank_{i}") for i, expression in enumerate(ordering)]
    ranked = (
        select(
            Entry.id.label("entry_id"), *ranks,
            func.row_number().over(
                partition_by=identity, order_by=(*ordering, Entry.id),
            ).label("duplicate_rank"),
        )
        .where(condition, valid)
        .subquery()
    )
    winners = (
        select(ranked.c.entry_id)
        .where(ranked.c.duplicate_rank == 1)
        .order_by(*(ranked.c[f"search_rank_{i}"] for i in range(len(ranks))), ranked.c.entry_id)
        .limit(limit)
    )
    result = await session.execute(winners)
    ids = list(result.scalars().all())
    if not ids:
        return []
    result = await session.execute(
        select(Entry).where(Entry.id.in_(ids))
        .order_by(case({entry_id: position for position, entry_id in enumerate(ids)}, value=Entry.id))
    )
    return list(result.scalars().all())


def russian_search_rank(entry: Entry, query: str) -> tuple:
    """Semantic tiers shared by the SQL query and focused ranking regression tests."""
    query = normalize_query(query).lower()
    headword = russian_headword(entry)
    value = (headword or entry.ru or "").strip().lower()
    boundary_prefix = bool(re.match(re.escape(query) + r"(?:[^\w]|$)", value))
    if value == query:
        tier = 0 if headword else 3
    elif value.startswith(query):
        tier = (1 if boundary_prefix else 2) if headword else (4 if boundary_prefix else 5)
    else:
        tier = 6 if headword else 7
    preferred = _PREFERRED_RUSSIAN_HEADWORDS.get(query)
    preference = int(bool(preferred) and (entry.hanzi or "").strip() != preferred)
    return (preference, tier, len(value) if headword else 0, -_entry_quality_score(entry),
            len((entry.hanzi or "").strip()), entry.id or 0)


@lru_cache(maxsize=26)
def _pinyin_initial_variants(initial: str) -> tuple[str, ...]:
    """Latin case/tone forms of one initial, including syllabic n/m tones.

    Keep the established first-letter candidate scope; do not normalize every
    pinyin across the entire dictionary merely to find one initial's matches.
    """
    latin = (*range(0x0250), *range(0x1E00, 0x1F00))
    return tuple(chr(code) for code in latin if normalize_pinyin(chr(code)) == initial)


async def search_entries(session: AsyncSession, q: str, limit: int = 30) -> list[Entry]:
    q = normalize_query(q)
    if not q or limit <= 0:
        return []
    q_like = _escape_like(q)
    quality = -_quality_sql()
    chinese_source_first = case((_russian_source_sql(), 1), else_=0)

    if _has_cjk(q):
        # Exact lookup uses the existing hanzi index. Keep prefix suggestions,
        # but do not run an unbounded contains scan merely to fill the page.
        exact = await _ranked_entries(
            session, Entry.hanzi == q, [chinese_source_first, quality, Entry.id], limit,
        )
        if len(exact) >= limit:
            return exact
        # Case folding is unnecessary for a purely Chinese query.
        prefix = Entry.hanzi.like(f"{q_like}%", escape="\\") if not _LAT_RE.search(q) else Entry.hanzi.ilike(f"{q_like}%", escape="\\")
        additional = await _ranked_entries(
            session, and_(Entry.hanzi != q, prefix),
            [func.length(Entry.hanzi), chinese_source_first, quality, Entry.id], limit - len(exact),
        )
        if exact or additional:
            return exact + additional
        if len(q) < 2:
            return []
        contains = Entry.hanzi.like(f"%{q_like}%", escape="\\") if not _LAT_RE.search(q) else Entry.hanzi.ilike(f"%{q_like}%", escape="\\")
        return await _ranked_entries(
            session, contains, [func.length(Entry.hanzi), chinese_source_first, quality, Entry.id], limit,
        )

    if _looks_like_pinyin(q):
        flat_query = normalize_pinyin(q).replace(" ", "")
        if not flat_query:
            return []
        # PostgreSQL 15 is pinned in compose. NFD plus [^a-z] matches the
        # existing Python normalization (tone marks, spacing and punctuation).
        initial_match = func.left(Entry.pinyin, 1).in_(_pinyin_initial_variants(flat_query[0]))
        normalized = func.regexp_replace(
            func.normalize(func.lower(Entry.pinyin), literal_column("NFD")), "[^a-z]", "", "g",
        )
        # CASE makes the CPU guard explicit even if PostgreSQL reorders WHERE
        # predicates. Only this initial's rows undergo NFD/regexp normalization.
        flat = case((initial_match, normalized), else_=None)
        pattern = _escape_like(flat_query)
        ordering = [case((flat == flat_query, 0), else_=1),
                    func.length(flat), func.length(Entry.hanzi), chinese_source_first, quality, Entry.id]
        direct = await _ranked_entries(
            session, and_(initial_match, flat.like(f"{pattern}%", escape="\\")), ordering, limit,
        )
        if direct:
            return direct
        return await _ranked_entries(
            session, and_(initial_match, flat.like(f"%{pattern}%", escape="\\")),
            [func.strpos(flat, flat_query), func.length(flat), func.length(Entry.hanzi), chinese_source_first, quality, Entry.id], limit,
        )

    lower = func.lower(Entry.ru)
    q_lower = q.lower()
    source = _russian_source_sql()
    boundary_prefix = lower.op("~")("^" + re.escape(q_lower) + r"([^[:alnum:]А-Яа-яЁё_]|$)")
    tier = case(
        (and_(source, lower == q_lower), 0),
        (and_(source, boundary_prefix), 1),
        (source, 2),
        (lower == q_lower, 3),
        (boundary_prefix, 4), else_=5,
    )
    ordering = [tier, case((source, func.length(Entry.ru)), else_=0),
                quality, func.length(Entry.hanzi), Entry.id]
    preferred = _PREFERRED_RUSSIAN_HEADWORDS.get(q_lower)
    preference = [case((func.btrim(Entry.hanzi) == preferred, 0), else_=1)] if preferred else []
    ordering = preference + ordering
    valid_ru = and_(Entry.ru.is_not(None), func.btrim(Entry.ru) != "", func.btrim(Entry.ru) != "_")
    # This expression and predicate match ix_entries_ru_prefix80. Queries longer
    # than 80 characters use an indexed 80-character guard plus the full match.
    direct = await _ranked_entries(
        session,
        and_(valid_ru,
             func.left(lower, 80).like(_escape_like(q_lower[:80]) + "%", escape="\\"),
             lower.like(_escape_like(q_lower) + "%", escape="\\")),
        ordering, limit, preserve_russian_articles=True,
    )
    if direct:
        return direct
    # Whole-word fallback avoids e.g. «кот» matching «который». A short query
    # with no direct matches should not trigger a global translation scan.
    if sum(char.isalpha() for char in q_lower) < 3:
        return []
    word_pattern = r"(^|[^[:alnum:]А-Яа-яЁё_])" + re.escape(q_lower) + r"([^[:alnum:]А-Яа-яЁё_]|$)"
    return await _ranked_entries(
        session, and_(valid_ru, lower.op("~")(word_pattern)),
        preference + [case((source, 6), else_=7), quality, func.length(Entry.hanzi), Entry.id], limit,
        preserve_russian_articles=True,
    )


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
