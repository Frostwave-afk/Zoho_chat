from pydantic import BaseModel
from typing import Optional, List


# ── Inbound ─────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str


class ApproveRequest(BaseModel):
    draft_id: str
    task_description: Optional[str] = None
    amount: Optional[float] = None
    currency: Optional[str] = None
    client_name: Optional[str] = None


# ── Internal data shapes ─────────────────────────────────────────────────────

class InvoiceData(BaseModel):
    is_confirmation: bool = False
    client_name: Optional[str] = None
    client_email: Optional[str] = None
    task_description: Optional[str] = None
    amount: Optional[float] = None
    currency: Optional[str] = "USD"
    confidence: str = "low"
    missing_fields: List[str] = []


class DraftInvoice(BaseModel):
    draft_id: str
    data: InvoiceData
    gmail_message_id: str
    email_subject: Optional[str] = None
    zoho_contact_id: Optional[str] = None


class CreatedInvoice(BaseModel):
    zoho_invoice_id: str
    invoice_number: str
    client_name: str
    amount: float
    currency: str
    invoice_url: Optional[str] = None


class AmbiguousContact(BaseModel):
    name: str
    email: Optional[str]
    zoho_contact_id: str


# ── Outbound ─────────────────────────────────────────────────────────────────

class ChatResponse(BaseModel):
    reply: str
    action: str  # invoice_created | draft_pending | clarification_needed | emails_scanned | error
    drafts: Optional[List[DraftInvoice]] = None
    invoices_created: Optional[List[CreatedInvoice]] = None
    ambiguous_contacts: Optional[List[AmbiguousContact]] = None


class AuthStatus(BaseModel):
    gmail: bool
    zoho: bool
