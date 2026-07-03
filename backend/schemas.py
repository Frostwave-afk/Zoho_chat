from pydantic import BaseModel
from typing import Optional, List, Literal


# ── Inbound ─────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str


class ApproveRequest(BaseModel):
    draft_id: str
    item_name: Optional[str] = None
    task_description: Optional[str] = None
    amount: Optional[float] = None
    currency: Optional[str] = None
    client_name: Optional[str] = None
    client_email: Optional[str] = None
    send_email: bool = False          # if True → fire /invoices/{id}/email after creation


class BatchApproveRequest(BaseModel):
    batch_draft_id: str
    mode: Literal["separate", "combined"]  # separate = N invoices; combined = 1 multi-line invoice
    selected_item_ids: List[str]           # which BatchDraftItems to include
    send_email: bool = False               # if True → email the invoice(s) after creation


class SendInvoiceRequest(BaseModel):
    invoice_id: str                   # Zoho invoice ID to email


class ManualInvoiceApproveRequest(BaseModel):
    draft_id: str
    send_email: bool = False
    # Optional overrides from the editable card
    client_name:  Optional[str] = None
    client_email: Optional[str] = None
    line_items:   Optional[List["ManualInvoiceLineItem"]] = None


# ── Internal data shapes ─────────────────────────────────────────────────────

class InvoiceData(BaseModel):
    is_confirmation: bool = False
    client_name: Optional[str] = None
    client_email: Optional[str] = None
    item_name: Optional[str] = None
    task_description: Optional[str] = None
    amount: Optional[float] = None
    currency: Optional[str] = "USD"
    confidence: str = "high"
    missing_fields: List[str] = []


class DraftInvoice(BaseModel):
    draft_id: str
    data: InvoiceData
    gmail_message_id: str
    email_subject: Optional[str] = None
    zoho_contact_id: Optional[str] = None
    is_new_contact: bool = False  # True when the contact doesn't exist in Zoho yet


class BatchDraftItem(BaseModel):
    """A single invoice candidate inside a batch (same client, multiple emails)."""
    item_id: str
    data: InvoiceData
    gmail_message_id: str
    email_subject: Optional[str] = None


class BatchDraft(BaseModel):
    """Groups multiple invoice candidates for the same existing Zoho contact."""
    batch_id: str
    client_name: str
    client_email: Optional[str] = None
    zoho_contact_id: str
    items: List[BatchDraftItem]


class ManualInvoiceLineItem(BaseModel):
    item_name: str
    task_description: str
    amount: float


class ManualInvoiceDraft(BaseModel):
    draft_id: str
    client_name: str
    client_email: Optional[str] = None
    currency: str = "USD"
    zoho_contact_id: Optional[str] = None
    is_new_contact: bool = False
    line_items: List[ManualInvoiceLineItem]


class ManualInvoiceConversation(BaseModel):
    step: str
    client_name: Optional[str] = None
    client_email: Optional[str] = None
    currency: str = "USD"
    zoho_contact_id: Optional[str] = None
    is_new_contact: bool = False
    item_count: int = 0
    line_items: List[ManualInvoiceLineItem] = []
    pending_item_name: Optional[str] = None
    pending_item_description: Optional[str] = None
    send_email: Optional[bool] = None


class CreatedInvoice(BaseModel):
    zoho_invoice_id: str
    invoice_number: str
    client_name: str
    client_email: Optional[str] = None
    amount: float
    currency: str
    invoice_url: Optional[str] = None
    email_sent: bool = False


class AmbiguousContact(BaseModel):
    name: str
    email: Optional[str]
    zoho_contact_id: str


class PaymentInvoice(BaseModel):
    invoice_id: str
    customer_name: str
    status: str
    due_date: Optional[str] = None
    balance: float
    currency_code: str = "INR"
    zoho_view_url: Optional[str] = None
    days_overdue: Optional[int] = None


# ── Outbound ─────────────────────────────────────────────────────────────────

class ChatResponse(BaseModel):
    reply: str
    action: str  # invoice_created | draft_pending | batch_pending | manual_invoice_pending | payment_status | clarification_needed | emails_scanned | error
    drafts: Optional[List[DraftInvoice]] = None
    batch_draft: Optional[BatchDraft] = None
    manual_invoice_draft: Optional[ManualInvoiceDraft] = None
    recurring_draft: Optional[RecurringConversation] = None
    invoices_created: Optional[List[CreatedInvoice]] = None
    payment_invoices: Optional[List[PaymentInvoice]] = None
    ambiguous_contacts: Optional[List[AmbiguousContact]] = None



class RecurringConversation(BaseModel):
    """Tracks state for the multi-step recurring invoice creation conversation."""
    step: str = "client"           # client → amount → frequency → start_date → confirm
    client_name: Optional[str] = None
    client_email: Optional[str] = None
    zoho_contact_id: Optional[str] = None
    is_new_contact: bool = False
    item_name: Optional[str] = None
    task_description: Optional[str] = None
    amount: Optional[float] = None
    currency: str = "INR"
    frequency: Optional[str] = None   # monthly | weekly | yearly | daily
    start_date: Optional[str] = None  # YYYY-MM-DD
    end_date: Optional[str] = None    # YYYY-MM-DD or None = no end


class RecurringInvoiceInfo(BaseModel):
    """A single active recurring invoice returned from Zoho."""
    recurring_invoice_id: str
    recurrence_name: str
    customer_name: str
    amount: float
    currency_code: str = "INR"
    recurrence_frequency: str   # monthly | weekly | yearly
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    status: str = "active"


class AuthStatus(BaseModel):
    gmail: bool
    zoho: bool
