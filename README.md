# 🧾 Zoho Chat — AI-Powered Invoice Assistant

> A conversational invoicing tool that reads your Gmail, extracts invoice data using an LLM, and creates/sends invoices via Zoho Invoice — all through a simple chat interface.

---

## ✨ What It Does

Type naturally. The assistant handles the rest.

- 📬 **Reads Gmail** for client emails requesting work or invoices
- 🤖 **Uses an LLM** (Groq / Llama 3.3 70B) to extract invoice data — client name, amount, description
- 📄 **Creates invoices** in Zoho Invoice via the Zoho API v3
- 📧 **Sends invoices** to clients directly by email
- 💬 **Answers questions** about payment status — overdue, pending, who hasn't paid, etc.
- ✍️ **Manual invoice creation** — through a conversational flow or a single detailed message, no email needed

The UI is a minimal chat interface — type in natural language, get results.

---

## 🖥️ Frontend Interfaces

### Web UI
A single-page chat app with a dark glassmorphism design. No framework, no build step.

### Telegram Bot
The same features, available over Telegram. The bot imports backend Python functions directly (no HTTP between them).

---

## 🏗️ Tech Stack

| Layer | Technology |
|---|---|
| Backend | FastAPI + Python 3.14 |
| LLM | Groq API — `llama-3.3-70b-versatile` |
| Gmail | Google API Python Client (OAuth 2.0) |
| Invoicing | Zoho Invoice API v3 |
| Database | PostgreSQL (async via asyncpg + SQLAlchemy) |
| Frontend | Vanilla HTML / CSS / JS |
| Bot | python-telegram-bot |

---

## 📁 Project Structure

```
Zoho_chat/
├── .env                          # Secrets & config (see below)
├── requirements.txt
├── telegram_bot.py               # Telegram bot frontend
├── frontend/
│   ├── index.html                # Shell
│   ├── style.css                 # Dark theme, glassmorphism, chat bubbles
│   └── app.js                    # All frontend logic
└── backend/
    ├── main.py                   # FastAPI app, lifespan, static file serving
    ├── config.py                 # Pydantic settings loaded from .env
    ├── schemas.py                # All Pydantic request/response models
    ├── utils.py                  # Token encryption + payment response formatters
    ├── auth/
    │   ├── gmail_auth.py         # Gmail OAuth flow + token refresh
    │   └── zoho_auth.py          # Zoho OAuth flow + token refresh
    ├── db/
    │   ├── database.py           # AsyncEngine, AsyncSessionLocal, Base, init_db
    │   └── models.py             # SQLAlchemy ORM models
    ├── routers/
    │   ├── auth_router.py        # /auth/* endpoints
    │   └── chat_router.py        # /chat and approval endpoints
    └── services/
        ├── groq_service.py       # Intent parsing + manual invoice extraction
        ├── gemini_service.py     # Invoice data extraction from email (also uses Groq)
        ├── gmail_service.py      # Gmail search with date/person/keyword filters
        ├── pipeline.py           # Main orchestration: intent → Gmail/manual → Zoho
        ├── zoho_service.py       # Zoho Invoice CRUD (contacts, invoices, send email)
        └── zoho_payments.py      # Payment status queries (overdue/pending/summary)
```

---

## ⚙️ Setup

### 1. Clone & Create Virtual Environment

```bash
git clone <your-repo-url>
cd Zoho_chat
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure Environment Variables

Create a `.env` file in the project root:

```env
# Google / Gmail OAuth
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...

# Groq (used for ALL LLM calls)
GROQ_API_KEY=...

# Zoho OAuth
ZOHO_CLIENT_ID=...
ZOHO_CLIENT_SECRET=...
ZOHO_REGION=in           # "in" for India region, "com" for global

# PostgreSQL
DATABASE_URL=postgresql+asyncpg://user:password@host:5432/dbname

# Token encryption key
SECRET_KEY=...           # Any strong random string

# Telegram (optional)
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USER_ID=  # Your Telegram numeric user ID
```

> **Note:** `GEMINI_API_KEY` is not used. The file `gemini_service.py` was named before the LLM was switched to Groq — it still calls Groq internally.

### 3. Set Up OAuth

**Gmail:** Go to [Google Cloud Console](https://console.cloud.google.com), create an OAuth 2.0 Client ID, and add `http://127.0.0.1:8000/auth/gmail/callback` as a redirect URI.

**Zoho:** Go to [Zoho API Console](https://api-console.zoho.in), create a Server-based application, and add `http://127.0.0.1:8000/auth/zoho/callback` as the redirect URI. Required scopes:
- `ZohoInvoice.invoices.CREATE`
- `ZohoInvoice.invoices.READ`
- `ZohoInvoice.contacts.READ`
- `ZohoInvoice.contacts.CREATE`
- `ZohoInvoice.settings.READ`

---

## 🚀 Running the App

### Web UI

```bash
.venv/bin/uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000` in your browser. On first run, visit `/auth/status` to connect Gmail and Zoho.

### Telegram Bot

```bash
# Terminal 1 — FastAPI server (still needed for OAuth callbacks)
.venv/bin/uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000

# Terminal 2 — Telegram bot (polling mode)
.venv/bin/python telegram_bot.py
```

**Telegram setup steps:**
1. Create a bot via **@BotFather** → get your token
2. Add `TELEGRAM_BOT_TOKEN` to `.env`
3. Run the bot and send `/myid` to get your numeric user ID
4. Add that ID as `TELEGRAM_ALLOWED_USER_ID` in `.env` and restart

---

## 💬 Usage Examples

| What you type | What happens |
|---|---|
| `"make an invoice for Rahul for ₹5000"` | Creates a draft invoice |
| `"check emails from Piyusha yesterday"` | Scans Gmail and extracts invoice data |
| `"send the invoice to Vismay"` | Sends a previously created invoice |
| `"who hasn't paid?"` | Lists unpaid/pending invoices |
| `"show overdue invoices"` | Lists overdue invoices |
| `"did Rahul pay?"` | Checks a specific client's payment status |
| `"how much am I owed?"` | Full payment summary |
| `"create a new invoice from scratch"` | Starts a manual invoice conversation |

### Telegram Bot Commands

| Command | Description |
|---|---|
| `/start` | Welcome message + usage examples |
| `/status` | Gmail/Zoho connection status + connect links |
| `/get_mails` | Scan Gmail for a given timeframe |
| `/payment_status` | Overall payment summary |
| `/payment_status_of` | Check one client's payment status |
| `/cancel` | Cancel a pending prompt |
| `/myid` | Print your Telegram user ID |

---

## 🗄️ Database Tables

| Table | Purpose |
|---|---|
| `oauth_tokens` | Fernet-encrypted Gmail & Zoho OAuth tokens |
| `processed_emails` | Deduplication — prevents re-creating invoices from the same email |
| `contact_cache` | Zoho contact lookups cached by name (24h TTL) |
| `invoice_cache` | Zoho invoice snapshot for fast payment queries (15 min TTL) |
| `recurring_cache` | Snapshot of active recurring invoice profiles |

### Re-Processing Emails

If you need to re-scan emails that have already been processed:

```bash
.venv/bin/python clear_processed_emails.py
```

---

## 🔌 API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Serves the chat UI |
| `GET` | `/auth/status` | `{gmail: bool, zoho: bool}` |
| `GET` | `/auth/gmail` | Redirect to Gmail OAuth |
| `GET` | `/auth/gmail/callback` | Gmail OAuth callback |
| `GET` | `/auth/zoho` | Redirect to Zoho OAuth |
| `GET` | `/auth/zoho/callback` | Zoho OAuth callback |
| `POST` | `/chat` | Main chat — `{message: str}` |
| `POST` | `/chat/approve` | Approve a draft invoice |
| `POST` | `/chat/batch-approve` | Approve a batch of drafts |
| `POST` | `/chat/manual-approve` | Approve a manual invoice draft |
| `POST` | `/chat/estimate-approve` | Approve a manual estimate draft |
| `POST` | `/chat/payment-approve` | Record a manual payment |
| `GET` | `/api/invoices` | List invoices (optional status filter) |
| `GET` | `/api/invoices/recurring` | List active recurring invoices |
| `POST` | `/api/invoices/send` | Send an invoice via Zoho email |
| `POST` | `/api/estimates/send` | Send an estimate via Zoho email |
| `GET` | `/api/stats` | Invoice stats, revenue history, customer breakdown |

---

## ⚠️ Known Gotchas

1. **`gemini_service.py` uses Groq, not Gemini.** The filename is historical — `GEMINI_API_KEY` is unused.
2. **Zoho email send requires `organization_id`** as a query param. Without it → HTTP 400.
3. **In-memory state is lost on restart.** Pending drafts, conversations, and the recent invoices list are cleared when the server restarts.
4. **Email deduplication.** Once a Gmail message ID is in `processed_emails`, it won't produce a draft again — clear the table to re-process.
5. **Invoice cache TTL = 15 minutes.** The first payment query after a long idle period will be slower (hits Zoho API).
6. **Telegram bot does not hot-reload.** Restart it manually after any code changes.
7. **Telegram startup requires `api.telegram.org` to be reachable.** A timeout at startup is a network/VPN issue, not an app bug.
8. **UPI is not a native Zoho payment mode.** It's mapped to `banktransfer` internally.

---

## 📦 Dependencies

```
fastapi                     # Web framework
uvicorn[standard]           # ASGI server
python-dotenv               # .env loading
pydantic-settings           # Typed config from env
sqlalchemy[asyncio]         # ORM (async)
asyncpg                     # Async PostgreSQL driver
httpx                       # Async HTTP client
aiofiles                    # Async file I/O
google-auth                 # Google OAuth
google-auth-oauthlib        # OAuth flow helpers
google-api-python-client    # Gmail API
groq                        # Groq LLM API client
cryptography                # Fernet token encryption
python-telegram-bot         # Telegram bot framework
```

---

## 📝 License

Personal freelancer tool. All rights reserved.
