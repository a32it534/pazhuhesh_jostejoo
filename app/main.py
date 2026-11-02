"""FastAPI application: REST endpoints, SSE streaming and static frontend."""
from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import STATIC_DIR, load_settings
from .llm import build_llm
from .pipeline import JobManager, JobState
from .schemas import JobCreate, JobCreated
from .storage import Storage

settings = load_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    llm = build_llm(settings)
    storage = Storage(settings.db_path)
    manager = JobManager(settings, llm, storage)
    app.state.llm = llm
    app.state.storage = storage
    app.state.manager = manager
    cleanup = asyncio.create_task(manager.cleanup_loop())
    try:
        yield
    finally:
        cleanup.cancel()
        await manager.shutdown()


app = FastAPI(title="پژوهش و جستجو", version="1.0.0", lifespan=lifespan)

# CORS is restricted to localhost origins only.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def validation_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    message = "ورودی نامعتبر است. موضوع را (۳ تا ۵۰۰ نویسه) و عمق پژوهش را بررسی کنید."
    for err in exc.errors():
        text = str(err.get("msg", ""))
        if text.startswith("Value error, "):
            message = text[len("Value error, "):]
            break
    return JSONResponse(status_code=422, content={"detail": message})


# ------------------------------------------------------------------ helpers

def _manager(request: Request) -> JobManager:
    return request.app.state.manager


def _storage(request: Request) -> Storage:
    return request.app.state.storage


def _sse(event: dict[str, Any]) -> str:
    return f"id: {event['seq']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


async def _event_stream(job: JobState, start: int) -> AsyncIterator[str]:
    index = start
    while True:
        while index < len(job.events):
            yield _sse(job.events[index])
            index += 1
        if job.finished and index >= len(job.events):
            return
        ping = False
        async with job.cond:
            if index >= len(job.events) and not job.finished:
                try:
                    await asyncio.wait_for(job.cond.wait(), timeout=15)
                except asyncio.TimeoutError:
                    ping = True
        if ping:
            yield ": ping\n\n"


def _safe_filename(topic: str, rid: str) -> str:
    cleaned = re.sub(r"[^\w\- ]", "", topic, flags=re.UNICODE).strip().replace(" ", "-")[:60]
    return f"{cleaned or 'report'}-{rid[:8]}.md"


# ------------------------------------------------------------------ jobs

@app.post("/api/jobs", response_model=JobCreated)
async def create_job(payload: JobCreate, request: Request) -> JobCreated:
    manager = _manager(request)
    if manager.running_count() >= settings.max_concurrent_jobs:
        raise HTTPException(429, "چند پژوهش هم‌زمان در حال اجراست. کمی صبر کنید و دوباره امتحان کنید.")
    job = manager.create(payload.topic, payload.depth)
    return JobCreated(job_id=job.id)


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request) -> StreamingResponse:
    job = _manager(request).get(job_id)
    if job is None:
        raise HTTPException(404, "این پژوهش پیدا نشد (شاید منقضی شده باشد).")
    start = 0
    last_id = request.headers.get("last-event-id")
    if last_id and last_id.isdigit():
        start = int(last_id) + 1
    return StreamingResponse(
        _event_stream(job, start),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request) -> dict[str, str]:
    job = _manager(request).get(job_id)
    if job is None:
        raise HTTPException(404, "این پژوهش پیدا نشد.")
    if job.finished:
        return {"status": job.status}
    job.request_cancel()
    return {"status": "cancelling"}


@app.get("/api/jobs/{job_id}/export")
async def export_job(job_id: str, request: Request, format: str = Query("md")) -> Response:
    if format != "md":
        raise HTTPException(400, "فقط قالب md پشتیبانی می‌شود.")
    topic, report = "", ""
    job = _manager(request).get(job_id)
    if job is not None and job.report_md:
        topic, report = job.topic, job.report_md
    else:
        row = await _storage(request).get_report(job_id)
        if row is None:
            raise HTTPException(404, "گزارشی برای دانلود پیدا نشد.")
        topic, report = row["topic"], row["report"]
    name = _safe_filename(topic, job_id)
    disposition = f"attachment; filename=\"report-{job_id[:8]}.md\"; filename*=UTF-8''{quote(name)}"
    return Response(content=report, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": disposition})


# ------------------------------------------------------------------ history

@app.get("/api/history")
async def history(request: Request) -> list[dict[str, Any]]:
    return await _storage(request).list_reports()


@app.get("/api/history/{item_id}")
async def history_item(item_id: str, request: Request) -> dict[str, Any]:
    row = await _storage(request).get_report(item_id)
    if row is None:
        raise HTTPException(404, "این گزارش پیدا نشد.")
    return row


@app.delete("/api/history/{item_id}")
async def history_delete(item_id: str, request: Request) -> dict[str, bool]:
    if not await _storage(request).delete_report(item_id):
        raise HTTPException(404, "این گزارش پیدا نشد.")
    return {"deleted": True}


# ------------------------------------------------------------------ health

@app.get("/api/health")
async def health(request: Request) -> dict[str, Any]:
    llm = request.app.state.llm
    available, detail = await llm.ping()
    return {"status": "ok", "provider": llm.name, "model": llm.model,
            "llm_available": available, "detail": detail}


# ------------------------------------------------------------------ frontend

@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
