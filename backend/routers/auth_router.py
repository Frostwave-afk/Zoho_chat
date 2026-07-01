from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.database import get_db
from backend.db.models import OAuthToken
from backend.auth.gmail_auth import get_gmail_auth_url, exchange_gmail_code, is_gmail_connected
from backend.auth.zoho_auth import get_zoho_auth_url, exchange_zoho_code, is_zoho_connected
from backend.schemas import AuthStatus
from backend.services.pipeline import clear_session_state

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/status", response_model=AuthStatus)
async def auth_status(db: AsyncSession = Depends(get_db)):
    return AuthStatus(
        gmail=await is_gmail_connected(db),
        zoho=await is_zoho_connected(db),
    )


@router.get("/gmail/start")
async def gmail_start():
    url, _ = get_gmail_auth_url()
    return RedirectResponse(url)


@router.get("/gmail/callback")
async def gmail_callback(code: str, state: str, db: AsyncSession = Depends(get_db)):
    await exchange_gmail_code(code, state, db)
    return RedirectResponse("/?connected=gmail")


@router.get("/zoho/start")
async def zoho_start():
    return RedirectResponse(get_zoho_auth_url())


@router.get("/zoho/callback")
async def zoho_callback(code: str, db: AsyncSession = Depends(get_db)):
    await exchange_zoho_code(code, db)
    return RedirectResponse("/?connected=zoho")


@router.post("/logout")
async def logout(db: AsyncSession = Depends(get_db)):
    """Fully reset: wipe all tokens, caches, processed email history and
    in-memory session state so the next login starts completely fresh."""
    await clear_session_state(db)
    return {"message": "Logged out successfully."}
