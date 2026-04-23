from sqladmin import Admin
from dictapp.db import engine
from dictapp.admin import EntryAdmin
from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_, and_, func, case
from dictapp.db import get_session
from dictapp.repo import get_entry_by_id, search_entries
from dictapp.repo import find_dictionary_hits_for_text
from dictapp.schemas import EntryOut, SearchResponse
from dictapp.schemas import AIAnalyzeRequest, AIAnalyzeResponse, AIDictionaryHit
from dictapp.ollama_service import analyze_with_ollama
from dictapp.ollama_service import translate_ru_to_cn_with_ollama
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dictapp.schemas import AITranslateRuToCnRequest, AITranslateRuToCnResponse
from dictapp.repo import split_ru_examples

app = FastAPI(title="Chinese-Russian Dictionary MVP")
app.mount("/static", StaticFiles(directory="src/dictapp/static"), name="static")

templates = Jinja2Templates(directory="src/dictapp/templates")
admin = Admin(app, engine)
admin.add_view(EntryAdmin)


@app.get("/api/health")
async def api_health():
    return {"status": "ok"}

@app.get("/api/search", response_model=SearchResponse)
async def api_search(
    q: str = Query(..., min_length=1, description="Search query"),
    limit: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
):
    q_clean = " ".join((q or "").strip().split())

    if not q_clean:
        results = []
    else:
        results = await search_entries(session, q=q_clean, limit=limit)

        cleaned_results = []
        for entry in results:
            translation, examples = split_ru_examples(entry.ru or "")
            entry.ru = translation
            entry.examples = examples
            cleaned_results.append(entry)

        results = cleaned_results

    return SearchResponse(q=q_clean, count=len(results), results=results)


@app.get("/api/entry/{entry_id}", response_model=EntryOut)
async def api_entry(
    entry_id: int,
    session: AsyncSession = Depends(get_session),
):
    entry = await get_entry_by_id(session, entry_id=entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Entry not found")
    translation, examples = split_ru_examples(entry.ru or "")

    return EntryOut(
        id=entry.id,
        hanzi=entry.hanzi,
        pinyin=entry.pinyin,
        ru=translation,
        pos=entry.pos,
        examples=examples,
    )


@app.post("/api/ai/analyze", response_model=AIAnalyzeResponse)
async def api_ai_analyze(
    payload: AIAnalyzeRequest,
    session: AsyncSession = Depends(get_session),
):
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Text is required")

    hits = await find_dictionary_hits_for_text(session, text=text, limit=12)
    analysis_data = await analyze_with_ollama(text=text, dictionary_entries=hits)

    return AIAnalyzeResponse(
        text=text,
        literal=analysis_data["literal"],
        natural=analysis_data["natural"],
        pinyin=analysis_data["pinyin"],
        keywords=analysis_data["keywords"],
        dictionary_hits=[
            AIDictionaryHit(
                hanzi=e.hanzi,
                pinyin=e.pinyin,
                ru=e.ru,
                pos=e.pos,
            )
            for e in hits
        ],
    )


@app.post("/api/ai/translate-ru-to-cn", response_model=AITranslateRuToCnResponse)
async def api_ai_translate_ru_to_cn(
    payload: AITranslateRuToCnRequest,
):
    text = (payload.text or "").strip()

    if not text:
        raise HTTPException(status_code=400, detail="Text is required")

    translation = await translate_ru_to_cn_with_ollama(text=text)

    return AITranslateRuToCnResponse(
        text=text,
        translation=translation,
    )

@app.get("/", response_class=HTMLResponse)
async def page_index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {"request": request},
    )

@app.get("/search", response_class=HTMLResponse)
async def page_search(
    request: Request,
    q: str = Query("", description="Search query"),
    limit: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
):
    q_clean = " ".join((q or "").strip().split())

    if not q_clean:
        results = []
    else:
        results = await search_entries(session, q=q_clean, limit=limit)
        cleaned_results = []

        for entry in results:
            translation, examples = split_ru_examples(entry.ru or "")

            cleaned_results.append(
                EntryOut(
                    id=entry.id,
                    hanzi=entry.hanzi,
                    pinyin=entry.pinyin,
                    ru=translation,
                    pos=entry.pos,
                    examples=examples,
                )
            )

        results = cleaned_results
        results = results or []

    return templates.TemplateResponse(
        "results.html",
        {
            "request": request,
            "q": q_clean,
            "results": results,
            "count": len(results),
        },
    )

@app.get("/entry/{entry_id}", response_class=HTMLResponse)
async def page_entry(
        request: Request,
        entry_id: int,
        session: AsyncSession = Depends(get_session),
):
    entry = await get_entry_by_id(session, entry_id=entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Entry not found")

    translation, examples = split_ru_examples(entry.ru or "")

    entry.ru = translation
    entry.examples = examples

    return templates.TemplateResponse(
        "entry.html",
        {"request": request, "entry": entry},
    )






