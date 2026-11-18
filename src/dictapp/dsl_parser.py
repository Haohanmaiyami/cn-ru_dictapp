"""Conservative, offline parser for the BKRS article layout used by this app.

The original DSL remains the source of truth. Existing simple articles retain
version 1. More complex layouts use version 2 ordered blocks with explicit
source levels, preserving ambiguous examples as complete text. Unsupported
layouts return None instead of discarding content. No DB or settings imports.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
import unicodedata


class _Unsupported(ValueError):
    pass


@dataclass
class _Node:
    tag: str
    attrs: str = ""
    children: list[str | "_Node"] = field(default_factory=list)


# These styles change presentation, not the underlying text. Media, language
# switches and arbitrary tags deliberately fall back until explicitly supported.
_INLINE = {"b", "i", "u", "p", "c", "sup", "sub", "trn", "com", "t", "*"}
_NAME = re.compile(r"(/?)([A-Za-z][A-Za-z0-9]*|\*)(?:\s+(.*))?", re.DOTALL)
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U000323af]")
_CYR = re.compile(r"[А-Яа-яЁё]")
_NUMBER = re.compile(r"^(\d+)[).]\s*")


def _tree(body: str) -> _Node:
    root = _Node("root")
    stack = [root]
    text: list[str] = []

    def flush() -> None:
        if text:
            stack[-1].children.append("".join(text))
            text.clear()

    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body) and body[i + 1] in "[]{}\\":
            text.append(body[i + 1])
            i += 2
            continue
        if ch in "{}":
            raise _Unsupported("Unescaped DSL hidden-text syntax")
        if ch != "[":
            text.append(ch)
            i += 1
            continue
        end = body.find("]", i + 1)
        if end == -1:
            raise _Unsupported("Unclosed bracket")
        raw = body[i + 1:end]
        match = _NAME.fullmatch(raw)
        if not match:
            # Ordinary bracketed prose is text; escaped forms are handled above.
            text.append(body[i:end + 1])
            i = end + 1
            continue
        closing, tag, attrs = match.groups()
        tag = tag.lower()
        attrs = (attrs or "").strip()
        if tag not in _INLINE | {"m", "m1", "m2", "ex", "ref"}:
            raise _Unsupported(f"Unsupported DSL tag: {tag}")
        flush()
        if closing:
            expected = "m" if stack[-1].tag in {"m1", "m2"} else stack[-1].tag
            if len(stack) == 1 or tag != expected or attrs:
                raise _Unsupported("Mismatched DSL closing tag")
            stack.pop()
        else:
            if tag == "m":
                raise _Unsupported("Paragraph without explicit sense/example level")
            if attrs and tag not in {"c", "ref"}:
                raise _Unsupported("Unsupported tag attributes")
            if len(stack) >= 64:
                raise _Unsupported("Excessively nested DSL")
            node = _Node(tag, attrs)
            stack[-1].children.append(node)
            stack.append(node)
        i = end + 1
    flush()
    if len(stack) != 1:
        raise _Unsupported("Unclosed DSL tag")
    return root


def _plain(node: str | _Node) -> str:
    if isinstance(node, str):
        return node
    return "".join(_plain(child) for child in node.children)


def _clean(text: str) -> str:
    return " ".join(text.replace("\ufeff", "").replace("\u00a0", " ").split())


def _is_pinyin(text: str) -> bool:
    if not text or len(text) > 120 or _CJK.search(text) or _CYR.search(text):
        return False
    return all(ch.isspace() or ch in "'’·-12345" or
               (ch.isalpha() and "LATIN" in unicodedata.name(ch, "")) or
               unicodedata.combining(ch) for ch in text)


def _reference(node: _Node) -> dict:
    # A link cannot itself contain another structural node/link.
    if any(isinstance(child, _Node) and child.tag not in _INLINE for child in node.children):
        raise _Unsupported("Nested or structural link")
    nested_references: list[dict] = []
    label = _clean("".join(_inline(child, nested_references) for child in node.children))
    if nested_references:
        raise _Unsupported("Nested reference")
    target = label
    if node.attrs:
        match = re.fullmatch(r'target\s*=\s*"([^"\r\n]+)"', node.attrs)
        if not match:
            raise _Unsupported("Unsupported cross-dictionary/reference attributes")
        target = _clean(match.group(1))
    if not label or not target:
        raise _Unsupported("Empty reference")
    return {"target": target, "label": label}


def _inline(node: str | _Node, references: list[dict]) -> str:
    if isinstance(node, str):
        return node
    if node.tag == "ref":
        reference = _reference(node)
        if reference not in references:
            references.append(reference)
        return reference["label"]
    if node.tag not in _INLINE:
        raise _Unsupported("Unexpected structural tag in inline text")
    return "".join(_inline(child, references) for child in node.children)


def _example(node: _Node) -> dict:
    references: list[dict] = []
    value = _clean("".join(_inline(child, references) for child in node.children))
    if references:
        # The version-1 example contract cannot represent linked spans yet.
        raise _Unsupported("Reference inside an example")
    cyr = _CYR.search(value)
    if not cyr:
        raise _Unsupported("Example has no unambiguous Russian translation")
    # Split only inside an explicit [ex], using a whitespace-separated language
    # boundary. Punctuation stays with its Chinese phrase, including Chinese
    # clauses with spaces. Do not split at Chinese sentence punctuation.
    boundary = value.rfind(" ", 0, cyr.start())
    if boundary == -1:
        raise _Unsupported("Example language boundary has no separator")
    chinese = value[:boundary].strip()
    translation = value[boundary + 1:].strip()
    # Parenthesized Russian starts '(русский...)', so the opening bracket is
    # naturally on the translation side. If the prefix contains Latin words,
    # guessing where e.g. 'iPhone' belongs would be lossy: retain legacy text.
    if not _CJK.search(chinese) or _CYR.search(chinese):
        raise _Unsupported("Example is not Chinese followed by Russian")
    if not _CJK.search(chinese.rsplit(" ", 1)[-1]):
        # '已经九点了? 9 часов' and '你好 — привет' have a leading
        # non-Cyrillic translation token. The source does not mark which side
        # owns it, so do not silently move '9' or '—' into the Chinese phrase.
        raise _Unsupported("Ambiguous numeric/punctuation translation prefix")
    if any(ch.isalpha() and "LATIN" in unicodedata.name(ch, "") for ch in chinese):
        raise _Unsupported("Ambiguous Latin text before Russian translation")
    if _CJK.search(translation):
        raise _Unsupported("Interleaved Chinese and Russian example")
    return {"chinese": chinese, "translation": translation}


def _examples(node: _Node) -> list[dict]:
    """Read only explicit example containers, preserving their original order."""
    if node.tag == "ex":
        return [_example(node)]
    if node.tag not in {"m2", "*"}:
        raise _Unsupported("Unsupported example wrapper")
    result = []
    for child in node.children:
        if isinstance(child, str):
            if child.strip():
                raise _Unsupported("Text outside explicit example")
        else:
            result.extend(_examples(child))
    if not result:
        raise _Unsupported("Empty example paragraph")
    return result


def _sense(node: _Node) -> dict:
    references: list[dict] = []
    parts = []
    examples = []
    for child in node.children:
        if isinstance(child, _Node) and child.tag in {"m2", "ex"}:
            examples.extend(_examples(child))
            parts.append(" ")
        else:
            parts.append(_inline(child, references))
    definition = _clean("".join(parts))
    number = None
    match = _NUMBER.match(definition)
    if match:
        number = match.group(1)
        definition = definition[match.end():].strip()
    if not definition:
        raise _Unsupported("Empty sense definition")
    return {"number": number, "definition": definition,
            "examples": examples, "references": references}


def _parse_v1(body: str) -> dict | None:
    """Return a version-1 article, or None if we cannot safely preserve meaning.

    Callers must retain ``body`` and their complete legacy fields on None. No
    heuristic migration from already flattened ``ru``/HTML is attempted here.
    """
    if not isinstance(body, str) or not body.strip():
        return None
    try:
        tree = _tree(body)
        prefix = []
        senses = []
        for child in tree.children:
            if isinstance(child, _Node) and child.tag == "m1":
                if not senses:
                    header = _clean("".join(prefix))
                    # BKRS uses a standalone '_' for missing pronunciation.
                    # Recognize only that exact header; underscores in a sense
                    # remain literal text and no pronunciation is invented.
                    if header and header != "_" and not _is_pinyin(header):
                        raise _Unsupported("Unrecognized article prefix")
                senses.append(_sense(child))
            elif isinstance(child, _Node) and child.tag in {"m2", "ex"}:
                if not senses:
                    raise _Unsupported("Example without preceding sense")
                senses[-1]["examples"].extend(_examples(child))
            elif not senses:
                prefix.append(_inline(child, []))
            elif _plain(child).strip():
                raise _Unsupported("Text outside a sense")
        if not senses:
            return None
        return {"version": 1, "senses": senses}
    except _Unsupported:
        return None


_PARAGRAPHS = {f"m{level}" for level in range(1, 5)}


def _block_tree(body: str) -> _Node:
    """Keep structural boundaries strict, while accepting crossed text styles.

    BKRS sometimes closes [c] before an inner [i]. These tags only style text,
    so their nesting does not define article semantics. Each style must still
    close in the structural container where it opened; [m], [ex], and [ref]
    cannot cross or be silently repaired. Styles are validated, then omitted.
    """
    root = _Node("root")
    stack = [root]
    styles: list[tuple[str, int]] = []
    text: list[str] = []

    def flush() -> None:
        if text:
            stack[-1].children.append("".join(text))
            text.clear()

    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body) and body[i + 1] in "[]{}\\":
            text.append(body[i + 1])
            i += 2
            continue
        if ch in "{}":
            raise _Unsupported("Unescaped DSL hidden-text syntax")
        if ch != "[":
            text.append(ch)
            i += 1
            continue
        end = body.find("]", i + 1)
        if end == -1:
            raise _Unsupported("Unclosed bracket")
        match = _NAME.fullmatch(body[i + 1:end])
        if not match:
            text.append(body[i:end + 1])
            i = end + 1
            continue
        closing, tag, attrs = match.groups()
        tag, attrs = tag.lower(), (attrs or "").strip()
        if tag not in _INLINE | _PARAGRAPHS | {"m", "ex", "ref"}:
            raise _Unsupported(f"Unsupported DSL tag: {tag}")
        if closing and attrs:
            raise _Unsupported("Closing tag attributes")
        if not closing and attrs and tag not in {"c", "ref"}:
            raise _Unsupported("Unsupported tag attributes")
        if tag in _INLINE:
            if closing:
                indexes = [j for j, (name, _) in enumerate(styles) if name == tag]
                if not indexes or styles[indexes[-1]][1] != len(stack):
                    raise _Unsupported("Style crosses structural boundary")
                styles.pop(indexes[-1])
            else:
                if len(stack) + len(styles) >= 64:
                    raise _Unsupported("Excessively nested DSL")
                styles.append((tag, len(stack)))
            # Do not insert a word boundary where the source has none.
            i = end + 1
            continue
        flush()
        if closing:
            expected = "m" if stack[-1].tag in _PARAGRAPHS else stack[-1].tag
            if len(stack) == 1 or tag != expected:
                raise _Unsupported("Mismatched DSL closing tag")
            if any(depth == len(stack) for _, depth in styles):
                raise _Unsupported("Unclosed style in structural container")
            stack.pop()
        else:
            if tag == "m":
                raise _Unsupported("Paragraph without explicit level")
            if len(stack) + len(styles) >= 64:
                raise _Unsupported("Excessively nested DSL")
            if stack[-1].tag in {"ex", "ref"} and tag != "ref":
                raise _Unsupported("Nested paragraph or example")
            if tag == "ref" and any(node.tag == "ref" for node in stack):
                raise _Unsupported("Nested reference")
            node = _Node(tag, attrs)
            stack[-1].children.append(node)
            stack.append(node)
        i = end + 1
    flush()
    if len(stack) != 1 or styles:
        raise _Unsupported("Unclosed DSL tag")
    return root


def _block(node: _Node, level: int, kind: str) -> dict:
    references: list[dict] = []
    value = _clean("".join(_inline(child, references) for child in node.children))
    if not value:
        raise _Unsupported("Empty article block")
    number = None
    chinese = translation = None
    if kind == "definition":
        # A decimal such as '1.5 метра' is content, not a numbered sense.
        match = re.match(r"^([0-9]+)(?:\)\s*|\.(?:\s+|$))", value)
        if match:
            number, value = match.group(1), value[match.end():].strip()
            if not value:
                raise _Unsupported("Number without definition")
    else:
        # Keep the complete source example even when the v1 pair heuristic is
        # too ambiguous (pinyin, Latin words, interleaved languages, links).
        try:
            pair = _example(node)
            chinese, translation = pair["chinese"], pair["translation"]
        except _Unsupported:
            pass
    return {"level": level, "kind": kind, "number": number, "text": value,
            "references": references, "chinese": chinese, "translation": translation}


def _paragraph_blocks(node: _Node) -> list[dict]:
    """Emit source-order runs, with exactly the source's explicit indent level."""
    level = int(node.tag[1:])
    result: list[dict] = []
    pending = _Node("definition")

    def flush() -> None:
        if _plain(pending).strip():
            result.append(_block(pending, level, "definition"))
        pending.children.clear()

    for child in node.children:
        if isinstance(child, str) or child.tag == "ref":
            pending.children.append(child)
        elif child.tag == "ex":
            flush()
            result.append(_block(child, level, "example"))
        elif child.tag in _PARAGRAPHS:
            flush()
            result.extend(_paragraph_blocks(child))
        else:
            raise _Unsupported("Unexpected article structure")
    flush()
    if not result:
        raise _Unsupported("Empty paragraph")
    return result


def _parse_v2(body: str) -> dict | None:
    try:
        tree = _block_tree(body)
        prefix: list[str] = []
        blocks: list[dict] = []
        for child in tree.children:
            if isinstance(child, _Node) and child.tag in _PARAGRAPHS:
                if not blocks:
                    header = _clean("".join(prefix))
                    if header:
                        pronunciations = re.split(r"[,;]", header)
                        if not all(part.strip() == "_" or _is_pinyin(part.strip())
                                   for part in pronunciations):
                            raise _Unsupported("Unrecognized article prefix")
                blocks.extend(_paragraph_blocks(child))
            elif isinstance(child, str):
                if not blocks:
                    prefix.append(child)
                elif child.strip():
                    raise _Unsupported("Text outside a paragraph")
            else:
                raise _Unsupported("Structure outside an explicit paragraph")
        return {"version": 2, "blocks": blocks} if blocks else None
    except _Unsupported:
        return None


def parse_dsl_article(body: str) -> dict | None:
    """Prefer the unchanged v1 contract; otherwise preserve ordered v2 blocks.

    V2 records explicit source levels (1–4), not inferred ownership or new
    dictionary meanings. Callers must retain raw/legacy fields on None.
    """
    if not isinstance(body, str) or not body.strip():
        return None
    old = _parse_v1(body)
    if old is not None:
        return old
    ordered = _parse_v2(body)
    if ordered is not None:
        return ordered
    # Keep both established contracts unchanged. Recovery canonicalizes only
    # previously unsupported source and is validated by this same v2 parser.
    from dictapp.dsl_recovery import recovery_source
    recovered = recovery_source(body)
    return _parse_v2(recovered) if recovered is not None else None
