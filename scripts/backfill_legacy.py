"""Exact legacy BKRS importer projection, without importing app settings or DB.

Keep these rules compatible with scripts/import_dsl.py. Matching all five stored
fields is intentional: a same-looking headword is not sufficient provenance.
"""
from __future__ import annotations

import re

_TAG_RE = re.compile(r"\[/?[A-Za-z0-9*]+\]")
_BRACE_RE = re.compile(r"\{[^}]*\}")
_REF_RE = re.compile(r"\[ref\](.*?)\[/ref\]", re.IGNORECASE | re.DOTALL)
_CJK_RE = re.compile(r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]")
_CYR_RE = re.compile(r"[А-Яа-яЁё]")
_PINYIN_LIKE_RE = re.compile(
    r"^[A-Za-zÀ-ÖØ-öø-ÿĀ-žǍ-ǐǑ-ǔǕ-ǜǞ-ǟǠ-ǡǢ-ǣǦ-ǧǨ-ǩǪ-ǫǬ-ǭǮ-ǯǰ-ǳǴ-ǵǸ-ǹǺ-ǻǼ-ǽǾ-ǿ\s'·\-]+$"
)


def clean_dsl_text(body: str) -> str:
    value = body.replace("\ufeff", "").replace("\u00A0", " ")
    value = _BRACE_RE.sub("", _TAG_RE.sub("", value))
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", value)).strip()


def dsl_to_html(body: str) -> str:
    value = body.replace("\ufeff", "").replace("\u00A0", " ")

    def reference(match):
        label = match.group(1).strip()
        return f'<a href="/search?q={label}">{label}</a>' if label else ""

    value = _REF_RE.sub(reference, value)
    value = _BRACE_RE.sub("", _TAG_RE.sub("", value))
    value = re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", value)).strip()
    return value.replace("\n", "<br>")


def project(head: str, raw: str) -> dict | None:
    head = re.sub(r"[ \t]+", " ", head.replace("\ufeff", "").replace("\u00A0", " ")).strip()
    lines = [line.strip() for line in clean_dsl_text(raw).splitlines() if line.strip()]
    pinyin = None
    if lines:
        first = lines[0]
        if (len(first) <= 120 and not _CJK_RE.search(first) and not _CYR_RE.search(first)
                and _PINYIN_LIKE_RE.fullmatch(first)):
            pinyin = lines.pop(0)
    ru = "\n".join(lines).strip()
    hanzi = head[:64]
    if not hanzi or hanzi.strip() == "-" or not ru:
        return None
    return {"hanzi": hanzi, "pinyin": pinyin[:128] if pinyin else None,
            "ru": ru, "pos": None, "examples": dsl_to_html(raw) if raw else ru}
