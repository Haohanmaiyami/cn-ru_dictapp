from pydantic import BaseModel, ConfigDict

class EntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    hanzi: str
    pinyin: str | None = None
    ru: str
    pos: str | None =  None
    examples: str | None = None

class SearchResponse(BaseModel):
    q: str
    count: int
    results: list[EntryOut]

class AIAnalyzeRequest(BaseModel):
    text: str


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
    text: str


class AITranslateRuToCnResponse(BaseModel):
    text: str
    translation: str
    pinyin: str = ""