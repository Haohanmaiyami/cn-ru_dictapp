from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from dictapp.db import AsyncSessionMaker
from dictapp.models import Entry


_CJK_RE = re.compile(r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]")
_PINYIN_LIKE_RE = re.compile(
    r"^[A-Za-zÀ-ÖØ-öø-ÿĀ-žǍ-ǐǑ-ǔǕ-ǜǞ-ǟǠ-ǡǢ-ǣǦ-ǧǨ-ǩǪ-ǫǬ-ǭǮ-ǯǰ-ǳǴ-ǵǸ-ǹǺ-ǻǼ-ǽǾ-ǿ\s'·\-]+$"
)
_CYR_RE = re.compile(r"[А-Яа-яЁё]")


def has_cjk(s: str) -> bool:
    return bool(_CJK_RE.search(s or ""))


def has_cyrillic(s: str) -> bool:
    return bool(_CYR_RE.search(s or ""))


def looks_like_pinyin(s: str) -> bool:
    s = (s or "").strip()
    if not s:
        return False
    if has_cjk(s):
        return False
    if has_cyrillic(s):
        return False
    if len(s) > 120:
        return False
    return bool(_PINYIN_LIKE_RE.fullmatch(s))


def normalize_head(s: str) -> str:
    if not s:
        return ""
    s = s.replace("\ufeff", "")
    s = s.replace("\u00A0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()


_TAG_RE = re.compile(r"\[/?[A-Za-z0-9*]+\]")
_BRACE_RE = re.compile(r"\{[^}]*\}")
_INCLUDE_RE = re.compile(r'#INCLUDE\s+"([^"]+)"')


def clean_dsl_text(text_: str) -> str:
    t = (text_ or "").replace("\ufeff", "").replace("\u00A0", " ")
    t = _TAG_RE.sub("", t)
    t = _BRACE_RE.sub("", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def iter_dsl_articles(path: Path, encoding: str = "utf-16") -> Iterator[tuple[str, str]]:
    def walk_file(p: Path) -> Iterator[str]:
        with p.open("r", encoding=encoding, errors="replace") as f:
            for raw in f:
                line = raw.rstrip("\n").rstrip("\r")

                inc_match = _INCLUDE_RE.match(line.strip())
                if inc_match:
                    inc_path = (p.parent / inc_match.group(1)).resolve()
                    if inc_path.exists():
                        yield from walk_file(inc_path)
                    continue

                yield line

    head: Optional[str] = None
    body_lines: list[str] = []

    def flush() -> Optional[tuple[str, str]]:
        nonlocal head, body_lines
        if head is None:
            return None

        body = "\n".join(body_lines).strip()
        result = (head.strip(), body)

        head = None
        body_lines = []
        return result

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

        if st.startswith("#"):
            continue

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


def extract_first_hanzi(text_: str) -> Optional[str]:
    s = text_ or ""
    m = _CJK_RE.search(s)
    if not m:
        return None

    start = m.start()
    end = m.start()

    while start > 0 and has_cjk(s[start - 1]):
        start -= 1
    while end < len(s) and has_cjk(s[end]):
        end += 1

    hanzi = s[start:end].strip()
    return hanzi if hanzi else None


def extract_pinyin_after_hanzi(text_: str) -> Optional[str]:
    for line in (text_ or "").splitlines():
        s = line.strip()
        if not s:
            continue

        m = _CJK_RE.search(s)
        if not m:
            continue

        start = m.start()
        end = m.start()

        while start > 0 and has_cjk(s[start - 1]):
            start -= 1
        while end < len(s) and has_cjk(s[end]):
            end += 1

        tail = s[end:].strip()
        if not tail:
            continue

        tail = re.split(r"[;,]", tail, maxsplit=1)[0].strip()

        if looks_like_pinyin(tail):
            return tail

    return None


def extract_first_pinyin_line(text_: str) -> Optional[str]:
    for line in (text_ or "").splitlines():
        s = line.strip()
        if looks_like_pinyin(s):
            return s
    return None


def split_pinyin_and_translation(text_: str) -> tuple[Optional[str], Optional[str]]:
    lines = [line.strip() for line in (text_ or "").splitlines() if line.strip()]
    if not lines:
        return None, None

    pinyin = None
    if looks_like_pinyin(lines[0]):
        pinyin = lines[0]
        lines = lines[1:]

    ru_text = "\n".join(lines).strip() if lines else None
    return pinyin, ru_text


@dataclass
class ParsedEntry:
    hanzi: str
    pinyin: Optional[str]
    ru_head: str
    ru_full: Optional[str]


def iter_entries_for_file(path: Path, encoding: str = "utf-16") -> Iterator[ParsedEntry]:
    name = path.name.lower()
    is_bruks = "bruks" in name

    for head, body in iter_dsl_articles(path, encoding=encoding):
        head = normalize_head(head)
        body_clean = clean_dsl_text(body)

        if not head:
            continue

        if not is_bruks:
            hanzi = head
            pinyin, ru_text = split_pinyin_and_translation(body_clean)

            if not ru_text:
                continue

            yield ParsedEntry(
                hanzi=hanzi,
                pinyin=pinyin,
                ru_head=ru_text,
                ru_full=ru_text,
            )

        else:
            ru_head = head
            hanzi = extract_first_hanzi(body_clean) or "-"
            pinyin = extract_pinyin_after_hanzi(body_clean) or extract_first_pinyin_line(body_clean)
            ru_full = body_clean if body_clean else None

            yield ParsedEntry(
                hanzi=hanzi,
                pinyin=pinyin,
                ru_head=ru_head,
                ru_full=ru_full,
            )


async def truncate_entries(session: AsyncSession) -> None:
    await session.execute(text("TRUNCATE TABLE public.entries RESTART IDENTITY;"))
    await session.commit()


async def import_file(session: AsyncSession, file_path: Path, batch_size: int = 5000) -> int:
    inserted = 0
    batch: list[Entry] = []

    for item in iter_entries_for_file(file_path):
        hanzi = (item.hanzi or "")[:64]
        pinyin = item.pinyin[:128] if item.pinyin else None
        ru_head = (item.ru_head or "").strip()
        ru_full = item.ru_full.strip() if item.ru_full else None

        if not ru_head:
            continue

        if ru_head.lower() == "москва":
            print("\n=== DEBUG IMPORT MOSCOW ===")
            print("FILE:", file_path.name)
            print("ru_head:", repr(ru_head))
            print("hanzi:", repr(hanzi))
            print("pinyin:", repr(pinyin))
            print("ru_full:", repr(ru_full))
            print("===========================\n")

        e = Entry(
            hanzi=hanzi if hanzi else "-",
            pinyin=pinyin,
            ru=ru_head,
            pos=None,
            examples=ru_full,
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
                print("❌ batch failed near:", bad.hanzi, bad.pinyin, "ru:", bad.ru[:80] if bad.ru else None)
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
            print("❌ final batch failed near:", bad.hanzi, bad.pinyin, "ru:", bad.ru[:80] if bad.ru else None)
            raise

    return inserted


async def main() -> None:
    data_dir = Path("data")
    files: list[Path] = []

    files.extend(sorted(data_dir.glob("dabkrs_*.dsl")))

    bruks = data_dir / "dabruks.dsl"
    if bruks.exists():
        files.append(bruks)

    if not files:
        raise SystemExit("No DSL files found in ./data")

    async with AsyncSessionMaker() as session:
        await truncate_entries(session)

        total = 0
        for fp in files:
            print(f"\n== importing {fp.name} ==")
            count = await import_file(session, fp)
            total += count
            print(f"🎉 finished {fp.name}. inserted: {count}")

        print(f"\n🎉 DONE. Total inserted: {total}")


if __name__ == "__main__":
    asyncio.run(main())