import asyncio
import logging
from sqlalchemy import text
from backend.db.database import AsyncSessionLocal

logger = logging.getLogger(__name__)

async def main():
    logger.info("Running database migration to add last_reminded_at column…")
    async with AsyncSessionLocal() as session:
        await session.execute(text(
            "ALTER TABLE invoice_cache ADD COLUMN IF NOT EXISTS last_reminded_at BIGINT;"
        ))
        await session.commit()
    logger.info("Migration successful — last_reminded_at column is ready.")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
