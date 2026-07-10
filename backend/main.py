import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.db.database import init_db, AsyncSessionLocal
from backend.routers.auth_router import router as auth_router
from backend.routers.chat_router import router as chat_router
from backend.services.zoho_service import list_all_invoices, list_recurring_invoices, get_invoice_stats, send_invoice_email, send_estimate_email

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up — initialising database…")
    await init_db()
    
    # Run additive migrations on invoice_cache
    from sqlalchemy import text
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("ALTER TABLE invoice_cache ADD COLUMN IF NOT EXISTS invoice_date DATE;"))
            await session.execute(text("ALTER TABLE invoice_cache ADD COLUMN IF NOT EXISTS last_payment_date DATE;"))
            await session.commit()
        logger.info("Additive database migrations applied successfully.")
    except Exception as e:
        logger.error(f"Failed to apply database migrations: {e}")
        
    logger.info("Ready.")
    yield
    logger.info("Shutting down.")


app = FastAPI(
    title="Gmail → Zoho Invoice Agent",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(chat_router)

# Serve the frontend's static assets under /static
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(
        os.path.join(FRONTEND_DIR, "index.html"),
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/api/invoices")
async def api_invoices(status: str = Query(default="all")):
    """Return all invoices, optionally filtered by status."""
    async with AsyncSessionLocal() as db:
        try:
            invoices = await list_all_invoices(db, status_filter=status)
            return JSONResponse({"invoices": invoices})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/invoices/recurring")
async def api_recurring_invoices():
    """Return all active recurring invoices."""
    async with AsyncSessionLocal() as db:
        try:
            invoices = await list_recurring_invoices(db)
            return JSONResponse({"recurring_invoices": invoices})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/stats")
async def api_stats():
    """Return invoice statistics and 6-month revenue history."""
    async with AsyncSessionLocal() as db:
        try:
            stats = await get_invoice_stats(db)
            return JSONResponse(stats)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/invoices/send")
async def api_send_invoice(body: dict):
    """Send an existing invoice to the client via Zoho email API."""
    invoice_id = (body.get("invoice_id") or "").strip()
    to_email   = (body.get("to_email")   or "").strip() or None
    if not invoice_id:
        return JSONResponse({"error": "invoice_id is required"}, status_code=400)
    async with AsyncSessionLocal() as db:
        try:
            ok, reason = await send_invoice_email(invoice_id, db, to_email=to_email)
            if ok:
                return JSONResponse({"success": True})
            return JSONResponse({"error": reason or "Failed to send invoice"}, status_code=500)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/estimates/send")
async def api_send_estimate(body: dict):
    """Send an existing estimate to the client via Zoho email API."""
    estimate_id = (body.get("estimate_id") or "").strip()
    to_email   = (body.get("to_email")   or "").strip() or None
    if not estimate_id:
        return JSONResponse({"error": "estimate_id is required"}, status_code=400)
    async with AsyncSessionLocal() as db:
        try:
            ok, reason = await send_estimate_email(estimate_id, db, to_email=to_email)
            if ok:
                return JSONResponse({"success": True})
            return JSONResponse({"error": reason or "Failed to send estimate"}, status_code=500)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
