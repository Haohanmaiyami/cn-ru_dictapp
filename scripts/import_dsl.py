from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# твои импорты под проект
from dictapp.db import AsyncSessionMaker
from dictapp.models import Entry


# =========================
# Helpers: detect line types
# =========================

# CJK Unified + ExtA + Compatibility
_CJK_RE = re.compile(r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]")

_PINYIN_LIKE_RE = re.compile(
    r"^[A-Za-zÀ-ÖØ-öø-ÿĀ-žǍ-ǐǑ-ǔǕ-ǜǞ-ǟǠ-ǡǢ-ǣǦ-ǧǨ-ǩǪ-ǫǬ-ǭǮ-ǯǰ-ǳǴ-ǵǸ-ǹǺ-ǻǼ-ǽǾ-ǿ\s'·-]+$"
)

def has_cjk(s: str) -> bool:
    return bool(_CJK_RE.search(s))


def looks_like_pinyin(s: str) -> bool:
    s = s.strip()
    if not s:
        return False
    if has_cjk(s):
        return False
    # часто мусор типа "—"
    if len(s) > 120:
        return False
    # если есть кириллица — это точно не пиньинь
    if re.search(r"[А-Яа-яЁё]", s):
        return False
    return bool(_PINYIN_LIKE_RE.match(s))


# =========================
# DSL cleanup
# =========================

_TAG_RE = re.compile(r"\[/?[A-Za-z0-9]+\]")      # [m1], [/m], [i], [/i], [p], [/p], [ref]...
_BRACE_RE = re.compile(r"\{[^}]*\}")            # {....}
_INCLUDE_RE = re.compile(r'#INCLUDE\s+"([^"]+)"')

def clean_dsl_text(text_: str) -> str:
    """
    Делает текст читабельным:
    - убирает DSL-теги [m1], [i], [/i], [p], [ref]...
    - убирает фигурные {...}
    - нормализует пробелы/пустые строки
    """
    t = text_.replace("\u00A0", " ")
    t = _TAG_RE.sub("", t)
    t = _BRACE_RE.sub("", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


# =========================
# Universal DSL reader (headword + body) with #INCLUDE
# =========================

def iter_dsl_articles(path: Path, encoding: str = "utf-16") -> Iterator[tuple[str, str]]:
    """
    Читает DSL как пары (headword, body_text).
    Поддерживает #INCLUDE "file.dsl".

    Формат статьи DSL:
      headword (строка без ведущих пробелов/табов)
      body (строки с ведущим пробелом/табом)
    """
    def walk_file(p: Path) -> Iterator[str]:
        with p.open("r", encoding=encoding, errors="replace") as f:
            for raw in f:
                line = raw.rstrip("\n").rstrip("\r")
                m = _INCLUDE_RE.match(line.strip())
                if m:
                    inc = (p.parent / m.group(1)).resolve()
                    if inc.exists():
                        yield from walk_file(inc)
                    continue
                yield line

    head: Optional[str] = None
    body_lines: list[str] = []

    def flush() -> Optional[tuple[str, str]]:
        nonlocal head, body_lines
        if head is None:
            return None
        body = "\n".join(body_lines).strip()
        out = (head.strip(), body)
        head = None
        body_lines = []
        return out

    for line in walk_file(path):
        if not line:
            if head is not None:
                body_lines.append("")
            continue

        st = line.strip()
        if not st:
            if head is not None:
                body_lines.append("")
            continue

        # директивы
        if st.startswith("#"):
            continue

        # новая статья — строка без ведущих пробелов/табов
        if line[0] not in (" ", "\t"):
            prev = flush()
            if prev:
                yield prev
            head = st
        else:
            if head is None:
                continue
            body_lines.append(st)

    prev = flush()
    if prev:
        yield prev


# =========================
# Extractors for BRUKS
# =========================

def extract_first_hanzi(text_: str) -> Optional[str]:
    m = _CJK_RE.search(text_)
    if not m:
        return None

    start = m.start()
    end = m.start()

    while start > 0 and has_cjk(text_[start - 1]):
        start -= 1
    while end < len(text_) and has_cjk(text_[end]):
        end += 1

    hanzi = text_[start:end].strip()
    return hanzi if hanzi else None


def extract_first_pinyin(text_: str) -> Optional[str]:
    for line in text_.splitlines():
        s = line.strip()
        if looks_like_pinyin(s):
            return s
    return None


# =========================
# Parsed entry
# =========================

@dataclass
class ParsedEntry:
    hanzi: str
    pinyin: Optional[str]
    ru: str


def iter_entries_for_file(path: Path, encoding: str = "utf-16") -> Iterator[ParsedEntry]:
    """
    dabkrs_*.dsl (CN->RU):
      headword = hanzi
      pinyin = первая пиньинь-подобная строка в body
      ru = body

    dabruks.dsl (RU->CN):
      headword = ru
      hanzi/pinyin пробуем вытащить из body
      ru кладём как headword (чтобы русские простые слова реально искались)
    """
    name = path.name.lower()
    is_bruks = "bruks" in name

    for head, body in iter_dsl_articles(path, encoding=encoding):
        body_clean = clean_dsl_text(body)

        if not is_bruks:
            hanzi = head.strip()
            if not hanzi:
                continue
            pinyin = extract_first_pinyin(body_clean)
            ru = body_clean
            if ru:
                yield ParsedEntry(hanzi=hanzi, pinyin=pinyin, ru=ru)
        else:
            ru_head = head.strip()
            if not ru_head:
                continue
            hanzi = extract_first_hanzi(body_clean) or "-"
            pinyin = extract_first_pinyin(body_clean)
            # ВАЖНО: ru = русское headword (чтобы "отлично", "хорошо", "бу" и т.п. точно находились)
            yield ParsedEntry(hanzi=hanzi, pinyin=pinyin, ru=ru_head)


# =========================
# DB import
# =========================

async def truncate_entries(session: AsyncSession) -> None:
    await session.execute(text("TRUNCATE TABLE public.entries RESTART IDENTITY;"))
    await session.commit()


async def import_file(session: AsyncSession, file_path: Path, batch_size: int = 5000) -> int:
    inserted = 0
    batch: list[Entry] = []

    for item in iter_entries_for_file(file_path):
        hanzi = (item.hanzi or "")[:64]
        pinyin = (item.pinyin[:128] if item.pinyin else None)

        e = Entry(
            hanzi=hanzi if hanzi else "-",
            pinyin=pinyin,
            ru=item.ru,
            pos=None,
            examples=None,
        )
        batch.append(e)

        if len(batch) >= batch_size:
            session.add_all(batch)
            try:
                await session.commit()
                inserted += len(batch)
                print(f"✅ inserted: {inserted}")
            except Exception:
                await session.rollback()
                bad = batch[-1]
                print("❌ batch failed near:", bad.hanzi, bad.pinyin, "ru_head:", (bad.ru[:80] if bad.ru else None))
                raise
            finally:
                batch.clear()

    if batch:
        session.add_all(batch)
        try:
            await session.commit()
            inserted += len(batch)
        except Exception:
            await session.rollback()
            bad = batch[-1]
            print("❌ final batch failed near:", bad.hanzi, bad.pinyin, "ru_head:", (bad.ru[:80] if bad.ru else None))
            raise

    return inserted


async def main() -> None:
    data_dir = Path("data")
    files: list[Path] = []

    # dabkrs_1..3
    files.extend(sorted(data_dir.glob("dabkrs_*.dsl")))

    # bruks
    bruks = data_dir / "dabruks.dsl"
    if bruks.exists():
        files.append(bruks)

    if not files:
        raise SystemExit("No DSL files found in ./data (expected dabkrs_*.dsl and/or dabruks.dsl)")

    async with AsyncSessionMaker() as session:
        # если хочешь каждый раз с нуля — раскомментируй:
        # await truncate_entries(session)

        total = 0
        for fp in files:
            print(f"\n== importing {fp.name} ==")
            count = await import_file(session, fp)
            total += count
            print(f"🎉 finished {fp.name}. inserted: {count}")

        print(f"\n🎉 DONE. Total inserted: {total}")


if __name__ == "__main__":
    asyncio.run(main())