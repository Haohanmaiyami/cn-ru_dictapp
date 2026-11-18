"""One read-only projection for legacy API strings and structured articles."""
from pydantic import ValidationError

from dictapp.dsl_parser import parse_dsl_article
from dictapp.inline_styles import enrich_article
from dictapp.repo import get_effective_pinyin, split_ru_examples
from dictapp.schemas import DictionaryArticle, EntryOut


def article_for_entry(entry) -> DictionaryArticle | None:
    cached = getattr(entry, "article", None)
    raw = getattr(entry, "raw_dsl", None)
    if cached is not None:
        try:
            return enrich_article(DictionaryArticle.model_validate(cached), raw)
        except ValidationError:
            pass
    if raw:
        parsed = parse_dsl_article(raw)
        if parsed is not None:
            try:
                return enrich_article(DictionaryArticle.model_validate(parsed), raw)
            except ValidationError:
                pass
    # Old rows have no raw markup. Keep their established behavior until a
    # separately reviewed, ID-preserving backfill can recover their sources.
    return None


def present_entry(entry) -> EntryOut:
    article = article_for_entry(entry)
    if article is None:
        translation, examples = split_ru_examples(entry.ru or "")
    elif article.version == 1:
        translation = "\n".join(
            (f"{sense.number}) " if sense.number else "") + sense.definition
            for sense in article.senses
        )
        examples = "\n".join(
            " — ".join(part for part in [example.chinese, example.translation] if part)
            for sense in article.senses for example in sense.examples
        )
    else:
        # Preserve complete paragraphs, source indentation and order within
        # the legacy fields. The v2 client uses blocks to interleave them.
        translation = "\n".join(
            "  " * (block.level - 1) +
            (f"{block.number}) " if block.number else "") + block.text
            for block in article.blocks if block.kind == "definition"
        )
        examples = "\n".join(
            block.text for block in article.blocks if block.kind == "example"
        )
    # Never mutate ORM rows while returning search/detail responses.
    return EntryOut(id=entry.id, hanzi=entry.hanzi, pinyin=get_effective_pinyin(entry),
                    ru=translation, pos=entry.pos, examples=examples, article=article)
