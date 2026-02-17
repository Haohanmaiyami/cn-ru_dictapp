from sqladmin import Admin
from dictapp.db import engine
from dictapp.admin import EntryAdmin
from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from dictapp.db import get_session
from dictapp.repo import get_entry_by_id, search_entries
from dictapp.schemas import EntryOut, SearchResponse

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

app = FastAPI(title="Chinese-Russian Dictionary MVP")
app.mount("/static", StaticFiles(directory="src/dictapp/static"), name="static")

templates = Jinja2Templates(directory="src/dictapp/templates")
admin = Admin(app, engine)
admin.add_view(EntryAdmin)


@app.get("/api/search", response_model=SearchResponse)
async def api_search(
    q: str = Query(..., min_length=1, description="Search query"),
    limit: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
):
    results = await search_entries(session, q=q, limit=limit)
    return SearchResponse(q=q, count=len(results), results=results)


@app.get("/api/entry/{entry_id}", response_model=EntryOut)
async def api_entry(
    entry_id: int,
    session: AsyncSession = Depends(get_session),
):
    entry = await get_entry_by_id(session, entry_id=entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Entry not found")
    return entry

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
    q_clean = (q or "").strip()
    results = []
    if q_clean:
        results = await search_entries(session, q=q_clean, limit=limit)

    return templates.TemplateResponse(
        "results.html",
        {
            "request":request,
            "q": q_clean,
            "count": len(results),
            "results": results,
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

    return templates.TemplateResponse(
        "entry.html",
        {"request": request, "entry": entry},
    )






