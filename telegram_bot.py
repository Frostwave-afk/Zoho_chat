"""
Telegram bot frontend for the Zoho Invoice Agent.

Run alongside the FastAPI server (needed for OAuth callbacks):
    Terminal 1: uvicorn backend.main:app --reload
    Terminal 2: .venv/bin/python telegram_bot.py

The bot reuses all existing pipeline/service code directly — no HTTP calls.
"""
import asyncio
import logging
import os
from typing import Iterable

from dotenv import load_dotenv

load_dotenv()

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

from backend.db.database import AsyncSessionLocal, init_db
from backend.services.pipeline import (
    process_chat,
    approve_draft,
    approve_batch,
    get_pending_batch,
    get_pending_manual_invoice,
    approve_manual_invoice,
)
from backend.config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)
settings = get_settings()

# ── Security ──────────────────────────────────────────────────────────────────
ALLOWED_USER_ID: int | None = (
    int(settings.TELEGRAM_ALLOWED_USER_ID)
    if settings.TELEGRAM_ALLOWED_USER_ID.strip()
    else None
)


def _is_allowed(user_id: int) -> bool:
    if ALLOWED_USER_ID is None:
        return True  # dev mode: allow anyone
    return user_id == ALLOWED_USER_ID


def _not_allowed_reply() -> str:
    return "⛔ Sorry, this bot is private."


def _normalize_timeframe(raw: str) -> str | None:
    text = " ".join((raw or "").strip().lower().replace("-", " ").split())
    if not text:
        return None

    alias_map = {
        "today": "today",
        "yesterday": "yesterday",
        "this week": "this_week",
        "last week": "last_week",
        "monday": "last_monday",
        "tuesday": "last_tuesday",
        "wednesday": "last_wednesday",
        "thursday": "last_thursday",
        "friday": "last_friday",
        "saturday": "last_saturday",
        "sunday": "last_sunday",
        "last monday": "last_monday",
        "last tuesday": "last_tuesday",
        "last wednesday": "last_wednesday",
        "last thursday": "last_thursday",
        "last friday": "last_friday",
        "last saturday": "last_saturday",
        "last sunday": "last_sunday",
        "this monday": "this_monday",
        "this tuesday": "this_tuesday",
        "this wednesday": "this_wednesday",
        "this thursday": "this_thursday",
        "this friday": "this_friday",
        "this saturday": "this_saturday",
        "this sunday": "this_sunday",
    }
    return alias_map.get(text)


def _set_pending_command_input(
    context: ContextTypes.DEFAULT_TYPE,
    command_name: str,
    prompt: str,
) -> None:
    context.user_data["pending_command_input"] = {
        "command": command_name,
        "prompt": prompt,
    }


def _pop_pending_command_input(context: ContextTypes.DEFAULT_TYPE) -> dict | None:
    return context.user_data.pop("pending_command_input", None)


# ── HTML rendering helpers ────────────────────────────────────────────────────
# We use ParseMode.HTML throughout — it only requires escaping <, >, &
# so user data (names, emails, amounts, descriptions) never causes parse errors.
import html as _html


def _e(text: str) -> str:
    """HTML-escape a value so it's safe inside HTML parse mode messages."""
    return _html.escape(str(text))


def _md(text: str) -> str:
    """
    Convert the app's simple markdown (**bold**, *italic*) to Telegram HTML.
    """
    import re
    # **bold** → <b>bold</b>
    text = re.sub(r"\*\*(.+?)\*\*", lambda m: f"<b>{_e(m.group(1))}</b>", text)
    # *italic* → <i>italic</i>
    text = re.sub(r"\*(.+?)\*", lambda m: f"<i>{_e(m.group(1))}</i>", text)
    # Escape remaining plain text segments (everything not already in tags)
    # Simple approach: escape the whole thing first, then unescape our tags
    # Better approach: process segments between tags
    return text


def _md_safe(text: str) -> str:
    """
    Safely convert **bold** / *italic* markdown to HTML.
    Also supports Markdown links like [label](url) and converts them to HTML <a> links.
    Escapes raw text portions while preserving bold/italic/link tags.
    """
    import re
    # 1. Extract markdown links and replace them with tokens
    links = []
    def link_repl(match):
        label, url = match.group(1), match.group(2)
        token = f"__TG_CHAT_LINK_{len(links)}__"
        links.append((label, url))
        return token
    
    text_with_tokens = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", link_repl, text)
    
    # 2. Run standard split & bold/italic parsing on text_with_tokens
    parts = re.split(r"(\*\*.+?\*\*|\*.+?\*)", text_with_tokens)
    result = []
    for part in parts:
        if part.startswith("**") and part.endswith("**"):
            result.append(f"<b>{_e(part[2:-2])}</b>")
        elif part.startswith("*") and part.endswith("*"):
            result.append(f"<i>{_e(part[1:-1])}</i>")
        else:
            result.append(_e(part))
            
    html_result = "".join(result)
    
    # 3. Replace link tokens back with escaped label and raw URL HTML
    for i, (label, url) in enumerate(links):
        link_html = f'<a href="{_e(url)}">{_e(label)}</a>'
        html_result = html_result.replace(f"__TG_CHAT_LINK_{i}__", link_html)
        
    return html_result


# ── Response rendering ────────────────────────────────────────────────────────
def _render_draft_keyboard(draft_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✓ Create Invoice", callback_data=f"approve:{draft_id}:no_email"),
            InlineKeyboardButton("📧 Create & Send", callback_data=f"approve:{draft_id}:send_email"),
        ],
        [
            InlineKeyboardButton("✗ Discard", callback_data=f"discard:{draft_id}"),
        ],
    ])


def _get_batch_selection(context: ContextTypes.DEFAULT_TYPE, batch_id: str, item_ids: Iterable[str]) -> list[str]:
    selections = context.user_data.setdefault("batch_selections", {})
    valid_ids = list(item_ids)
    stored = selections.get(batch_id)
    if stored is None:
        selections[batch_id] = valid_ids.copy()
        return valid_ids

    valid_set = set(valid_ids)
    filtered = [item_id for item_id in stored if item_id in valid_set]
    if filtered != stored:
        selections[batch_id] = filtered
    return filtered


def _set_batch_selection(context: ContextTypes.DEFAULT_TYPE, batch_id: str, selected_item_ids: list[str]) -> None:
    selections = context.user_data.setdefault("batch_selections", {})
    selections[batch_id] = selected_item_ids


def _clear_batch_selection(context: ContextTypes.DEFAULT_TYPE, batch_id: str) -> None:
    selections = context.user_data.get("batch_selections")
    if selections:
        selections.pop(batch_id, None)


def _render_batch_keyboard_for_telegram(batch, selected_item_ids: list[str]) -> InlineKeyboardMarkup:
    selected = set(selected_item_ids)
    rows: list[list[InlineKeyboardButton]] = []

    for index, item in enumerate(batch.items):
        is_selected = item.item_id in selected
        icon = "✅" if is_selected else "⬜"
        label = item.data.item_name or item.data.task_description or f"Email {index + 1}"
        button_text = f"{icon} {index + 1}. {label[:22]}"
        rows.append([
            InlineKeyboardButton(
                button_text,
                callback_data=f"bt:{batch.batch_id}:{index}",
            )
        ])

    rows.extend([
        [
            InlineKeyboardButton("💾 Separate Drafts", callback_data=f"ba:{batch.batch_id}:s:0"),
            InlineKeyboardButton("📤 Send Separately", callback_data=f"ba:{batch.batch_id}:s:1"),
        ],
        [
            InlineKeyboardButton("🔗 Combine Draft", callback_data=f"ba:{batch.batch_id}:c:0"),
            InlineKeyboardButton("🔗📤 Combine Send", callback_data=f"ba:{batch.batch_id}:c:1"),
        ],
        [
            InlineKeyboardButton("✗ Discard", callback_data=f"bd:{batch.batch_id}"),
        ],
    ])

    return InlineKeyboardMarkup(rows)


def _render_manual_invoice_keyboard(draft_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✓ Create Invoice", callback_data=f"mi:{draft_id}:0"),
            InlineKeyboardButton("📧 Create & Send", callback_data=f"mi:{draft_id}:1"),
        ],
        [
            InlineKeyboardButton("✗ Discard", callback_data=f"md:{draft_id}"),
        ],
    ])


def _format_draft_message(draft) -> str:
    d = draft.data
    lines = ["🧾 <b>Draft Invoice</b>\n"]
    if d.client_name:
        lines.append(f"👤 <b>Client:</b> {_e(d.client_name)}")
    if d.client_email:
        lines.append(f"📧 <b>Email:</b> {_e(d.client_email)}")
    if d.item_name:
        lines.append(f"📦 <b>Item:</b> {_e(d.item_name)}")
    if d.task_description:
        lines.append(f"📝 <b>Description:</b> {_e(d.task_description)}")
    if d.amount is not None:
        currency = d.currency or "INR"
        lines.append(f"💰 <b>Amount:</b> {_e(currency)} {d.amount:,.2f}")
    if draft.email_subject:
        lines.append(f"📨 <b>Subject:</b> <i>{_e(draft.email_subject)}</i>")
    if draft.is_new_contact:
        lines.append("\n⚠️ <b>New contact</b> — will be created in Zoho on approval.")
    if d.missing_fields:
        missing = ", ".join(d.missing_fields)
        lines.append(f"\n❓ <b>Missing:</b> {_e(missing)}")
    return "\n".join(lines)


def _format_batch_message(batch, selected_item_ids: Iterable[str] | None = None) -> str:
    selected = set(selected_item_ids or [])
    lines = [f"📦 <b>Batch Invoice — {_e(batch.client_name)}</b>\n"]
    lines.append(f"Found <b>{len(batch.items)} emails</b> that can be invoiced:")
    for i, item in enumerate(batch.items, 1):
        d = item.data
        amount_str = f"{d.currency or 'INR'} {d.amount:,.2f}" if d.amount else "amount unknown"
        desc = d.task_description or d.item_name or "no description"
        marker = "✅" if item.item_id in selected else "⬜"
        lines.append(f"\n{marker} <b>{i}. {_e(desc[:60])}</b>")
        lines.append(f"   💰 {_e(amount_str)}")
        if item.email_subject:
            lines.append(f"   📨 <i>{_e(item.email_subject[:60])}</i>")
    lines.append("\nTap an item to include or exclude it, then choose how to process the selection.")
    return "\n".join(lines)


def _format_payment_message(payment_invoices) -> str:
    if not payment_invoices:
        return ""
    lines = []
    for inv in payment_invoices:
        status_emoji = "🔴" if inv.status == "overdue" else "🟡"
        overdue_str = f" ({inv.days_overdue}d overdue)" if inv.days_overdue else ""
        lines.append(
            f"{status_emoji} <b>{_e(inv.customer_name)}</b> — "
            f"{_e(inv.currency_code)} {inv.balance:,.2f}{_e(overdue_str)}"
        )
        if inv.due_date:
            lines.append(f"   📅 Due: {_e(inv.due_date)}")
    return "\n".join(lines)


def _format_created_invoices(invoices) -> str:
    lines = []
    for inv in invoices:
        sent_str = " 📧 sent" if inv.email_sent else ""
        lines.append(
            f"✅ Invoice <b>#{_e(inv.invoice_number)}</b> — "
            f"{_e(inv.client_name)} — "
            f"{_e(inv.currency)} {inv.amount:,.2f}{sent_str}"
        )
    return "\n".join(lines)


def _format_manual_invoice_message(draft) -> str:
    total = sum(item.amount for item in draft.line_items)
    lines = [f"🧾 <b>Manual Invoice Draft — {_e(draft.client_name)}</b>\n"]
    if draft.client_email:
        lines.append(f"📧 <b>Email:</b> {_e(draft.client_email)}")
    if draft.is_new_contact:
        lines.append("👤 <b>New customer</b> — will be created in Zoho on approval.")
    for index, item in enumerate(draft.line_items, 1):
        lines.append(f"\n<b>{index}. {_e(item.item_name)}</b>")
        lines.append(f"   📝 {_e(item.task_description)}")
        lines.append(f"   💰 {_e(draft.currency)} {item.amount:,.2f}")
    lines.append(f"\n<b>Total:</b> {_e(draft.currency)} {total:,.2f}")
    return "\n".join(lines)


# ── Handlers ─────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text(_not_allowed_reply())
        return

    welcome = (
        f"👋 Hey <b>{_e(user.first_name)}</b>! I'm your <b>Zoho Invoice Agent</b>.\n\n"
        "I can:\n"
        "• Scan your Gmail and create invoices\n"
        "• Answer payment questions (overdue, pending, etc.)\n"
        "• Send invoices to clients\n\n"
        "Useful commands:\n"
        "• <code>/get_mails yesterday</code>\n"
        "• <code>/get_mails last week</code>\n"
        "• <code>/payment_status</code>\n"
        "• <code>/payment_status_of Jash Khatri</code>\n\n"
        "Try typing:\n"
        '<i>"Check emails from yesterday"</i>\n'
        '<i>"Who hasn\'t paid me?"</i>\n'
        '<i>"Invoice Piyusha for the design work"</i>\n\n'
        "Use /status to check if Gmail and Zoho are connected."
    )
    await update.message.reply_text(welcome, parse_mode=ParseMode.HTML)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text(_not_allowed_reply())
        return

    async with AsyncSessionLocal() as db:
        from backend.db.models import OAuthToken
        from sqlalchemy import select
        result = await db.execute(select(OAuthToken))
        tokens = {t.service: t for t in result.scalars().all()}

    gmail_ok = "gmail" in tokens
    zoho_ok = "zoho" in tokens

    gmail_str = "✅ Connected" if gmail_ok else "❌ Not connected"
    zoho_str = "✅ Connected" if zoho_ok else "❌ Not connected"

    lines = [
        "🔌 <b>Connection Status</b>\n",
        f"📧 <b>Gmail:</b> {gmail_str}",
        f"🏢 <b>Zoho:</b> {zoho_str}",
    ]

    if not gmail_ok:
        lines.append('\n👉 <a href="http://localhost:8000/auth/gmail">Connect Gmail</a>')
    if not zoho_ok:
        lines.append('👉 <a href="http://localhost:8000/auth/zoho">Connect Zoho</a>')

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Utility command — tells the user their Telegram numeric ID."""
    user = update.effective_user
    # Plain text — no MarkdownV2 so numeric IDs never cause parse errors
    await update.message.reply_text(
        f"Your Telegram user ID is: {user.id}\n\n"
        f"Add this to your .env as:\n"
        f"TELEGRAM_ALLOWED_USER_ID={user.id}"
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text(_not_allowed_reply())
        return

    cleared = _pop_pending_command_input(context)
    if cleared:
        await update.message.reply_text("Okay, cancelled that command input.")
    else:
        await update.message.reply_text("There isn't anything pending right now.")


async def _run_chat_flow(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    message_text: str,
) -> None:
    user = update.effective_user
    logger.info(f"[TG] Message from {user.id}: {message_text!r}")

    # Send a "thinking" message that we'll update with status
    status_msg = await update.message.reply_text("⏳ Working on it…")

    async def status_cb(text: str) -> None:
        try:
            await status_msg.edit_text(text)
        except Exception:
            pass  # ignore if message can't be edited (too many edits, etc.)

    async with AsyncSessionLocal() as db:
        response = await process_chat(message_text, db, status_cb=status_cb, session_key=user.id)

    # Delete status message
    try:
        await status_msg.delete()
    except Exception:
        pass

    # ── Reply text ────────────────────────────────────────────────────────────
    if response.action not in ("recurring_pending", "recurring_list"):
        await update.message.reply_text(_md_safe(response.reply), parse_mode=ParseMode.HTML)

    # ── Payment invoice cards ─────────────────────────────────────────────────
    if response.payment_invoices:
        payment_text = _format_payment_message(response.payment_invoices)
        if payment_text:
            await update.message.reply_text(payment_text, parse_mode=ParseMode.HTML)

    # ── Created invoice cards ─────────────────────────────────────────────────
    if response.invoices_created:
        inv_text = _format_created_invoices(response.invoices_created)
        await update.message.reply_text(inv_text, parse_mode=ParseMode.HTML)

    # ── Draft cards ───────────────────────────────────────────────────────────
    if response.drafts:
        for draft in response.drafts:
            draft_text = _format_draft_message(draft)
            keyboard = _render_draft_keyboard(draft.draft_id)
            await update.message.reply_text(
                draft_text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )

    # ── Batch draft card ──────────────────────────────────────────────────────
    if response.batch_draft:
        batch = response.batch_draft
        item_ids = [item.item_id for item in batch.items]
        selected_ids = _get_batch_selection(context, batch.batch_id, item_ids)
        batch_text = _format_batch_message(batch, selected_ids)
        keyboard = _render_batch_keyboard_for_telegram(batch, selected_ids)
        await update.message.reply_text(
            batch_text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )

    if response.manual_invoice_draft:
        draft = response.manual_invoice_draft
        await update.message.reply_text(
            _format_manual_invoice_message(draft),
            parse_mode=ParseMode.HTML,
            reply_markup=_render_manual_invoice_keyboard(draft.draft_id),
        )

    # ── Recurring invoice: confirm card (pending) ─────────────────────────────
    if response.action == "recurring_pending":
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm & Create", callback_data=f"confirm_recurring:{user.id}"),
            InlineKeyboardButton("✗ Cancel",           callback_data=f"cancel_recurring:{user.id}"),
        ]])
        await update.message.reply_text(
            _md_safe(response.reply),
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
        return

    # ── Recurring list: add ⏹ Stop buttons ───────────────────────────────────
    if response.action == "recurring_list":
        from backend.services.pipeline import _pending_recurring_list
        raw_list = _pending_recurring_list.get(user.id, [])
        if raw_list:
            buttons = [
                [InlineKeyboardButton(
                    f"⏹ Stop #{i}  {inv.get('recurrence_name') or inv.get('customer_name','')}",
                    callback_data=f"stop_recurring:{inv.get('recurring_invoice_id')}",
                )]
                for i, inv in enumerate(raw_list, 1)
            ]
            await update.message.reply_text(
                _md_safe(response.reply),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        else:
            await update.message.reply_text(_md_safe(response.reply), parse_mode=ParseMode.HTML)
        return



async def cmd_get_mails(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text(_not_allowed_reply())
        return

    args_text = " ".join(context.args).strip()
    timeframe = _normalize_timeframe(args_text)
    if not timeframe:
        _set_pending_command_input(
            context,
            "get_mails",
            "Tell me the timeframe for `/get_mails`.\nExamples: `today`, `yesterday`, `monday`, `last week`",
        )
        await update.message.reply_text(
            "Got it — what timeframe should I use?\n\n"
            "Examples:\n"
            "• today\n"
            "• yesterday\n"
            "• monday\n"
            "• last week\n\n"
            "You can also send /cancel.",
        )
        return

    prompt = f"Check emails from {timeframe.replace('_', ' ')}"
    await _run_chat_flow(update, context, prompt)


async def cmd_payment_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text(_not_allowed_reply())
        return

    await _run_chat_flow(update, context, "Payment summary")


async def cmd_payment_status_of(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text(_not_allowed_reply())
        return

    client_name = " ".join(context.args).strip()
    if not client_name:
        _set_pending_command_input(
            context,
            "payment_status_of",
            "Tell me the client name for `/payment_status_of`.",
        )
        await update.message.reply_text(
            "Sure — which client should I check?\n\n"
            "Example:\n"
            "• Jash Khatri\n\n"
            "You can also send /cancel.",
        )
        return

    await _run_chat_flow(update, context, f"Did {client_name} pay?")


async def cmd_recurring(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start a recurring invoice — optionally pass all fields inline."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text(_not_allowed_reply())
        return
    args_text = " ".join(context.args).strip()
    prompt = args_text if args_text else "I want to create a recurring invoice"
    await _run_chat_flow(update, context, prompt)


async def cmd_list_recurring(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all active recurring invoices with inline Stop buttons."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text(_not_allowed_reply())
        return
    await _run_chat_flow(update, context, "list recurring invoices")


async def cmd_stop_recurring(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stop one or more recurring invoices. Pass numbers to stop directly."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text(_not_allowed_reply())
        return
    args_text = " ".join(context.args).strip()
    prompt = f"stop recurring {args_text}" if args_text else "stop recurring invoice"
    await _run_chat_flow(update, context, prompt)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not _is_allowed(user.id):
        # Helpfully tell them their ID even if blocked (so they can set it up)
        await update.message.reply_text(
            f"⛔ This bot is private\\.\n\n"
            f"If this is your bot, add your ID to `.env`:\n"
            f"`TELEGRAM_ALLOWED_USER_ID={user.id}`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    pending = context.user_data.get("pending_command_input")
    message_text = (update.message.text or "").strip()

    if pending:
        command_name = pending.get("command")
        _pop_pending_command_input(context)

        if command_name == "get_mails":
            timeframe = _normalize_timeframe(message_text)
            if not timeframe:
                _set_pending_command_input(
                    context,
                    "get_mails",
                    pending.get("prompt", ""),
                )
                await update.message.reply_text(
                    "I didn't recognize that timeframe. Try `today`, `yesterday`, `monday`, or `last week`.\n"
                    "Send `/cancel` to stop.",
                    parse_mode=ParseMode.MARKDOWN,
                )
                return
            await _run_chat_flow(update, context, f"Check emails from {timeframe.replace('_', ' ')}")
            return

        if command_name == "payment_status_of":
            if not message_text:
                _set_pending_command_input(
                    context,
                    "payment_status_of",
                    pending.get("prompt", ""),
                )
                await update.message.reply_text("Please send the client name, or `/cancel`.")
                return
            await _run_chat_flow(update, context, f"Did {message_text} pay?")
            return

    await _run_chat_flow(update, context, message_text)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()  # removes the loading spinner on the button

    user = query.from_user
    if not _is_allowed(user.id):
        await query.answer("⛔ Not allowed.", show_alert=True)
        return

    data = query.data
    logger.info(f"[TG] Callback from {user.id}: {data!r}")

    # ── Discard draft ─────────────────────────────────────────────────────────
    if data.startswith("discard:"):
        await query.edit_message_text("🗑️ Draft discarded.", parse_mode=ParseMode.HTML)
        return

    if data.startswith("bd:"):
        batch_id = data.split(":", 1)[1]
        _clear_batch_selection(context, batch_id)
        await query.edit_message_text("🗑️ Batch discarded.", parse_mode=ParseMode.HTML)
        return

    if data.startswith("md:"):
        await query.edit_message_text("🗑️ Manual invoice draft discarded.", parse_mode=ParseMode.HTML)
        return

    if data.startswith("bt:"):
        _, batch_id, item_index_raw = data.split(":")
        batch = get_pending_batch(batch_id)
        if not batch:
            _clear_batch_selection(context, batch_id)
            await query.edit_message_text(
                "This batch draft has expired or was already processed.",
                parse_mode=ParseMode.HTML,
            )
            return

        try:
            item_index = int(item_index_raw)
            item = batch.items[item_index]
        except (ValueError, IndexError):
            await query.answer("That selection is no longer available.", show_alert=True)
            return

        item_ids = [batch_item.item_id for batch_item in batch.items]
        selected_ids = _get_batch_selection(context, batch_id, item_ids)

        if item.item_id in selected_ids:
            selected_ids = [selected_id for selected_id in selected_ids if selected_id != item.item_id]
        else:
            selected_ids.append(item.item_id)

        _set_batch_selection(context, batch_id, selected_ids)
        await query.edit_message_text(
            _format_batch_message(batch, selected_ids),
            parse_mode=ParseMode.HTML,
            reply_markup=_render_batch_keyboard_for_telegram(batch, selected_ids),
        )
        return

    # ── Approve single draft ──────────────────────────────────────────────────
    if data.startswith("approve:"):
        parts = data.split(":")
        draft_id = parts[1]
        send_email = parts[2] == "send_email"

        await query.edit_message_text("⏳ Creating invoice…")

        async with AsyncSessionLocal() as db:
            response = await approve_draft(draft_id, {"send_email": send_email}, db)

        result_text = _md_safe(response.reply)
        if response.invoices_created:
            result_text += "\n\n" + _format_created_invoices(response.invoices_created)

        await query.edit_message_text(result_text, parse_mode=ParseMode.HTML)
        return

    # ── Approve batch ─────────────────────────────────────────────────────────
    if data.startswith("ba:"):
        _, batch_id, mode_code, email_flag = data.split(":")
        mode = "combined" if mode_code == "c" else "separate"
        send_email = email_flag == "1"
        batch = get_pending_batch(batch_id)
        if not batch:
            _clear_batch_selection(context, batch_id)
            await query.edit_message_text(
                "This batch draft has expired or was already processed.",
                parse_mode=ParseMode.HTML,
            )
            return

        item_ids = [item.item_id for item in batch.items]
        selected_ids = _get_batch_selection(context, batch_id, item_ids)
        if not selected_ids:
            await query.answer("Select at least one email first.", show_alert=True)
            return

        action_label = (
            "Combining into one invoice" if mode == "combined" else "Creating separate invoices"
        )
        await query.edit_message_text(f"⏳ {action_label}…")

        async with AsyncSessionLocal() as db:
            response = await approve_batch(
                batch_draft_id=batch_id,
                mode=mode,
                selected_item_ids=selected_ids,
                send_email=send_email,
                db=db,
            )

        _clear_batch_selection(context, batch_id)
        result_text = _md_safe(response.reply)
        if response.invoices_created:
            result_text += "\n\n" + _format_created_invoices(response.invoices_created)

        await query.edit_message_text(result_text, parse_mode=ParseMode.HTML)
        return

    if data.startswith("mi:"):
        _, draft_id, send_flag = data.split(":")
        draft = get_pending_manual_invoice(draft_id)
        if not draft:
            await query.edit_message_text(
                "This manual invoice draft has expired or was already processed.",
                parse_mode=ParseMode.HTML,
            )
            return

        send_email = send_flag == "1"
        await query.edit_message_text("⏳ Creating manual invoice…")

        async with AsyncSessionLocal() as db:
            response = await approve_manual_invoice(
                draft_id=draft_id,
                send_email=send_email,
                db=db,
            )

        result_text = _md_safe(response.reply)
        if response.invoices_created:
            result_text += "\n\n" + _format_created_invoices(response.invoices_created)
        await query.edit_message_text(result_text, parse_mode=ParseMode.HTML)
        return

    # ── Recurring: confirm create ─────────────────────────────────────────────
    if data.startswith("confirm_recurring:"):
        await query.edit_message_text("⏳ Creating recurring invoice…")
        async with AsyncSessionLocal() as db:
            response = await process_chat("confirm", db, session_key=user.id)
        await query.edit_message_text(response.reply, parse_mode=ParseMode.HTML)
        return

    if data.startswith("cancel_recurring:"):
        from backend.services.pipeline import _pending_recurring_conv
        _pending_recurring_conv.pop(user.id, None)
        await query.edit_message_text("Recurring invoice cancelled. ✗")
        return

    # ── Recurring: stop one invoice ───────────────────────────────────────────
    if data.startswith("stop_recurring:"):
        recurring_id = data.split(":", 1)[1]
        await query.edit_message_text("⏹ Stopping recurring invoice…")
        from backend.services.zoho_service import stop_recurring_invoice, recurring_invoice_url
        async with AsyncSessionLocal() as db:
            ok = await stop_recurring_invoice(recurring_id, db)
        url = recurring_invoice_url(recurring_id)
        if ok:
            await query.edit_message_text(
                f"✅ Recurring invoice stopped.\n\n🔗 <b><a href=\"{_e(url)}\">View Profile in Zoho</a></b>",
                parse_mode=ParseMode.HTML,
            )
        else:
            await query.edit_message_text(
                f"⚠️ Could not stop recurring invoice.\n\n🔗 <b><a href=\"{_e(url)}\">Check on Zoho</a></b>",
                parse_mode=ParseMode.HTML,
            )
        return

    # Unknown callback
    await query.answer("Unknown action.", show_alert=True)


# ── Main ──────────────────────────────────────────────────────────────────────
async def post_init(application: Application) -> None:
    """Called by python-telegram-bot after the app is initialised but before polling starts."""
    # Initialise DB tables
    try:
        await init_db()
        logger.info("Database ready.")
    except Exception as e:
        logger.warning(
            f"Database not reachable at startup: {e}\n"
            "The bot will still run — DB-dependent features will fail until the DB is back."
        )
    # Register bot commands in Telegram menu
    await application.bot.set_my_commands([
        BotCommand("start",             "Welcome message & usage guide"),
        BotCommand("status",            "Check Gmail & Zoho connection status"),
        BotCommand("get_mails",         "Scan emails for a timeframe like today or last week"),
        BotCommand("payment_status",    "Show overall payment summary and status"),
        BotCommand("payment_status_of", "Check payment status of one client"),
        BotCommand("recurring",         "Create a recurring invoice"),
        BotCommand("list_recurring",    "List all active recurring invoices"),
        BotCommand("stop_recurring",    "Stop one or more recurring invoices"),
        BotCommand("cancel",            "Cancel a pending command prompt"),
        BotCommand("myid",              "Show your Telegram user ID"),
    ])
    logger.info("Bot commands registered.")


if __name__ == "__main__":
    token = settings.TELEGRAM_BOT_TOKEN.strip()
    if not token:
        raise ValueError(
            "TELEGRAM_BOT_TOKEN is not set in .env. "
            "Get a token from @BotFather on Telegram and add it."
        )

    if ALLOWED_USER_ID:
        logger.info(f"Security: only accepting messages from user ID {ALLOWED_USER_ID}")
    else:
        logger.warning(
            "TELEGRAM_ALLOWED_USER_ID is not set — the bot will respond to ANYONE. "
            "Message the bot and use /myid to get your ID, then add it to .env."
        )

    app = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .build()
    )

    # ── Global error handler — keeps the bot alive on network blips ───────────
    from telegram.error import TimedOut, NetworkError as TgNetworkError

    async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        err = context.error
        if isinstance(err, (TimedOut, TgNetworkError)):
            # Transient connectivity issue — log and keep running
            logger.warning(f"Telegram network error (ignored): {err}")
            return
        # Genuine application error — log the full traceback
        logger.error("Unhandled exception in bot handler", exc_info=context.error)

    app.add_error_handler(_error_handler)

    app.add_handler(CommandHandler("start",             cmd_start))
    app.add_handler(CommandHandler("status",            cmd_status))
    app.add_handler(CommandHandler("get_mails",         cmd_get_mails))
    app.add_handler(CommandHandler("payment_status",    cmd_payment_status))
    app.add_handler(CommandHandler("payment_status_of", cmd_payment_status_of))
    app.add_handler(CommandHandler("recurring",         cmd_recurring))
    app.add_handler(CommandHandler("list_recurring",    cmd_list_recurring))
    app.add_handler(CommandHandler("stop_recurring",    cmd_stop_recurring))
    app.add_handler(CommandHandler("cancel",            cmd_cancel))
    app.add_handler(CommandHandler("myid",              cmd_myid))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("Starting Telegram bot (polling)…")
    # Python 3.14 no longer auto-creates an event loop — create one explicitly
    import asyncio as _asyncio
    loop = _asyncio.new_event_loop()
    _asyncio.set_event_loop(loop)
    # run_polling() manages the event loop lifecycle itself
    app.run_polling(allowed_updates=Update.ALL_TYPES)
