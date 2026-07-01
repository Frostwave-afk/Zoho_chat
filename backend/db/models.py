from sqlalchemy import Column, String, BigInteger, Text, Numeric, Date
from backend.db.database import Base


class OAuthToken(Base):
    """Stores Gmail and Zoho OAuth tokens (Fernet-encrypted) for the single user."""
    __tablename__ = "oauth_tokens"

    service = Column(String(50), primary_key=True)   # "gmail" | "zoho"
    access_token = Column(Text, nullable=False)        # encrypted
    refresh_token = Column(Text, nullable=True)        # encrypted
    expires_at = Column(BigInteger, nullable=False)    # Unix timestamp


class ProcessedEmail(Base):
    """Records Gmail message IDs that have already produced an invoice (deduplication)."""
    __tablename__ = "processed_emails"

    gmail_message_id = Column(String(255), primary_key=True)
    zoho_invoice_id = Column(String(255), nullable=True)
    created_at = Column(BigInteger, nullable=False)


class ContactCache(Base):
    """Name → Zoho contact ID cache. TTL 24h to avoid hammering the Zoho Contacts API."""
    __tablename__ = "contact_cache"

    name_lower = Column(String(255), primary_key=True)    # e.g. "james carter"
    zoho_contact_id = Column(String(255), nullable=False)
    zoho_email = Column(String(255), nullable=True)
    cached_at = Column(BigInteger, nullable=False)         # Unix timestamp


class InvoiceCache(Base):
    """Zoho invoice snapshot for payment-status queries. Refreshed every 15 min."""
    __tablename__ = "invoice_cache"

    invoice_id = Column(String(255), primary_key=True)
    customer_name = Column(String(255), nullable=False)
    status = Column(String(50), nullable=False)
    due_date = Column(Date, nullable=True)
    balance = Column(Numeric(12, 2), nullable=False, default=0)
    total = Column(Numeric(12, 2), nullable=False, default=0)
    currency_code = Column(String(10), nullable=False, default="INR")
    zoho_view_url = Column(Text, nullable=True)
    last_synced = Column(BigInteger, nullable=False)
