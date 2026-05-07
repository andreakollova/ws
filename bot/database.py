import aiosqlite
import logging
from pathlib import Path

DB_PATH = Path("/tmp/woeva_bot.db")
logger = logging.getLogger(__name__)


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS processed_events (
                supabase_id  TEXT PRIMARY KEY,
                discord_msg_id TEXT,
                channel_id   TEXT,
                status       TEXT DEFAULT 'pending',
                created_at   TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.commit()


async def is_processed(supabase_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM processed_events WHERE supabase_id = ?", (supabase_id,)
        ) as cur:
            return await cur.fetchone() is not None


async def add_pending_event(supabase_id: str, discord_msg_id: str, channel_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO processed_events (supabase_id, discord_msg_id, channel_id) VALUES (?, ?, ?)",
            (supabase_id, discord_msg_id, channel_id),
        )
        await db.commit()


async def update_event_status(supabase_id: str, status: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE processed_events SET status = ? WHERE supabase_id = ?",
            (status, supabase_id),
        )
        await db.commit()
