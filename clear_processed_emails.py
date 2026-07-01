"""
One-shot script: delete all rows from processed_emails so emails
can be re-scanned and re-invoiced during testing.

Run with:
    source .venv/bin/activate
    python clear_processed_emails.py
"""
import asyncio
from sqlalchemy import text
from backend.db.database import engine


async def main() -> None:
    async with engine.begin() as conn:
        result = await conn.execute(text("DELETE FROM processed_emails"))
        print(f"✅  Deleted {result.rowcount} row(s) from processed_emails.")


if __name__ == "__main__":
    asyncio.run(main())
