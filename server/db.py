import os
import asyncpg
from typing import Optional
from .config import get_database_token

CREATE_SESSIONS_TABLE = """
CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL DEFAULT 'anonymous',
    title TEXT NOT NULL DEFAULT 'New Chat',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

CREATE_MESSAGES_TABLE = """
CREATE TABLE IF NOT EXISTS chat_messages (
    message_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES chat_sessions(session_id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    genie_response JSONB,
    summary TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON chat_messages(session_id, created_at);
"""

CREATE_USER_SP_MAPPING = """
CREATE TABLE IF NOT EXISTS user_sp_mapping (
    user_email   TEXT        PRIMARY KEY,
    sp_client_id TEXT        NOT NULL,
    role_name    TEXT        NOT NULL DEFAULT 'analyst',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

UPDATE_SESSION_TRIGGER = """
CREATE OR REPLACE FUNCTION update_session_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    UPDATE chat_sessions SET updated_at = NOW() WHERE session_id = NEW.session_id;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trig_update_session ON chat_messages;
CREATE TRIGGER trig_update_session
AFTER INSERT ON chat_messages
FOR EACH ROW EXECUTE FUNCTION update_session_timestamp();
"""


class DatabasePool:
    def __init__(self):
        self._pool: Optional[asyncpg.Pool] = None
        self.demo_mode = False

    async def initialize(self):
        """Initialize DB pool and create tables."""
        if not os.environ.get("PGHOST"):
            print("WARNING: PGHOST not set - running in demo mode (no persistence)")
            self.demo_mode = True
            return

        try:
            # Generate a Lakebase-scoped credential token (required for Databricks identity login)
            instance_name = os.environ.get("LAKEBASE_INSTANCE_NAME", "akash-finance-app")
            password = get_database_token(instance_name)
            pg_port_raw = os.environ.get("PGPORT", "5432")
            try:
                pg_port = int(pg_port_raw)
            except (ValueError, TypeError):
                pg_port = 5432

            self._pool = await asyncpg.create_pool(
                host=os.environ["PGHOST"],
                port=pg_port,
                database=os.environ.get("PGDATABASE", "databricks_postgres"),
                user=os.environ.get("PGUSER", "postgres"),
                password=password,
                ssl="require",
                min_size=2,
                max_size=10,
                command_timeout=30,
            )
            # Table setup is non-fatal: tables may already be pre-created by admin
            try:
                await self._create_tables()
            except Exception as table_err:
                print(f"Table setup skipped (may already exist): {table_err}")
            print("Database initialized successfully")
        except Exception as e:
            print(f"Database init failed: {e} - running in demo mode")
            self.demo_mode = True

    async def _create_tables(self):
        async with self._pool.acquire() as conn:
            await conn.execute(CREATE_SESSIONS_TABLE)
            await conn.execute(CREATE_MESSAGES_TABLE)
            await conn.execute(CREATE_USER_SP_MAPPING)
            try:
                await conn.execute(UPDATE_SESSION_TRIGGER)
            except Exception:
                pass  # Trigger may already exist

    async def get_pool(self) -> Optional[asyncpg.Pool]:
        if self.demo_mode:
            return None
        if self._pool is None:
            await self.initialize()
        return self._pool

    async def refresh_token(self):
        """Refresh OAuth token - call every ~45 minutes."""
        if self._pool:
            await self._pool.close()
            self._pool = None
        await self.initialize()

    async def close(self):
        if self._pool:
            await self._pool.close()


db = DatabasePool()


async def get_sp_for_user(pool, user_email: str) -> str | None:
    """Look up which SP client_id a user is mapped to. Returns None if unmapped."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT sp_client_id FROM user_sp_mapping WHERE user_email = $1",
            user_email,
        )
        return row["sp_client_id"] if row else None
