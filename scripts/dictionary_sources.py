"""Pure, strict reader for the four local BKRS/BRuKS source files.

This module has no application settings, database, or network imports. Includes
describe the same physical parts already selected here, so they are validated
but never recursively expanded. Body normalization matches the existing DSL
importer (line indentation is removed; DSL markup and text are retained).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterator


SOURCE_FILENAMES = (
    "dabkrs_1.dsl", "dabkrs_2.dsl", "dabkrs_3.dsl", "dabruks.dsl",
)
_INCLUDE = re.compile(r'#INCLUDE\s+"([^"\r\n]+)"\s*')
_METADATA = re.compile(r'#(?:NAME|INDEX_LANGUAGE|CONTENTS_LANGUAGE)\s+"[^"\r\n]*"\s*')


class SourceFormatError(ValueError):
    """A local file cannot be read completely without guessing or text loss."""


@dataclass(frozen=True)
class SourceArticle:
    source: str
    ordinal: int
    heads: tuple[str, ...]
    body: str


def _directive(line: str, source: Path, known: set[Path]) -> None:
    include = _INCLUDE.fullmatch(line)
    if include:
        # Absolute paths, nested paths, symlinks, unknown sources, and self
        # references are outside the fixed local-source contract.
        name = include.group(1)
        target = source.parent / name
        if (name not in SOURCE_FILENAMES or target.is_symlink()
                or target.resolve() not in known or target.resolve() == source):
            raise SourceFormatError(f"{source.name}: unsupported include {name!r}")
        return
    if not _METADATA.fullmatch(line):
        raise SourceFormatError(f"{source.name}: unsupported DSL directive")


def inspect_source_files(data_dir: Path) -> tuple[Path, ...]:
    """Validate source paths, encoding markers, and header include directives.

    Full strict decoding continues during iteration. A malformed late line
    raises rather than substituting characters or silently skipping an entry.
    """
    root = Path(data_dir).resolve(strict=True)
    files: list[Path] = []
    for name in SOURCE_FILENAMES:
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise SourceFormatError(f"Missing or non-regular local source: {name}")
        resolved = path.resolve(strict=True)
        if resolved.parent != root:
            raise SourceFormatError(f"Source outside local data directory: {name}")
        with resolved.open("rb") as stream:
            if stream.read(2) not in {b"\xff\xfe", b"\xfe\xff"}:
                raise SourceFormatError(f"{name}: UTF-16 byte-order marker required")
        files.append(resolved)
    known = set(files)
    for path in files:
        with path.open("r", encoding="utf-16", errors="strict") as stream:
            for raw in stream:
                line = raw.strip()
                if not line:
                    continue
                if not line.startswith("#"):
                    break
                _directive(line, path, known)
    return tuple(files)


def iter_source_articles(data_dir: Path) -> Iterator[SourceArticle]:
    """Yield each physical article once, keeping consecutive headword aliases.

    Distinct bodies with the same headword are deliberately preserved. Choosing
    which entries to retain or map to existing IDs belongs to the local loader.
    """
    files = inspect_source_files(data_dir)
    known = set(files)
    for path in files:
        heads: list[str] = []
        body_lines: list[str] = []
        has_body = False
        ordinal = 0
        with path.open("r", encoding="utf-16", errors="strict") as stream:
            for line_number, raw in enumerate(stream, 1):
                line = raw.rstrip("\r\n")
                stripped = line.strip()
                if "\ufffd" in line or "\x00" in line:
                    raise SourceFormatError(f"{path.name}:{line_number}: unexpected replacement/NUL character")
                if not stripped:
                    if has_body:
                        body_lines.append("")
                    continue
                if stripped.startswith("#"):
                    if heads or ordinal:
                        raise SourceFormatError(f"{path.name}:{line_number}: directive after article start")
                    _directive(stripped, path, known)
                    continue
                if line[0] not in " \t":
                    if has_body:
                        ordinal += 1
                        yield SourceArticle(path.name, ordinal, tuple(heads), "\n".join(body_lines).strip())
                        heads, body_lines, has_body = [], [], False
                    heads.append(stripped)
                else:
                    if not heads:
                        raise SourceFormatError(f"{path.name}:{line_number}: body without a headword")
                    body_lines.append(stripped)
                    has_body = True
        if heads:
            if not has_body:
                raise SourceFormatError(f"{path.name}: headword without an article body")
            ordinal += 1
            yield SourceArticle(path.name, ordinal, tuple(heads), "\n".join(body_lines).strip())
