"""Explicit BRuKS provenance; never infer direction from a short Russian gloss."""
from __future__ import annotations

import re
from dictapp.dsl_parser import parse_dsl_article


def normalized_headword(value: str) -> str:
    return " ".join(value.replace("\ufeff", "").replace("\u00a0", " ").split())


def parse_reverse_article(headword: str, body: str) -> dict | None:
    """Called only for a known Russian source, not arbitrary database examples."""
    headword = normalized_headword(headword)
    if not headword or not re.search(r"[А-Яа-яЁё]", headword):
        return None
    parsed = parse_dsl_article(body)
    if parsed is None:
        return None
    return dict(parsed, source={"dictionary": "bruks", "language": "ru", "headword": headword})


def russian_headword(entry) -> str | None:
    """Only accept explicit provenance consistent with the unchanged legacy head."""
    article = getattr(entry, "article", None)
    if not isinstance(article, dict):
        return None
    source = article.get("source")
    if not isinstance(source, dict):
        return None
    if source.get("dictionary") != "bruks" or source.get("language") != "ru":
        return None
    headword = source.get("headword")
    ru = getattr(entry, "ru", None)
    if (not isinstance(headword, str) or not isinstance(ru, str)
            or not headword.strip() or normalized_headword(headword) != normalized_headword(ru)
            or not re.search(r"[А-Яа-яЁё]", headword)):
        return None
    return normalized_headword(headword)
