import asyncio
import json

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.database import get_db
from backend.schemas import (
    ChatRequest, ChatResponse, ApproveRequest, BatchApproveRequest, ManualInvoiceApproveRequest,
)
from backend.services.pipeline import process_chat, approve_draft, approve_batch, approve_manual_invoice

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("", response_model=ChatResponse)
async def chat(request: ChatRequest, db: AsyncSession = Depends(get_db)):
    return await process_chat(request.message, db)


@router.post("/stream")
async def chat_stream(request: ChatRequest, db: AsyncSession = Depends(get_db)):
    """SSE endpoint — streams live status updates then the final response."""
    queue: asyncio.Queue = asyncio.Queue()

    async def status_cb(text: str) -> None:
        await queue.put(("status", text))

    async def run() -> None:
        try:
            result = await process_chat(request.message, db, status_cb=status_cb)
            await queue.put(("done", result))
        except Exception as exc:
            await queue.put(("error", str(exc)))

    task = asyncio.create_task(run())

    async def event_stream():
        try:
            while True:
                event, payload = await queue.get()
                if event == "status":
                    yield f"event: status\ndata: {json.dumps({'text': payload})}\n\n"
                elif event == "done":
                    yield f"event: done\ndata: {payload.model_dump_json()}\n\n"
                    break
                elif event == "error":
                    yield f"event: error\ndata: {json.dumps({'text': payload})}\n\n"
                    break
        finally:
            task.cancel()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx buffering
            "Connection":      "keep-alive",
        },
    )


@router.post("/approve", response_model=ChatResponse)
async def approve(request: ApproveRequest, db: AsyncSession = Depends(get_db)):
    return await approve_draft(
        request.draft_id,
        {
            "item_name":        request.item_name,
            "task_description": request.task_description,
            "amount":           request.amount,
            "currency":         request.currency,
            "client_name":      request.client_name,
            "client_email":     request.client_email,
            "send_email":       request.send_email,
        },
        db,
    )

@router.post("/batch-approve", response_model=ChatResponse)
async def batch_approve(request: BatchApproveRequest, db: AsyncSession = Depends(get_db)):
    return await approve_batch(
        batch_draft_id=request.batch_draft_id,
        mode=request.mode,
        selected_item_ids=request.selected_item_ids,
        send_email=request.send_email,
        db=db,
    )


@router.post("/manual-approve", response_model=ChatResponse)
async def manual_approve(request: ManualInvoiceApproveRequest, db: AsyncSession = Depends(get_db)):
    return await approve_manual_invoice(
        draft_id=request.draft_id,
        send_email=request.send_email,
        db=db,
    )
