"""Read-only inline DSL styling, aligned positionally with established article rows.

The semantic parser remains authoritative and unchanged. Styling is emitted only
when the raw DSL still produces the complete cached article and every generated
row is byte-for-byte the same text the client already displays. No substring
matching, guessed word occurrences, database updates, or HTML rendering is used.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import re
from pydantic import ValidationError

from dictapp.dsl_parser import _INLINE, _NAME, _PARAGRAPHS, _Unsupported, _parse_v2, parse_dsl_article
from dictapp.schemas import ArticleFormatting, DictionaryArticle


_MAX_SOURCE_CHARS = 65_536
_STYLE_TAG = re.compile(r"\[(?:i|b|u|p|c)(?:\s[^\]]*)?\]", re.IGNORECASE)
_COLORS = {"green": "green", "blue": "blue", "red": "red", "gray": "gray", "grey": "gray",
           "black": "primary", "brown": "brown", "violet": "violet", "crimson": "crimson"}


@dataclass(frozen=True)
class _Style:
    italic: bool = False
    bold: bool = False
    underline: bool = False
    color: str | None = None


_PLAIN = _Style()


@dataclass
class _Span:
    text: str
    style: _Style


@dataclass
class _Container:
    tag: str
    children: list[_Span | _Container] = field(default_factory=list)


def _active_style(styles: list[tuple[str, str, int]]) -> _Style:
    tags = {tag for tag, _, _ in styles}
    color = None
    for tag, attrs, _ in styles:
        if tag == "p":
            color = "green"
        elif tag == "c":
            # An unknown source color affects styling only, never body content.
            color = _COLORS.get(attrs.lower()) if attrs else "green"
    return _Style(italic=bool(tags & {"i", "p"}), bold="b" in tags,
                  underline="u" in tags, color=color)


def _styled_tree(body: str) -> _Container:
    """Keep structural nesting, and retain styles including valid crossed tags."""
    root = _Container("root")
    stack = [root]
    styles: list[tuple[str, str, int]] = []
    text: list[str] = []

    def flush() -> None:
        if text:
            stack[-1].children.append(_Span("".join(text), _active_style(styles)))
            text.clear()

    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body) and body[i + 1] in "[]{}\\":
            text.append(body[i + 1])
            i += 2
            continue
        if ch != "[":
            text.append(ch)
            i += 1
            continue
        end = body.find("]", i + 1)
        if end == -1:
            raise _Unsupported("Unclosed style bracket")
        match = _NAME.fullmatch(body[i + 1:end])
        if not match:
            text.append(body[i:end + 1])
            i = end + 1
            continue
        closing, tag, attrs = match.groups()
        tag, attrs = tag.lower(), (attrs or "").strip()
        flush()
        if tag in _INLINE:
            if closing:
                indices = [n for n, (name, _, _) in enumerate(styles) if name == tag]
                if not indices or styles[indices[-1]][2] != len(stack):
                    raise _Unsupported("Style crosses structural boundary")
                styles.pop(indices[-1])
            else:
                styles.append((tag, attrs, len(stack)))
        elif tag in _PARAGRAPHS | {"m", "ex", "ref"}:
            if closing:
                expected = "m" if stack[-1].tag in _PARAGRAPHS else stack[-1].tag
                if len(stack) == 1 or tag != expected or any(depth == len(stack) for _, _, depth in styles):
                    raise _Unsupported("Mismatched structural tag")
                stack.pop()
            else:
                node = _Container(tag)
                stack[-1].children.append(node)
                stack.append(node)
        else:
            raise _Unsupported("Unsupported styling tag")
        if len(styles) + len(stack) >= 64:
            raise _Unsupported("Excessive styling depth")
        i = end + 1
    flush()
    if len(stack) != 1 or styles:
        raise _Unsupported("Unclosed styled article")
    return root


def _atoms(node: _Span | _Container) -> list[tuple[str, _Style]]:
    if isinstance(node, _Span):
        return [(char, node.style) for char in node.text]
    return [atom for child in node.children for atom in _atoms(child)]


def _clean(atoms: list[tuple[str, _Style]]) -> list[tuple[str, _Style]]:
    result: list[tuple[str, _Style]] = []
    space = None
    for char, style in atoms:
        if char == "\ufeff":
            continue
        if char.isspace():
            if result and space is None:
                space = (" ", style)
            continue
        if space is not None:
            result.append(space)
            space = None
        result.append((char, style))
    return result


def _text(atoms: list[tuple[str, _Style]]) -> str:
    return "".join(char for char, _ in atoms)


def _definition(atoms: list[tuple[str, _Style]], version: int) -> list[tuple[str, _Style]]:
    atoms = _clean(atoms)
    pattern = r"^(\d+)[).]\s*" if version == 1 else r"^([0-9]+)(?:\)\s*|\.(?:\s+|$))"
    number = re.match(pattern, _text(atoms))
    return atoms[number.end():] if number else atoms


def _examples(node: _Container) -> list[list[tuple[str, _Style]]]:
    if node.tag == "ex":
        return [_clean(_atoms(node))]
    return [example for child in node.children if isinstance(child, _Container)
            for example in _examples(child)]


def _v1_rows(tree: _Container, article: dict) -> list[list[tuple[str, _Style]]]:
    senses: list[tuple[list[tuple[str, _Style]], list[list[tuple[str, _Style]]]]] = []
    for child in tree.children:
        if isinstance(child, _Container) and child.tag == "m1":
            definition: list[tuple[str, _Style]] = []
            examples = []
            for part in child.children:
                if isinstance(part, _Container) and part.tag in {"m2", "ex"}:
                    definition.append((" ", _PLAIN))
                    examples.extend(_examples(part))
                else:
                    definition.extend(_atoms(part))
            senses.append((_definition(definition, 1), examples))
        elif isinstance(child, _Container) and child.tag in {"m2", "ex"}:
            if not senses:
                raise _Unsupported("Example before a definition")
            senses[-1][1].extend(_examples(child))
    if len(senses) != len(article["senses"]):
        raise _Unsupported("Styled sense count mismatch")
    rows = []
    for (definition, examples), expected in zip(senses, article["senses"]):
        if _text(definition) != expected["definition"] or len(examples) != len(expected["examples"]):
            raise _Unsupported("Styled sense text mismatch")
        rows.append(definition)
        for source, example in zip(examples, expected["examples"]):
            chinese, translation = example["chinese"], example["translation"]
            if _text(source) != chinese + " " + translation:
                raise _Unsupported("Styled example text mismatch")
            # The legacy v1 view adds this separator; it has no source style.
            rows.append(source[:len(chinese)] + [(ch, _PLAIN) for ch in " — "]
                        + source[len(chinese) + 1:])
    return rows


def _v2_paragraph(node: _Container) -> list[list[tuple[str, _Style]]]:
    rows = []
    pending: list[tuple[str, _Style]] = []

    def flush() -> None:
        if _text(pending).strip():
            rows.append(_definition(pending, 2))
        pending.clear()

    for child in node.children:
        if isinstance(child, _Span) or child.tag == "ref":
            pending.extend(_atoms(child))
        elif child.tag == "ex":
            flush()
            rows.append(_clean(_atoms(child)))
        elif child.tag in _PARAGRAPHS:
            flush()
            rows.extend(_v2_paragraph(child))
        else:
            raise _Unsupported("Unexpected styled structure")
    flush()
    return rows


def _runs(atoms: list[tuple[str, _Style]]) -> tuple[tuple[str, _Style], ...]:
    runs: list[tuple[list[str], _Style]] = []
    for char, style in atoms:
        if not runs or runs[-1][1] != style:
            runs.append(([char], style))
        else:
            runs[-1][0].append(char)
    return tuple(("".join(text), style) for text, style in runs)


def _aligned_rows(body: str, parsed: dict):
    tree = _styled_tree(body)
    if parsed["version"] == 1:
        rows = _v1_rows(tree, parsed)
    else:
        rows = [row for child in tree.children
                if isinstance(child, _Container) and child.tag in _PARAGRAPHS
                for row in _v2_paragraph(child)]
    expected = DictionaryArticle.model_validate(parsed).row_texts()
    if [_text(row) for row in rows] != expected:
        raise _Unsupported("Styled article row mismatch")
    return rows


@lru_cache(maxsize=128)
def _source_formatting(body: str):
    parsed = parse_dsl_article(body)
    if parsed is None:
        return None
    try:
        rows = _aligned_rows(body, parsed)
    except (_Unsupported, ValidationError):
        if parsed["version"] != 2:
            return None
        # The recovery path may wrap loose prose and rebalance paired styles.
        # Use its exact canonical source only after checking the full semantic
        # article, including levels, order and reference targets, is unchanged.
        from dictapp.dsl_recovery import recovery_source
        canonical = recovery_source(body)
        if canonical is None or _parse_v2(canonical) != parsed:
            return None
        try:
            rows = _aligned_rows(canonical, parsed)
        except (_Unsupported, ValidationError):
            return None
    if not any(style != _PLAIN for row in rows for _, style in row):
        return None
    return parsed, tuple(_runs(row) for row in rows)


def enrich_article(article: DictionaryArticle, raw: str | None) -> DictionaryArticle:
    if not isinstance(raw, str) or len(raw) > _MAX_SOURCE_CHARS or not _STYLE_TAG.search(raw):
        return article
    source = _source_formatting(raw)
    if source is None:
        return article
    parsed, rows = source
    if article.model_dump(exclude={"formatting"}) != parsed:
        # An edited/cache-mismatched article keeps its current text and no new styles.
        return article
    formatting = ArticleFormatting.model_validate({"version": 1, "rows": [
        [{"text": text, "italic": style.italic, "bold": style.bold,
          "underline": style.underline, "color": style.color} for text, style in row]
        for row in rows
    ]})
    return article.model_copy(update={"formatting": formatting})
