"""Lightweight PostgreSQL client for auth_users table."""

import asyncio
import os
from typing import Dict, List, Optional

import asyncpg

from logger import logger

POOL_CONNECT_MAX_RETRIES = 5
POOL_CONNECT_BASE_DELAY = 1.0


class AuthDB:
    """PostgreSQL client managing the auth_users table."""

    def __init__(
        self,
        host: str = "postgres",
        port: int = 5432,
        database: str = "chatbot",
        user: str = "chatbot_user",
        password: str = "chatbot_password",
        pool_size: int = 5,
    ):
        self.host = host
        self.port = port
        self.database = database
        self.user = user
        self.password = password
        self.pool_size = pool_size
        self.pool: Optional[asyncpg.Pool] = None

    async def init_pool(self) -> None:
        """Initialize the connection pool with retry logic and create tables."""
        last_error = None
        for attempt in range(POOL_CONNECT_MAX_RETRIES):
            try:
                await self._ensure_database_exists()

                self.pool = await asyncpg.create_pool(
                    host=self.host,
                    port=self.port,
                    database=self.database,
                    user=self.user,
                    password=self.password,
                    min_size=2,
                    max_size=self.pool_size,
                    command_timeout=30,
                )

                await self._ensure_tables()
                logger.info("Auth DB pool initialized successfully")
                return

            except Exception as e:
                last_error = e
                if attempt < POOL_CONNECT_MAX_RETRIES - 1:
                    delay = POOL_CONNECT_BASE_DELAY * (2**attempt)
                    logger.warning(
                        f"Auth DB connection attempt {attempt + 1} failed: {e}, retrying in {delay}s"
                    )
                    await asyncio.sleep(delay)

        logger.error(
            f"Failed to initialize Auth DB pool after {POOL_CONNECT_MAX_RETRIES} attempts: {last_error}"
        )
        raise last_error

    async def _ensure_database_exists(self) -> None:
        """Ensure the target database exists, create if it doesn't."""
        try:
            conn = await asyncpg.connect(
                host=self.host,
                port=self.port,
                database="postgres",
                user=self.user,
                password=self.password,
            )
            try:
                result = await conn.fetchval(
                    "SELECT 1 FROM pg_database WHERE datname = $1", self.database
                )
                if not result:
                    await conn.execute(f'CREATE DATABASE "{self.database}"')
                    logger.info(f"Created database: {self.database}")
            finally:
                await conn.close()
        except Exception as e:
            logger.error(f"Error ensuring database exists: {e}")

    async def _ensure_tables(self) -> None:
        """Create auth_users table and index if they don't exist."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS auth_users (
                    id SERIAL PRIMARY KEY,
                    email VARCHAR(255) UNIQUE NOT NULL,
                    totp_secret VARCHAR(64),
                    is_totp_setup BOOLEAN DEFAULT FALSE,
                    is_allowed BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_auth_users_email ON auth_users(email)"
            )

    async def close(self) -> None:
        """Close the connection pool."""
        if self.pool:
            await self.pool.close()
            logger.info("Auth DB pool closed")

    async def get_auth_user(self, email: str) -> Optional[Dict]:
        """Get an allowed user by email."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, email, totp_secret, is_totp_setup, is_allowed "
                "FROM auth_users WHERE email = $1",
                email.lower(),
            )
            return dict(row) if row else None

    async def create_auth_user_totp(self, email: str, totp_secret: str) -> None:
        """Set TOTP secret for a user (first-time enrollment)."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE auth_users SET totp_secret = $1, updated_at = CURRENT_TIMESTAMP "
                "WHERE email = $2",
                totp_secret,
                email.lower(),
            )

    async def mark_totp_setup_complete(self, email: str) -> None:
        """Mark TOTP enrollment as complete after first successful verify."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE auth_users SET is_totp_setup = TRUE, updated_at = CURRENT_TIMESTAMP "
                "WHERE email = $1",
                email.lower(),
            )

    async def add_allowed_user(self, email: str) -> None:
        """Add or re-enable an email in the allowlist."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO auth_users (email, is_allowed) VALUES ($1, TRUE) "
                "ON CONFLICT (email) DO UPDATE SET is_allowed = TRUE, updated_at = CURRENT_TIMESTAMP",
                email.lower(),
            )

    async def sync_allowed_users(self, emails: list[str]) -> None:
        """Sync the allowlist: enable listed emails, disable removed ones."""
        if not emails:
            return
        lower_emails = [e.lower() for e in emails]
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                for email in lower_emails:
                    await conn.execute(
                        "INSERT INTO auth_users (email, is_allowed) VALUES ($1, TRUE) "
                        "ON CONFLICT (email) DO UPDATE SET is_allowed = TRUE, updated_at = CURRENT_TIMESTAMP",
                        email,
                    )
                await conn.execute(
                    "UPDATE auth_users SET is_allowed = FALSE, totp_secret = NULL, "
                    "is_totp_setup = FALSE, updated_at = CURRENT_TIMESTAMP "
                    "WHERE email != ALL($1::varchar[])",
                    lower_emails,
                )
        logger.info(f"Synced auth allowlist: {len(lower_emails)} user(s) enabled")
