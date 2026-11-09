import os
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import psycopg
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field
from dotenv import load_dotenv

import provider
from ledger import apply_question_constraints, execute_query, local_query, overview, transactions_from_documents
from scrub import LIMIT, combine, extract, redact, safe_name

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def connection():
    """Open a short-lived PostgreSQL connection using the configured URL."""
    return psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=5)


@asynccontextmanager
async def lifespan(_):
    """Create the tables before serving requests; startup fails if PostgreSQL is unavailable."""
    with connection() as db:
        db.execute((ROOT / "schema.sql").read_text())
    yield


app = FastAPI(title="Statement Scrubber", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]"])
app.mount("/web", StaticFiles(directory=ROOT / "web"), name="web")


@app.middleware("http")
async def local_boundary(request: Request, call_next):
    """Restrict browser origins and uploads, then apply common response protections."""
    if request.headers.get("origin") and request.headers["origin"] != f"{request.url.scheme}://{request.headers.get('host')}":
        return JSONResponse({"detail": "Cross-origin requests are disabled."}, status_code=403)
    if request.url.path == "/api/batches" and request.method == "POST":
        size = request.headers.get("content-length", "")
        if not size.isdecimal() or int(size) > 24_100_000:
            return JSONResponse({"detail": "Upload requires Content-Length and must be under 24 MB."}, status_code=413)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none'; base-uri 'none'"
    return response


@app.get("/")
def index():
    """Serve the single-page frontend."""
    return FileResponse(ROOT / "web" / "index.html")


@app.post("/api/batches", status_code=201)
def upload(files: list[UploadFile] = File(...)):
    """Scrub each uploaded file locally, then save the complete batch atomically."""
    if not 1 <= len(files) <= 20:
        raise HTTPException(422, "Select 1–20 statement files.")
    documents = []
    for index, file in enumerate(files, 1):
        try:
            # Read at most one byte past the supported limit so oversized files are rejected.
            name = safe_name(file.filename or "")
            documents.append((index, name, extract(name, file.file.read(LIMIT + 1))))
        except Exception:
            raise HTTPException(422, f"File {index} could not be safely extracted. Check format, size and text layer.") from None
    batch = uuid4()
    with connection() as db:
        db.execute("INSERT INTO batches (id) VALUES (%s)", (batch,))
        with db.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO documents (batch_id, ordinal, filename, scrubbed_text) VALUES (%s,%s,%s,%s)",
                [(batch, *doc) for doc in documents],
            )
    return {"id": batch, "files": len(documents)}


def get_documents(db, batch):
    """Load a batch in upload order and treat an empty result as an unknown batch."""
    documents = db.execute(
        "SELECT ordinal, filename, scrubbed_text FROM documents WHERE batch_id=%s ORDER BY ordinal",
        (batch,),
    ).fetchall()
    if not documents:
        raise HTTPException(404, "Batch not found.")
    return documents


@app.get("/api/batches/{batch}", response_class=PlainTextResponse)
def read(batch: UUID):
    """Return the combined scrubbed content that was actually persisted."""
    with connection() as db:
        return combine((name, body) for _, name, body in get_documents(db, batch))


@app.get("/api/batches/{batch}/storage")
def storage(batch: UUID):
    """Return storage metadata without sending the full saved statement text."""
    with connection() as db:
        documents = get_documents(db, batch)
        engine = db.execute("SELECT current_setting('server_version')").fetchone()[0]
    return {"postgresql_version": engine, "rows": [
        {"ordinal": ordinal, "filename": name, "characters": len(body)} for ordinal, name, body in documents
    ]}


def provider_error(exc):
    """Translate provider exceptions into concise messages safe to show in the UI."""
    if isinstance(exc, RuntimeError):
        return "Groq is unavailable or not configured."
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
        return "Groq rate limit reached. Wait briefly before retrying."
    if isinstance(exc, httpx.HTTPStatusError):
        detail = ""
        try:
            error = exc.response.json().get("error")
            if isinstance(error, dict):
                detail = error.get("message", "")
            elif isinstance(error, str):
                detail = error
        except ValueError:
            pass
        detail = " ".join(str(detail).split())[:200]
        suffix = f": {detail}" if detail else ""
        return f"Groq rejected the request (HTTP {exc.response.status_code}){suffix}"
    return "Groq returned an invalid response."


def analysis_result(documents):
    """Calculate authoritative local facts first and make Groq highlights optional."""
    rows = transactions_from_documents(documents)
    _, local_facts = overview(rows)
    local_values = list(local_facts.values())
    result = {
        "status": "local_only",
        "message": "Local totals are computed from parsed statement transactions.",
        "facts": local_values,
        "highlights": [],
        "tips": [],
        "parsed_rows": len(rows),
        "verified_outflows": [fact for fact in local_values if "outflow" in fact.casefold()],
    }
    if not rows:
        result.update(
            status="no_data",
            message="No supported transaction rows were found; no totals can be verified.",
        )
        return result
    try:
        # Groq can select from locally computed evidence, but it never calculates totals.
        selected = provider.ask(local_facts)
        if selected["status"] == "ok":
            result.update(status="ok", message=selected["message"],
                          highlights=selected["facts"], tips=selected["tips"])
        else:
            result["message"] = (
                "Local totals are shown. Groq found no valid highlight; retry Generate analysis to try again."
            )
    except (RuntimeError, httpx.HTTPStatusError, httpx.RequestError, ValueError, KeyError, IndexError, TypeError) as exc:
        result["message"] = f"Local totals are shown. {provider_error(exc)} Retry Generate analysis to try again."
    return result


@app.post("/api/batches/{batch}/analysis")
def analyze(batch: UUID):
    """Recompute a batch's analysis and upsert the latest result for display."""
    with connection() as db:
        documents = get_documents(db, batch)
    result = {**analysis_result(documents), "model": provider.MODEL}
    with connection() as db:
        db.execute(
            "INSERT INTO analyses VALUES (%s,%s) ON CONFLICT (batch_id) DO UPDATE SET result=EXCLUDED.result",
            (batch, Jsonb(result)),
        )
    return result


class Question(BaseModel):
    """Bound chat input and keep only a small amount of client-provided context."""

    question: str = Field(min_length=1, max_length=500)
    previous_questions: list[str] = Field(default_factory=list, max_length=3)


@app.post("/api/batches/{batch}/chat")
def chat(batch: UUID, payload: Question):
    """Interpret a question, calculate its answer locally, and optionally phrase it with Groq."""
    question = redact(payload.question.strip())
    if not question:
        raise HTTPException(422, "Enter a question.")
    if any(len(item) > 500 for item in payload.previous_questions):
        raise HTTPException(422, "Previous questions must be under 500 characters.")
    history = [redact(item) for item in payload.previous_questions]
    with connection() as db:
        documents = get_documents(db, batch)
    if any(word in question.lower() for word in ("ignore instructions", "system prompt", "unredact", "reveal identifiers")):
        return {
            "status": "unsupported",
            "message": "I can answer budgeting questions using scrubbed transaction evidence.",
            "facts": [],
            "tips": [],
        }
    rows = transactions_from_documents(documents)
    if not rows:
        return {"status": "unsupported", "message": "No supported transaction rows were found.", "facts": [], "tips": []}
    provider_statuses = []
    try:
        # Groq translates natural language to filters; local parsing is the bounded fallback.
        query = provider.translate_question(question, history, rows)
    except (RuntimeError, httpx.HTTPStatusError, httpx.RequestError, ValueError, KeyError, IndexError, TypeError) as exc:
        provider_statuses.append(provider_error(exc))
        query = local_query(question, rows)
        if query.get("status") != "ok":
            return {
                **execute_query(rows, query),
                "provider_status": provider_error(exc),
                "mode": "clarification",
            }
    apply_question_constraints(query, question, rows)
    result = execute_query(rows, query)
    result["mode"] = "chat"
    result["answer_source"] = "local"
    if result["status"] != "ok":
        return result
    facts = {f"E{index}": fact for index, fact in enumerate(result["facts"], 1)}
    try:
        # The response may only cite these facts, which have already been calculated locally.
        filter_names = (
            "metric", "category", "merchant", "date_from", "date_to", "currency",
            "exclude_categories", "exclude_merchants",
        )
        verified_filters = {
            name: query.get(name).isoformat() if isinstance(query.get(name), date) else query.get(name)
            for name in filter_names
        }
        answer = provider.compose_chat_answer(question, history, facts, verified_filters)
        result["message"] = answer["message"]
        result["facts"] = [facts[key] for key in answer["fact_ids"]]
        result["answer_source"] = "groq"
        if query["metric"] == "most_frequent":
            result["message"] += (
                " Merchant frequency uses exact scrubbed descriptions, so merchant name variants may appear separately."
            )
    except (RuntimeError, httpx.HTTPStatusError, httpx.RequestError, ValueError, KeyError, IndexError, TypeError) as exc:
        provider_statuses.append(provider_error(exc))
        result["message"] = f"Here are the locally verified results: {'; '.join(result['facts'])}"
        if query.get("category") == "Tax":
            result["message"] += " These are explicit tax-labelled outflows, not a complete tax liability."
    if provider_statuses:
        result["provider_status"] = "; ".join(dict.fromkeys(provider_statuses))
    return result
