from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError, model_serializer, model_validator


class ArticleExample(BaseModel):
    chinese: str
    translation: str


class ArticleReference(BaseModel):
    target: str
    label: str


class ArticleSense(BaseModel):
    number: str | None = None
    definition: str
    examples: list[ArticleExample] = Field(default_factory=list)
    references: list[ArticleReference] = Field(default_factory=list)


class ArticleBlock(BaseModel):
    """An ordered source paragraph; level is DSL indentation, not guessed meaning."""
    level: int = Field(strict=True, ge=1, le=4)
    kind: Literal["definition", "example"]
    number: str | None = None
    text: str
    references: list[ArticleReference] = Field(default_factory=list)
    chinese: str | None = None
    translation: str | None = None

    @model_validator(mode="after")
    def preserve_readable_content(self):
        if not self.text.strip():
            raise ValueError("Block must contain its complete readable text")
        if self.number is not None and (not self.number or not self.number.isascii()
                                        or not self.number.isdecimal()):
            raise ValueError("Only a numeric definition prefix may be extracted")
        if any(not r.target.strip() or not r.label.strip() for r in self.references):
            raise ValueError("Reference must contain target and label")
        if self.kind == "definition":
            if self.chinese is not None or self.translation is not None:
                raise ValueError("A definition cannot be an example pair")
        else:
            if self.number is not None:
                raise ValueError("Example numbering must remain in its text")
            if (self.chinese is None) != (self.translation is None):
                raise ValueError("Example pair must be complete or absent")
            if self.chinese is not None and (not self.chinese.strip() or not self.translation.strip()):
                raise ValueError("Example pair must not contain empty parts")
        return self


class ArticleTextRun(BaseModel):
    """Additive styling only: text remains authoritative in the original row."""
    model_config = ConfigDict(extra="forbid")

    text: str = Field(strict=True, min_length=1)
    italic: StrictBool = False
    bold: StrictBool = False
    underline: StrictBool = False
    color: Literal["green", "blue", "red", "gray", "primary", "brown", "violet", "crimson"] | None = None


class ArticleFormatting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(strict=True, ge=1, le=1)
    rows: list[list[ArticleTextRun]]


class ArticleSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dictionary: Literal["bruks"]
    language: Literal["ru"]
    headword: str = Field(strict=True, min_length=1)


class DictionaryArticle(BaseModel):
    version: Literal[1, 2]
    senses: list[ArticleSense] = Field(default_factory=list)
    blocks: list[ArticleBlock] = Field(default_factory=list)
    formatting: ArticleFormatting | None = None
    source: ArticleSource | None = None

    @model_validator(mode="before")
    @classmethod
    def optional_formatting_cannot_break_content(cls, value):
        if isinstance(value, dict) and value.get("formatting") is not None:
            # Copy the mapping: cached ORM JSON must never be mutated by a read.
            value = dict(value)
            try:
                value["formatting"] = ArticleFormatting.model_validate(value["formatting"])
            except ValidationError:
                value["formatting"] = None
        return value

    @model_validator(mode="after")
    def has_content(self):
        if self.version == 1:
            if self.blocks or not any(s.definition.strip() or s.examples or s.references for s in self.senses):
                raise ValueError("Version 1 requires readable senses only")
        elif self.senses or not self.blocks:
            raise ValueError("Version 2 requires ordered blocks only")
        if self.formatting is not None:
            expected = self.row_texts()
            actual = ["".join(run.text for run in row) for row in self.formatting.rows]
            if actual != expected:
                self.formatting = None
        return self

    def row_texts(self) -> list[str]:
        """Exactly the Swift DictionaryArticle.rows text, without number prefixes."""
        if self.version == 2:
            return [block.text for block in self.blocks]
        rows = []
        for sense in self.senses:
            if sense.definition or sense.number is not None or sense.references:
                rows.append(sense.definition)
            rows.extend(example.chinese + (" — " + example.translation if example.translation else "")
                        for example in sense.examples)
        return rows

    @model_serializer(mode="wrap")
    def serialize_version(self, handler):
        # Keep the version-specific fields stable. Optional formatting is
        # additive; older clients can continue using the original plain text.
        result = handler(self)
        result.pop("blocks" if self.version == 1 else "senses", None)
        if self.formatting is None:
            result.pop("formatting", None)
        if self.source is None:
            result.pop("source", None)
        return result

class EntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    hanzi: str
    pinyin: str | None = None
    ru: str
    pos: str | None =  None
    examples: str | None = None
    article: DictionaryArticle | None = None
    headword: str | None = None
    headword_language: Literal["ru"] | None = None

    @model_serializer(mode="wrap")
    def serialize_optional_direction(self, handler):
        result = handler(self)
        if self.headword is None or self.headword_language is None:
            result.pop("headword", None)
            result.pop("headword_language", None)
        return result

class SearchResponse(BaseModel):
    q: str
    count: int
    results: list[EntryOut]

class AIAnalyzeRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


class AIDictionaryHit(BaseModel):
    hanzi: str
    pinyin: str | None = None
    ru: str
    pos: str | None = None


class AIAnalyzeResponse(BaseModel):
    text: str
    literal: str
    natural: str
    pinyin: str
    keywords: list[str]
    dictionary_hits: list[AIDictionaryHit]


class AITranslateRuToCnRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


class AITranslateRuToCnResponse(BaseModel):
    text: str
    translation: str
    pinyin: str = ""
