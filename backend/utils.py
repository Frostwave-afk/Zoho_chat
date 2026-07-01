import hashlib
import base64
from cryptography.fernet import Fernet


def _make_fernet(secret_key: str) -> Fernet:
    """Derive a stable 32-byte Fernet key from SECRET_KEY."""
    raw = hashlib.sha256(secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(raw))


def encrypt_token(token: str, secret_key: str) -> str:
    return _make_fernet(secret_key).encrypt(token.encode()).decode()


def decrypt_token(encrypted: str, secret_key: str) -> str:
    return _make_fernet(secret_key).decrypt(encrypted.encode()).decode()


# ── Payment status formatting ────────────────────────────────────────────────

def _format_amount(amount: float, currency_code: str) -> str:
    if currency_code == "INR":
        return f"₹{amount:,.2f}"
    return f"{currency_code} {amount:,.2f}"


def format_payment_response(
    invoices: list[dict],
    *,
    title: str,
    empty_message: str,
    show_totals: bool = True,
) -> str:
    """Format a list of cached invoice dicts as chat text."""
    if not invoices:
        return empty_message

    lines = [title, ""]
    total_balance = 0.0
    currency = invoices[0].get("currency_code") or "INR"

    for inv in invoices:
        currency = inv.get("currency_code") or currency
        balance = float(inv.get("balance") or 0)
        total_balance += balance

        line = f"• **{inv['customer_name']}** — {_format_amount(balance, currency)}"
        if inv.get("status") == "overdue" and inv.get("days_overdue") is not None:
            days = inv["days_overdue"]
            line += f" ({days} day{'s' if days != 1 else ''} overdue)"
        elif inv.get("status") == "paid":
            line += " (paid)"
        elif inv.get("status") in ("sent", "partially_paid"):
            line += " (pending)"

        if inv.get("zoho_view_url"):
            line += f" · [View in Zoho]({inv['zoho_view_url']})"
        lines.append(line)

    if show_totals and total_balance > 0:
        lines.append("")
        lines.append(f"**Total:** {_format_amount(total_balance, currency)}")

    return "\n".join(lines)


def format_payment_summary(summary: dict) -> str:
    """Format aggregate payment stats as chat text."""
    currency = summary.get("currency_code") or "INR"
    return (
        "**Payment summary**\n\n"
        f"• Outstanding: **{_format_amount(summary['total_owed'], currency)}**\n"
        f"• Overdue: **{summary['overdue_count']}** invoice(s)\n"
        f"• Pending: **{summary['pending_count']}** invoice(s) awaiting payment\n"
        f"• Received: **{_format_amount(summary['total_received'], currency)}**\n"
        f"• Fully paid: **{summary['fully_paid_count']}** invoice(s)"
    )


def format_client_payment_response(client_name: str, invoices: list[dict]) -> str:
    """Format payment status for a specific client."""
    if not invoices:
        return (
            f"I couldn't find any invoices for **{client_name}** in Zoho. "
            "They may not have been invoiced yet."
        )

    paid = [i for i in invoices if i.get("status") == "paid"]
    overdue = [i for i in invoices if i.get("status") == "overdue"]
    pending = [
        i for i in invoices
        if i.get("status") in ("sent", "partially_paid") and float(i.get("balance") or 0) > 0
    ]

    if paid and not overdue and not pending:
        latest = paid[0]
        return (
            f"Yes — **{client_name}** has paid. "
            f"Latest invoice: {_format_amount(latest['total'], latest['currency_code'])} ✅"
        )

    if overdue:
        return format_payment_response(
            overdue,
            title=f"**{client_name}** has overdue invoice(s):",
            empty_message="",
            show_totals=True,
        )

    if pending:
        return format_payment_response(
            pending,
            title=f"**{client_name}** hasn't paid yet — pending invoice(s):",
            empty_message="",
            show_totals=True,
        )

    return format_payment_response(
        invoices,
        title=f"Invoices for **{client_name}**:",
        empty_message="",
        show_totals=False,
    )
