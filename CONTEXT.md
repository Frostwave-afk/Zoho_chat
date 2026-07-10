# Zoho Chat — Project Context

> **For AI assistants:** Read this entire file before touching any code. It covers architecture, every file's purpose, all design decisions, known gotchas, and how to run the project. Do not assume anything that isn't stated here.

---

## What This Project Does

A **single-user freelancer tool** that:
1. Reads Gmail for client emails requesting work/invoices
2. Uses an LLM to extract invoice data (client name, amount, description)
3. Creates invoices in Zoho Invoice via the Zoho API
4. Can send those invoices to clients by email
5. Answers natural-language questions about payment status (overdue, pending, who hasn't paid, etc.)
6. Can also create a brand-new invoice manually, without reading any emails, through a conversational flow or a single detailed message

The UI is a chat interface — the user types natural language, the backend parses intent and responds.

---

## How to Run

```bash
cd /Users/jash/Documents/Zoho_chat

# Clear processed emails (deduplication table) so emails can be re-scanned:
.venv/bin/python -c "
import asyncio
from backend.db.database import AsyncSessionLocal
from backend.db.models import ProcessedEmail
from sqlalchemy import delete

async def clear():
    async with AsyncSessionLocal() as session:
        result = await session.execute(delete(ProcessedEmail))
        await session.commit()
        print(f'Deleted {result.rowcount} records.')

asyncio.run(clear())
"

# Start the server (auto-reloads on file changes):
.venv/bin/uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000` in the browser.

---

## Telegram Bot

A Telegram bot frontend that provides the same features as the web UI.
File: `telegram_bot.py` (project root).

### How to Run

```bash
# Terminal 1 — FastAPI server (still needed for Gmail/Zoho OAuth callbacks)
.venv/bin/uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000

# Terminal 2 — Telegram bot (polling mode)
.venv/bin/python telegram_bot.py
```

### Setup Steps

1. Create a bot via **@BotFather** on Telegram → it gives you a token.
2. Add to `.env`:
   ```
   TELEGRAM_BOT_TOKEN=7412345678:AAHd...your_token_here
   TELEGRAM_ALLOWED_USER_ID=   # leave blank first, then see step 3
   ```
3. Run the bot and send `/myid` — it replies with your numeric Telegram user ID.
4. Add that ID to `.env` as `TELEGRAM_ALLOWED_USER_ID=123456789` and restart.

### How it Connects to the Backend

`telegram_bot.py` **imports and calls Python functions directly** — no HTTP:
```python
from backend.services.pipeline import process_chat, approve_draft, approve_batch
```
It shares the same PostgreSQL DB, OAuth tokens, and all service logic.

### Feature Mapping

| Feature | Telegram |
|---|---|
| Natural language chat | Regular text message |
| Status updates ("Analyzing…") | Edited interim message |
| Draft card | Formatted message + inline buttons |
| Approve / Create & Send | Inline button tap |
| Batch card | Selectable item list + compact inline buttons |
| Manual invoice draft | Formatted message + inline approve/send buttons |
| Payment tables | Formatted text with emoji |
| Auth connect | `/status` command shows clickable OAuth URLs |

### Bot Commands

| Command | Action |
|---|---|
| `/start` | Welcome message + usage examples |
| `/status` | Shows Gmail/Zoho connection status + connect links |
| `/get_mails` | Starts a timeframe prompt, then scans Gmail for that timeframe |
| `/payment_status` | Shows overall payment summary / unpaid status |
| `/payment_status_of` | Starts a client-name prompt, then checks one client's payment status |
| `/cancel` | Cancels a pending Telegram command prompt |
| `/myid` | Prints your numeric Telegram user ID |

### Telegram UX Notes

- Telegram slash commands are implemented as **two-step prompts** when they need extra input.
  Example: tapping `/get_mails` sends the command immediately, so the bot then asks for the timeframe
  (`today`, `yesterday`, `monday`, `last week`, etc.) in the next message.
- `telegram_bot.py` does **not** hot reload. Restart it manually after code changes:
  ```bash
  .venv/bin/python telegram_bot.py
  ```
- Telegram startup depends on reaching `api.telegram.org`. A timeout during startup is usually a
  network / DNS / proxy / VPN / firewall problem, not an application-logic problem.

### Security

`TELEGRAM_ALLOWED_USER_ID` gates all message handlers. If blank, the bot
responds to everyone (dev mode only). Always set it before sharing the bot.

---

## Environment Variables (`.env`)

```
GEMINI_API_KEY=...         # Not actively used (Groq replaced Gemini for LLM calls)
GOOGLE_CLIENT_ID=...       # Gmail OAuth
GOOGLE_CLIENT_SECRET=...
GROQ_API_KEY=...           # Used for ALL LLM calls (intent parsing + invoice extraction)
ZOHO_CLIENT_ID=...
ZOHO_CLIENT_SECRET=...
ZOHO_REGION=in             # Determines API base URLs (in = India region)
DATABASE_URL=postgresql+asyncpg://postgres:Sherbet-lemon1@[2406:da18:167b:f900:5ee6:2912:e8c4:ffce]:5432/postgres
SECRET_KEY=...             # Used to Fernet-encrypt OAuth tokens in DB
```

**Important:** Despite the file being named `gemini_service.py`, it calls **Groq (Llama 3.3 70B)** — not Gemini. The file was named before the switch.

---

## Tech Stack

| Layer | Tech |
|---|---|
| Backend | FastAPI + Python 3.14 |
| LLM | Groq API — `llama-3.3-70b-versatile` |
| Gmail | Google API Python Client (OAuth 2.0) |
| Invoicing | Zoho Invoice API v3 |
| DB | PostgreSQL (async via asyncpg + SQLAlchemy) |
| Frontend | Vanilla HTML/CSS/JS (no framework) |
| Virtual env | `.venv` at project root |

---

## Project Structure

```
Zoho_chat/
├── .env
├── requirements.txt
├── frontend/
│   ├── index.html
│   ├── style.css
│   └── app.js
└── backend/
    ├── main.py               # FastAPI app, lifespan, static file serving
    ├── config.py             # Pydantic settings loaded from .env
    ├── schemas.py            # All Pydantic request/response models
    ├── utils.py              # Token encryption + payment response formatters
    ├── auth/
    │   ├── gmail_auth.py     # Gmail OAuth flow + token refresh
    │   └── zoho_auth.py      # Zoho OAuth flow + token refresh
    ├── db/
    │   ├── database.py       # AsyncEngine, AsyncSessionLocal, Base, init_db
    │   └── models.py         # SQLAlchemy ORM models (4 tables)
    ├── routers/
    │   ├── auth_router.py    # /auth/* endpoints (connect Gmail/Zoho, status)
    │   └── chat_router.py    # POST /chat, /chat/approve, /chat/batch-approve, /chat/manual-approve
    └── services/
        ├── groq_service.py   # Intent parsing + manual invoice extraction
        ├── gemini_service.py # Invoice data extraction from email text (also uses Groq)
        ├── gmail_service.py  # Gmail search with date/person/keyword filters
        ├── pipeline.py       # Main orchestration: intent → Gmail/manual flow → Zoho → response
        ├── zoho_service.py   # Zoho Invoice CRUD (contacts, invoices, send email)
        └── zoho_payments.py  # Payment status queries (overdue/pending/summary)
```

---

## Database Tables

### `oauth_tokens`
Stores Fernet-encrypted OAuth tokens for the single user.
- `service` (PK): `"gmail"` | `"zoho"`
- `access_token`, `refresh_token`: encrypted strings
- `expires_at`: Unix timestamp — auto-refreshed when < 60s remaining

### `processed_emails`
Deduplication table. A Gmail message ID is recorded here once an invoice is created from it.
- `gmail_message_id` (PK)
- `zoho_invoice_id`
- `created_at`

**To re-process emails, delete rows from this table** (see run command above).

### `contact_cache`
Caches Zoho contact lookups (name → contact_id + email). TTL = 24 hours.
- `name_lower` (PK): lowercased name
- `zoho_contact_id`, `zoho_email`, `cached_at`

### `invoice_cache`
Snapshot of Zoho invoices for payment-status queries and dashboard stats. Refreshed on demand (15 minutes TTL) or instantly upon write.
- `invoice_id` (PK)
- `customer_name`, `status`, `due_date`, `invoice_date`, `last_payment_date`, `balance`, `total`, `currency_code`
- `zoho_view_url`, `last_synced`, `last_reminded_at`
- Statuses synced: `overdue`, `sent`, `partially_paid`, `paid`, `unpaid`

### `recurring_cache`
Snapshot of active recurring invoice profiles to drive dashboard KPI metrics and lists without querying the live Zoho API on every view.
- `profile_id` (PK)
- `customer_name`, `status`, `amount`, `last_synced`

---

## Zoho Configuration

- **Region:** `in` (India) → API base: `https://www.zohoapis.in/invoice/v3`
- **Org ID:** `60074311393` (hardcoded as `_ZOHO_ORG_ID` in `zoho_service.py` and `zoho_payments.py`)
- **Required OAuth scopes:** `ZohoInvoice.invoices.CREATE`, `ZohoInvoice.invoices.READ`, `ZohoInvoice.contacts.READ`, `ZohoInvoice.contacts.CREATE`, `ZohoInvoice.settings.READ`

> **Critical:** The `/invoices/{id}/email` endpoint **requires** `organization_id` as a query param or it returns 400. Always pass `params={"organization_id": _ZOHO_ORG_ID}`.

---

## Intent Actions (groq_service.py)

The intent parser returns one of these actions:

| Action | Trigger |
|---|---|
| `create_invoice` | "make an invoice for Rahul" |
| `scan_emails` | "check emails from Piyusha yesterday" |
| `send_invoices` | "send the invoice", "send it to Vismay", "send all invoices" |
| `check_overdue` | "show overdue invoices", "past due" |
| `check_pending` | "who hasn't paid?", "unpaid invoices", "outstanding" |
| `check_specific_payment` | "did Rahul pay?", "has Piyusha paid?" |
| `payment_summary` | "payment summary", "how much am I owed?" |
| `approve_draft` | "yes", "create it", "looks good" |
| `decline_draft` | "no", "cancel", "skip" |
| `greeting` | "hi", "thanks", "hello" |
| `unknown` | anything unclear |

Some common payment phrases are **short-circuited before hitting the LLM** (in `groq_service.py`) to avoid misclassification:
- "overdue" / "past due" → always `check_overdue`
- "who hasn't paid" / "unpaid" / "outstanding" → always `check_pending`

For **manual invoice creation**, the backend treats clearly explicit phrases like:
- "create a new invoice"
- "manual invoice"
- "from scratch"
- "without email"

as the trigger for the manual invoice conversation, so the older email-reading invoice flow still works.

---

## Pipeline Flow (`pipeline.py`)

```
process_chat(message)
    │
    ├── parse_intent() → action
    │
    ├── action == "greeting" → friendly help message
    ├── action == "unknown" → clarification prompt
    │
    ├── action == "send_invoices"
    │       → filter _recent_invoices (in-memory deque, maxlen=20)
    │       → smart: "send all" vs "send Vismay's" vs "send the invoice" (latest unsent)
    │       → send_invoice_email(invoice_id, db, to_email=client_email)
    │
    ├── action in payment queries → _handle_payment_query()
    │       → ensure_fresh_cache() (syncs Zoho if cache > 15min old)
    │       → query invoice_cache table
    │       → format with utils.py formatters
    │
    ├── action == "create_invoice" + explicit manual phrasing
    │       → optional one-shot parse via extract_manual_invoice_request()
    │       → otherwise start manual invoice conversation state machine
    │       → resolve existing customer or mark for new-contact creation
    │       → collect line items + send choice
    │       → return ManualInvoiceDraft preview
    │
    └── action in ("create_invoice", "scan_emails")
            ├── search_contact_by_name() in Zoho (with 24h cache)
            ├── search_gmail() with person_email/date_filter/keywords
            ├── For each email:
            │       extract_invoice_data() → LLM extracts structured data
            │       skip if: not is_confirmation AND low confidence AND no amount AND no task_description AND no client_name
            │       resolve contact_id or mark is_new=True
            │       fallback: infer client_email from From/To headers if not in body
            │       fallback: infer client_name from header display names if possible
            │       fallback: resolve contact by recipient email if name is missing
            │       reconcile drafts with the same resolved client email before batching
            │                 remove "client_email" from missing_fields after inference
            │       queue as DraftInvoice (always — never auto-creates)
            │       group same-contact drafts into a BatchDraft where possible
            └── return ChatResponse with drafts / batch draft / manual draft
```

### approve_draft()
```
approve_draft(draft_id, overrides, db)
    ├── Apply field overrides (amount, description, etc.)
    ├── If no zoho_contact_id → create_contact() first
    ├── create_invoice() in Zoho
    ├── mark_email_processed() in DB
    ├── Silently update_contact_email() on Zoho contact (background task)
    ├── If send_email=True → send_invoice_email(invoice_id, db, to_email=client_email)
    ├── Append to _recent_invoices deque
    └── Return reply with invoice number
```

### approve_batch()
```
approve_batch(batch_draft_id, mode, selected_item_ids, send_email, db)
    ├── Load pending BatchDraft
    ├── Either:
    │   • create separate invoices, or
    │   • create one combined multi-line invoice
    ├── Optionally send invoice email(s)
    ├── Append created invoice(s) to _recent_invoices
    └── Return reply + created invoice cards
```

### approve_manual_invoice()
```
approve_manual_invoice(draft_id, send_email, db)
    ├── Load pending ManualInvoiceDraft
    ├── If needed, create a new Zoho contact first
    ├── create_invoice() with multiple line_items
    ├── Optionally send invoice email
    ├── Append to _recent_invoices
    └── Return reply + created invoice card
```

---

## In-Memory State (lost on server restart)

- `_pending_drafts: dict[str, DraftInvoice]` — draft cards awaiting user approval
- `_pending_batches: dict[str, BatchDraft]` — grouped drafts for same-contact batch processing
- `_pending_manual_invoice_drafts: dict[str, ManualInvoiceDraft]` — manual invoice previews awaiting approval
- `_pending_estimate_drafts: dict[str, EstimateDraft]` — manual estimate previews awaiting approval
- `_manual_invoice_conversation: Optional[ManualInvoiceConversation]` — active manual invoice/estimate chat flow
- `_pending_recurring_conv: dict[str | int, RecurringConversation]` — active recurring invoice chat flow
- `_pending_recurring_list: dict[str | int, list[dict]]` — active recurring profiles cache for stop-by-number
- `_pending_estimate_disambiguations: dict[str | int, dict]` — matching accepted estimates for selection
- `_recent_invoices: deque[CreatedInvoice]` (maxlen=20) — invoices created this session, used for "send the invoice" intent

---

## LLM Usage

### `groq_service.py` — Intent parsing
- Model: `llama-3.3-70b-versatile`
- Temperature: 0, max_tokens: 150
- Returns JSON with `action`, `person_name`, `date_filter`, `keywords`

### `groq_service.py` — Manual invoice extraction
- Model: `llama-3.1-8b-instant`
- Used when the user gives a one-shot manual invoice request
- Returns JSON with `client_name`, `client_email`, `currency`, `send_email`, and `items[]`

### `gemini_service.py` — Invoice data extraction
- Model: `llama-3.3-70b-versatile` (same Groq client, despite the filename)
- Temperature: 0, max_tokens: 400
- Returns JSON with `is_confirmation`, `client_name`, `client_email`, `item_name`, `task_description`, `amount`, `currency`, `confidence`, `missing_fields`
- `confidence` default in schema is `"high"` — LLM only sets `"low"` for genuinely ambiguous emails
- **Skip condition:** only skip if `not is_confirmation AND confidence=="low" AND not amount AND not task_description AND not client_name` (all must be true)

---

## Gmail Search (`gmail_service.py`)

- Uses Google API Python Client with stored OAuth tokens
- Builds queries like `from:email@example.com after:2024/06/15 before:2024/06/16`
- Supports `date_filter` values: `today`, `yesterday`, `this_week`, `last_week`, `last_monday` through `last_sunday`, `this_monday` through `this_sunday`
- Falls back to name-keyword search if no email known for person
- Returns email metadata + decoded body text

---

## Frontend (`frontend/`)

Single-page chat app. No framework, no build step.

- `index.html` — shell, loads CSS and JS
- `style.css` — dark theme, glassmorphism, chat bubbles, draft cards, invoice cards
- `app.js` — all logic:
  - Sends messages to `POST /chat`
  - Renders manual invoice preview cards + approve/send actions
  - Renders draft cards with editable fields + two approve buttons:
    - **"✓ Create Invoice"** → `approveDraft(id, sendEmail=false)`
    - **"📧 Create & Send Invoice"** → `approveDraft(id, sendEmail=true)`
  - Renders created invoice cards with **"Send to Client"** button → `sendInvoice(invoiceId)`
  - Renders payment invoice cards for overdue/pending results
  - Low-confidence warning only shown when `confidence === 'low'` AND `amount` or `task_description` is missing (not just any missing field)

---

## Key Quirks & Gotchas

1. **`gemini_service.py` uses Groq, not Gemini.** The filename is misleading — `GEMINI_API_KEY` in `.env` is unused.

2. **Zoho email send requires `organization_id`** as a query param (`60074311393`). Without it → HTTP 400.

3. **Zoho contacts may have no email on record.** Always pass `to_email=data.client_email` explicitly to `send_invoice_email()` so Zoho knows where to send even if the contact record has no email. After approval, `update_contact_email()` patches the contact in the background.

4. **`_recent_invoices` is in-memory.** "Send the invoice" intent only works within the same server session. Restarting the server clears it.

5. **Draft approval is also in-memory.** `_pending_drafts` is cleared on restart — stale draft IDs from the frontend will get an "expired" error.

6. **Batch, manual invoice, and payment record previews are also in-memory.** `_pending_batches`, `_pending_manual_invoice_drafts`, `_pending_payment_drafts`, and `_manual_invoice_conversation` are all lost on restart.

7. **Email deduplication.** Once a Gmail message ID is in `processed_emails`, it won't produce a draft again. Clear the table to re-process (see run command).

8. **Client email inference from headers.** If the email body doesn't contain the client's email address (e.g. "send to this email" with no actual address), the pipeline extracts it from the correct header (`From:` for client-sent emails, `To:` for self-sent emails). It also removes `client_email` from `missing_fields` after successful inference so the draft card shows no warning.

9. **Receiver/contact fallback is multi-step now.** When the extractor misses the client name, the pipeline also tries header display names, Zoho lookup by email, and a second reconciliation pass across drafts with the same recipient email before batching.

10. **Invoice cache TTL = 15 minutes.** Payment queries auto-refresh when stale. First query after a long idle period will be slower (Zoho API call).

11. **Rate limiting.** A 1.5-second sleep between consecutive Groq calls when processing multiple emails. If rate-limited, the error is surfaced immediately as a chat reply.

12. **Contact cache TTL = 24 hours.** Zoho contact lookups are cached by lowercase name. Email-based Zoho lookup also checks cached rows by `zoho_email` before hitting the API.

13. **Payment reminder endpoint quirks.** The bulk reminder endpoint is `POST /invoices/paymentreminder` (not `/bulk_invoice_reminder`). Invoice IDs are passed as a comma-separated **query parameter** `invoice_ids` (not a JSON body). Max 10 IDs per call — callers must chunk. The endpoint also requires `organization_id` as a query param AND the `X-com-zoho-invoice-organizationid` header (same dual-send pattern as gotcha #2). Partial failures are reported in `response["info"]["email_errors_info"]` as a list of `{ids, message}` — only update `last_reminded_at` for IDs **not** present in this error list.

14. **Customer payments endpoint (/customerpayments)**: When recording manual payments via the API, the standard Zoho `payment_mode` enum includes `cash`, `check`, `creditcard`, `banktransfer`, `bankremittance`, `others`. Because `upi` is not natively supported as a manual payment_mode string, it is mapped to `"banktransfer"` in Zoho. All balance arithmetic updates must be rounded to 2 decimal places to avoid floating point precision issues.

15. **Estimates endpoint quirks**: Estimates v3 endpoints (including `GET /estimates`, `GET /estimates/{id}`, `POST /estimates`, and `POST /estimates/{id}/email`) require the `organization_id` query parameter on every request. Status filtering for listing estimates uses `filter_by=Status.Accepted` (not `status=accepted`). When an estimate has been successfully converted into an invoice, Zoho automatically sets its status field to `"invoiced"`.

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Serves `frontend/index.html` |
| `GET` | `/auth/status` | Returns `{gmail: bool, zoho: bool}` |
| `GET` | `/auth/gmail` | Redirects to Gmail OAuth |
| `GET` | `/auth/gmail/callback` | Handles Gmail OAuth callback |
| `GET` | `/auth/zoho` | Redirects to Zoho OAuth |
| `GET` | `/auth/zoho/callback` | Handles Zoho OAuth callback |
| `POST` | `/chat` | Main chat endpoint — `{message: str}` → `ChatResponse` |
| `POST` | `/chat/approve` | Approve a draft → `ApproveRequest` → `ChatResponse` |
| `POST` | `/chat/batch-approve` | Approve a batch draft |
| `POST` | `/chat/manual-approve` | Approve a manual invoice draft |
| `POST` | `/chat/estimate-approve` | Approve a manual estimate draft |
| `POST` | `/chat/payment-approve` | Approve a manual payment record draft |
| `GET` | `/api/invoices` | Return all invoices, optionally filtered by status |
| `GET` | `/api/invoices/recurring` | Return all active recurring invoices |
| `POST` | `/api/invoices/send` | Send an existing invoice to the client via Zoho email API |
| `POST` | `/api/estimates/send` | Send an existing estimate to the client via Zoho email API |
| `GET` | `/api/stats` | Return invoice statistics, revenue history, and customer breakdown |

---

## Schemas Quick Reference (`schemas.py`)

```python
# Inbound
ChatRequest(message: str)
ApproveRequest(draft_id, item_name?, task_description?, amount?, currency?,
               client_name?, client_email?, send_email: bool = False)
BatchApproveRequest(batch_draft_id, mode, selected_item_ids[], send_email=False)
ManualInvoiceApproveRequest(draft_id, send_email: bool = False, client_name?, client_email?, line_items?)

# Internal
InvoiceData(is_confirmation, client_name?, client_email?, item_name?,
            task_description?, amount?, currency="USD", confidence="high",
            missing_fields=[])

DraftInvoice(draft_id, data: InvoiceData, gmail_message_id, email_subject?,
             zoho_contact_id?, is_new_contact=False)

BatchDraft(batch_id, client_name, client_email?, zoho_contact_id, items[])

ManualInvoiceLineItem(item_name, task_description, amount)

ManualInvoiceDraft(draft_id, client_name, client_email?, currency="USD",
                   zoho_contact_id?, is_new_contact=False, line_items[])

RecurringConversation(session_key, step, client_name?, client_email?, item_name?,
                      amount?, currency?, frequency?, start_date?, zoho_contact_id?)

CreatedInvoice(zoho_invoice_id, invoice_number, client_name, client_email?,
               amount, currency, invoice_url?, email_sent=False)

PaymentInvoice(invoice_id, customer_name, status, due_date?, balance,
               currency_code="INR", zoho_view_url?, days_overdue?)

# Outbound
ChatResponse(reply, action, drafts?, batch_draft?, manual_invoice_draft?,
             invoices_created?, payment_invoices?, ambiguous_contacts?, recurring_draft?)
# action values: invoice_created | draft_pending | batch_pending |
#                manual_invoice_pending | payment_status |
#                invoice_sent | emails_scanned | clarification_needed | error
```
