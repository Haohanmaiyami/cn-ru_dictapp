"""Lossless canonical DSL for layouts the established article parsers reject.

This pass does not repair dictionary meanings or malformed structural markup.
It gives visible root prose an explicit level, retains literal single braces,
and makes correctly paired inline styles local to each text span. The original
v2 parser remains the validator and semantic projection of the resulting DSL.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
import unicodedata

from dictapp.dsl_parser import _INLINE, _NAME, _PARAGRAPHS, _Node, _Unsupported, _is_pinyin, _reference


@dataclass
class _Span:
    text: str
    styles: tuple[tuple[str, str], ...]


@dataclass
class _Container:
    tag: str
    attrs: str = ""
    children: list[_Span | "_Container"] = field(default_factory=list)


def _without_header(body: str) -> str:
    """Remove only an established pronunciation occupying the first line.

    Annotated, uncertain or punctuation-damaged pronunciations stay visible.
    In particular, accepting an arbitrary prefix up to the first [m] would
    incorrectly discard definitions that happen to precede that paragraph.
    """
    first, separator, rest = body.partition("\n")
    if separator:
        parts = re.split(r"[,;]", first.strip())
        # The old _is_pinyin deliberately accepts plain Latin letters, which
        # alone cannot distinguish a pronunciation from English body prose.
        # Recovery requires an explicit tone mark; uncertain neutral/numbered
        # pronunciation stays visible rather than risking text loss.
        def pronunciation(part: str) -> bool:
            value = part.strip()
            return value == "_" or (_is_pinyin(value) and any(
                char in "\u0300\u0301\u0304\u030c" for char in unicodedata.normalize("NFD", value)))
        if all(pronunciation(part) for part in parts):
            return rest
    return body


def _tree(body: str) -> _Container:
    root = _Container("root")
    stack = [root]
    # Paragraph boundaries may cross style scopes; links/examples may not.
    styles: list[tuple[str, str, tuple[int, ...]]] = []
    pending: list[str] = []

    def scope() -> tuple[int, ...]:
        return tuple(id(node) for node in stack if node.tag in {"ref", "ex"})

    def flush() -> None:
        if pending:
            stack[-1].children.append(_Span("".join(pending),
                                           tuple((tag, attrs) for tag, attrs, _ in styles)))
            pending.clear()

    offset = 0
    while offset < len(body):
        char = body[offset]
        if char == "\\" and offset + 1 < len(body) and body[offset + 1] in "[]{}\\":
            pending.append(body[offset + 1])
            offset += 2
            continue
        if body.startswith("{{", offset) or body.startswith("}}", offset):
            # DSL comments are not visible body content. Handling comments is
            # deliberately separate from the literal single-brace recovery.
            raise _Unsupported("DSL comments require separate support")
        if char != "[":
            pending.append(char)
            offset += 1
            continue
        end = body.find("]", offset + 1)
        if end == -1:
            raise _Unsupported("Unclosed recovery bracket")
        match = _NAME.fullmatch(body[offset + 1:end])
        if not match:
            pending.append(body[offset:end + 1])
            offset = end + 1
            continue
        closing, tag, attrs = match.groups()
        tag, attrs = tag.lower(), (attrs or "").strip()
        if tag not in _INLINE | _PARAGRAPHS | {"m", "ex", "ref"}:
            raise _Unsupported("Unsupported recovery tag")
        if closing and attrs or not closing and attrs and tag not in {"c", "ref"}:
            raise _Unsupported("Unsupported recovery attributes")
        flush()
        if tag in _INLINE:
            if closing:
                matches = [index for index, (name, _, _) in enumerate(styles) if name == tag]
                if not matches or styles[matches[-1]][2] != scope():
                    raise _Unsupported("Style crosses a link/example boundary")
                styles.pop(matches[-1])
            else:
                styles.append((tag, attrs, scope()))
        elif closing:
            expected = "m" if stack[-1].tag in _PARAGRAPHS else stack[-1].tag
            if len(stack) == 1 or tag != expected:
                raise _Unsupported("Mismatched recovery structural close")
            if stack[-1].tag in {"ref", "ex"} and any(id(stack[-1]) in item[2] for item in styles):
                raise _Unsupported("Unclosed style inside link/example")
            if stack[-1].tag == "ref":
                # Validate even when the surrounding paragraph has no visible
                # text: empty/invalid links are malformed, not empty spacing.
                _reference(_Node("ref", stack[-1].attrs, [_plain(stack[-1])]))
            stack.pop()
        else:
            if tag == "m":
                raise _Unsupported("Paragraph has no explicit source level")
            if stack[-1].tag in {"ex", "ref"} and tag != "ref":
                raise _Unsupported("Nested structural example content")
            if tag == "ref" and any(node.tag == "ref" for node in stack):
                raise _Unsupported("Nested recovery reference")
            node = _Container(tag, attrs)
            stack[-1].children.append(node)
            stack.append(node)
        if len(stack) + len(styles) >= 64:
            raise _Unsupported("Excessive recovery nesting")
        offset = end + 1
    flush()
    if len(stack) != 1 or styles:
        raise _Unsupported("Unclosed recovery markup")
    return root


def _plain(node: _Span | _Container) -> str:
    if isinstance(node, _Span):
        return node.text
    return "".join(_plain(child) for child in node.children)


def _escape(text: str) -> str:
    return "".join("\\" + char if char in "\\[]{}" else char for char in text)


def _inline(node: _Span | _Container) -> str:
    if isinstance(node, _Span):
        text = _escape(node.text)
        for tag, attrs in reversed(node.styles):
            text = f"[{tag}{' ' + attrs if attrs else ''}]" + text + f"[/{tag}]"
        return text
    if node.tag != "ref":
        raise _Unsupported("Unexpected recovery inline structure")
    # The authoritative v2 reference parser validates labels and attributes.
    return (f"[ref{' ' + node.attrs if node.attrs else ''}]"
            + "".join(_inline(child) for child in node.children) + "[/ref]")


def _paragraphs(node: _Container) -> list[str]:
    level = 1 if node.tag == "root" else int(node.tag[1:])
    result: list[str] = []
    pending: list[_Span | _Container] = []

    def flush() -> None:
        if "".join(_plain(item) for item in pending).strip():
            result.append(f"[m{level}]" + "".join(_inline(item) for item in pending) + "[/m]")
        pending.clear()

    for child in node.children:
        if isinstance(child, _Span):
            if node.tag == "root":
                # Keep a visible uncertain header on its own physical line.
                lines = child.text.split("\n")
                for index, line in enumerate(lines):
                    if index:
                        flush()
                    pending.append(_Span(line, child.styles))
            else:
                pending.append(child)
        elif child.tag == "ref":
            pending.append(child)
        elif child.tag == "ex":
            flush()
            if _plain(child).strip():
                result.append(f"[m{level}][ex]" + "".join(_inline(part) for part in child.children)
                              + "[/ex][/m]")
        elif child.tag in _PARAGRAPHS:
            flush()
            result.extend(_paragraphs(child))
        else:
            raise _Unsupported("Unexpected recovery paragraph structure")
    flush()
    return result


def recovery_source(body: str) -> str | None:
    """Return conservative canonical DSL, or None for unsupported/malformed input.

    Intended only after the old v1/v2 parsing paths failed. It does not persist
    anything and never invents translations, example boundaries or link targets.
    """
    if not isinstance(body, str) or not body.strip() or body.strip() == "_":
        return None
    # A bare unstructured string does not establish an article-body boundary.
    if "\n" not in body and "[" not in body:
        return None
    # Check the whole source, including bracketed prose/attributes, so a
    # comment marker cannot bypass the tokenizer as part of one bracket token.
    offset = 0
    while offset < len(body):
        if body[offset] == "\\" and offset + 1 < len(body) and body[offset + 1] in "[]{}\\":
            offset += 2
            continue
        if body.startswith("{{", offset) or body.startswith("}}", offset):
            return None
        offset += 1
    try:
        return "".join(_paragraphs(_tree(_without_header(body)))) or None
    except _Unsupported:
        return None
