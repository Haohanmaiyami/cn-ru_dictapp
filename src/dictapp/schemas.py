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
