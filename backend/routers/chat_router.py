from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.database import get_db
from backend.schemas import ChatRequest, ChatResponse, ApproveRequest
from backend.services.pipeline import process_chat, approve_draft

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("", response_model=ChatResponse)
async def chat(request: ChatRequest, db: AsyncSession = Depends(get_db)):
    return await process_chat(request.message, db)


@router.post("/approve", response_model=ChatResponse)
async def approve(request: ApproveRequest, db: AsyncSession = Depends(get_db)):
    return await approve_draft(
        request.draft_id,
        {
            "task_description": request.task_description,
            "amount": request.amount,
            "currency": request.currency,
        },
        db,
    )
