#
# SPDX-FileCopyrightText: Copyright (c) 1993-2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""PostgreSQL-based conversation storage with LRU caching and I/O optimization.

User-scoped: every conversation, chat metadata row, message, and user
preference is keyed by the JWT ``sub`` (an email string). Cross-user
reads/writes are not possible through the public API.

Message persistence model (PR 6): one row per message in a separate
``messages`` table, keyed by ``(chat_id, position)``. Previously the entire
conversation was stored as a JSONB blob on the ``conversations`` row and
rewritten on every turn (quadratic write amplification on long chats). The
new layout appends only the new turn's messages and supports cheap LIMIT'd
reads of recent history.
"""

import json
import time
from collections import OrderedDict
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
import asyncio
import asyncpg
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, BaseMessage, ToolMessage

from logger import logger

MAX_CACHE_ENTRIES = 200
POOL_CONNECT_MAX_RETRIES = 5
POOL_CONNECT_BASE_DELAY = 1.0

# Advisory lock key for schema migrations — serializes startup across pods so
# only one runs DDL at a time. Arbitrary but stable 64-bit integer.
_MIGRATION_ADVISORY_LOCK_KEY = 84283123491


@dataclass
class CacheEntry:
    """Cache entry with TTL support."""
    data: Any
    timestamp: float
    ttl: float = 300

    def is_expired(self) -> bool:
        return time.time() - self.timestamp > self.ttl


class LRUCache:
    """Bounded LRU cache with TTL expiration to prevent unbounded memory growth."""

    def __init__(self, max_size: int = MAX_CACHE_ENTRIES, default_ttl: float = 300):
        self._data: OrderedDict[str, CacheEntry] = OrderedDict()
        self.max_size = max_size
        self.default_ttl = default_ttl
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Optional[Any]:
        entry = self._data.get(key)
        if entry is None:
            self.misses += 1
            return None
        if entry.is_expired():
            del self._data[key]
            self.misses += 1
            return None
        self._data.move_to_end(key)
        self.hits += 1
        return entry.data

    def put(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = CacheEntry(
            data=value, timestamp=time.time(), ttl=ttl or self.default_ttl
        )
        while len(self._data) > self.max_size:
            self._data.popitem(last=False)

    def remove(self, key: str) -> None:
        self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)

    def evict_expired(self) -> int:
        expired = [k for k, v in self._data.items() if v.is_expired()]
        for k in expired:
            del self._data[k]
        return len(expired)


def _cache_key(user_id: str, chat_id: str) -> str:
    """Build a user-scoped cache key."""
    return f"{user_id}|{chat_id}"


# --- Message <-> DB row helpers ---


_ROLE_TO_CLASS = {
    "human": HumanMessage,
    "ai": AIMessage,
    "system": SystemMessage,
    "tool": ToolMessage,
}


def _message_role(message: BaseMessage) -> str:
    if isinstance(message, HumanMessage):
        return "human"
    if isinstance(message, AIMessage):
        return "ai"
    if isinstance(message, SystemMessage):
        return "system"
    if isinstance(message, ToolMessage):
        return "tool"
    return "human"  # safest default; preserves text round-trip


def _row_to_message(row) -> BaseMessage:
    role = row["role"]
    content = row["content"]
    if role == "ai":
        msg = AIMessage(content=content)
        if row.get("tool_calls"):
            tc = row["tool_calls"]
            if isinstance(tc, str):
                tc = json.loads(tc)
            msg.tool_calls = tc
        return msg
    if role == "human":
        return HumanMessage(content=content)
    if role == "system":
        return SystemMessage(content=content)
    if role == "tool":
        return ToolMessage(
            content=content,
            tool_call_id=row.get("tool_call_id") or "",
            name=row.get("name") or "",
        )
    return HumanMessage(content=content)


class PostgreSQLConversationStorage:
    """PostgreSQL-based conversation storage with LRU caching."""

    def __init__(
        self,
        host: str = 'postgres',
        port: int = 5432,
        database: str = 'chatbot',
        user: str = 'chatbot_user',
        password: str = '',
        pool_size: int = 10,
        cache_ttl: int = 300
    ):
        self.host = host
        self.port = port
        self.database = database
        self.user = user
        self.password = password
        self.pool_size = pool_size
        self.cache_ttl = cache_ttl

        self.pool: Optional[asyncpg.Pool] = None

        self._message_cache = LRUCache(max_size=MAX_CACHE_ENTRIES, default_ttl=cache_ttl)
        self._metadata_cache = LRUCache(max_size=MAX_CACHE_ENTRIES, default_ttl=cache_ttl)
        self._chat_list_cache: Dict[str, CacheEntry] = {}
        self._cache_eviction_task: Optional[asyncio.Task] = None

        self._db_operations = 0

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
                    command_timeout=30
                )

                await self._create_tables()
                logger.debug("PostgreSQL connection pool initialized successfully")

                self._cache_eviction_task = asyncio.create_task(self._cache_eviction_worker())
                return

            except Exception as e:
                last_error = e
                if attempt < POOL_CONNECT_MAX_RETRIES - 1:
                    delay = POOL_CONNECT_BASE_DELAY * (2 ** attempt)
                    logger.warning(f"PostgreSQL connection attempt {attempt + 1} failed: {e}, retrying in {delay}s")
                    await asyncio.sleep(delay)

        logger.error(f"Failed to initialize PostgreSQL pool after {POOL_CONNECT_MAX_RETRIES} attempts: {last_error}")
        raise last_error

    async def _ensure_database_exists(self) -> None:
        """Ensure the target database exists, create if it doesn't."""
        try:
            conn = await asyncpg.connect(
                host=self.host,
                port=self.port,
                database='postgres',
                user=self.user,
                password=self.password
            )

            try:
                result = await conn.fetchval(
                    "SELECT 1 FROM pg_database WHERE datname = $1",
                    self.database
                )

                if not result:
                    await conn.execute(f'CREATE DATABASE "{self.database}"')
                    logger.debug(f"Created database: {self.database}")
                else:
                    logger.debug(f"Database {self.database} already exists")

            finally:
                await conn.close()

        except Exception as e:
            logger.error(f"Error ensuring database exists: {e}")
            pass

    async def close(self) -> None:
        """Close the connection pool and cleanup background tasks."""
        if self._cache_eviction_task:
            self._cache_eviction_task.cancel()
            try:
                await self._cache_eviction_task
            except asyncio.CancelledError:
                pass

        if self.pool:
            await self.pool.close()
            logger.debug("PostgreSQL connection pool closed")

    async def _create_tables(self) -> None:
        """Create / migrate schema.

        Uses a Postgres advisory lock so only one pod runs DDL at a time
        during rolling deploys. Each migration step is idempotent.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Serialize migrations across pods
                await conn.execute(
                    "SELECT pg_advisory_xact_lock($1)",
                    _MIGRATION_ADVISORY_LOCK_KEY,
                )

                # --- Migration 1: chat tables become user-scoped (PR 1) ---
                conv_has_user_id = await conn.fetchval("""
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'conversations' AND column_name = 'user_id'
                    )
                """)
                if not conv_has_user_id:
                    logger.warning(
                        "Schema migration: dropping legacy conversations + chat_metadata "
                        "(no user_id column present). Existing chat history will be lost."
                    )
                    await conn.execute("DROP TABLE IF EXISTS chat_metadata CASCADE")
                    await conn.execute("DROP TABLE IF EXISTS conversations CASCADE")

                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS conversations (
                        chat_id VARCHAR(255) PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        messages JSONB NOT NULL DEFAULT '[]'::jsonb,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        message_count INTEGER DEFAULT 0
                    )
                """)
                # Migration 4 (below) moves message data out of this column. Kept
                # nullable-via-default for back-compat with code paths that may
                # still INSERT a chat row before append_messages_to_chat runs.

                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS chat_metadata (
                        chat_id VARCHAR(255) PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        name VARCHAR(500),
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (chat_id) REFERENCES conversations(chat_id) ON DELETE CASCADE
                    )
                """)

                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_conversations_user_updated "
                    "ON conversations(user_id, updated_at DESC)"
                )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_chat_metadata_user "
                    "ON chat_metadata(user_id)"
                )

                # --- Migration 2: document_sources gets user_id + visibility (PR 1) ---
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS document_sources (
                        source_name VARCHAR(500) PRIMARY KEY,
                        file_path VARCHAR(1000),
                        task_id VARCHAR(255),
                        chunk_count INTEGER DEFAULT 0,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                await conn.execute(
                    "ALTER TABLE document_sources ADD COLUMN IF NOT EXISTS user_id TEXT"
                )
                await conn.execute(
                    "ALTER TABLE document_sources ADD COLUMN IF NOT EXISTS "
                    "visibility TEXT NOT NULL DEFAULT 'public'"
                )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_document_sources_visibility_user "
                    "ON document_sources(visibility, user_id)"
                )

                # --- Migration 3: per-user preferences (PR 1) ---
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS user_preferences (
                        user_id TEXT PRIMARY KEY,
                        selected_sources JSONB NOT NULL DEFAULT '[]'::jsonb,
                        current_chat_id VARCHAR(255),
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                # --- Migration 4: row-per-message storage (PR 6) ---
                # New ``messages`` table holds one row per turn. The JSONB
                # ``conversations.messages`` column is preserved as a backup
                # for one release cycle and will be dropped in PR 7.
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS messages (
                        chat_id VARCHAR(255) NOT NULL,
                        user_id TEXT NOT NULL,
                        position INTEGER NOT NULL,
                        role TEXT NOT NULL CHECK (role IN ('human','ai','system','tool')),
                        content TEXT NOT NULL,
                        tool_calls JSONB,
                        tool_call_id VARCHAR(255),
                        name VARCHAR(255),
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (chat_id, position),
                        FOREIGN KEY (chat_id) REFERENCES conversations(chat_id) ON DELETE CASCADE
                    )
                """)
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_messages_user_chat_position "
                    "ON messages(user_id, chat_id, position)"
                )

                # One-shot backfill: copy JSONB messages into per-row storage
                # for any conversation that has data in the old column but
                # nothing in the new table. Idempotent — skipped on subsequent
                # startups because each conversation appears in `messages`
                # exactly once.
                unmigrated = await conn.fetch("""
                    SELECT c.chat_id, c.user_id, c.messages
                    FROM conversations c
                    WHERE jsonb_array_length(c.messages) > 0
                      AND NOT EXISTS (
                        SELECT 1 FROM messages m WHERE m.chat_id = c.chat_id
                      )
                """)
                if unmigrated:
                    logger.warning(
                        "Schema migration: backfilling %d conversation(s) "
                        "from JSONB to row-per-message storage",
                        len(unmigrated),
                    )
                    for conv_row in unmigrated:
                        chat_id = conv_row["chat_id"]
                        user_id = conv_row["user_id"]
                        msgs_blob = conv_row["messages"]
                        if isinstance(msgs_blob, str):
                            msgs_blob = json.loads(msgs_blob)
                        rows = []
                        for pos, m in enumerate(msgs_blob or []):
                            mtype = m.get("type", "")
                            role = {
                                "AIMessage": "ai",
                                "HumanMessage": "human",
                                "SystemMessage": "system",
                                "ToolMessage": "tool",
                            }.get(mtype, "human")
                            rows.append((
                                chat_id,
                                user_id,
                                pos,
                                role,
                                m.get("content", "") or "",
                                json.dumps(m["tool_calls"]) if m.get("tool_calls") else None,
                                m.get("tool_call_id"),
                                m.get("name"),
                            ))
                        if rows:
                            await conn.executemany(
                                """
                                INSERT INTO messages
                                    (chat_id, user_id, position, role, content,
                                     tool_calls, tool_call_id, name)
                                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                                ON CONFLICT (chat_id, position) DO NOTHING
                                """,
                                rows,
                            )

                # --- Images: unchanged (kept here for completeness) ---
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS images (
                        image_id VARCHAR(255) PRIMARY KEY,
                        image_data TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        expires_at TIMESTAMP DEFAULT (CURRENT_TIMESTAMP + INTERVAL '1 hour')
                    )
                """)
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_images_expires_at ON images(expires_at)"
                )

                # Trigger to keep updated_at fresh on conversations
                await conn.execute("""
                    CREATE OR REPLACE FUNCTION update_updated_at_column()
                    RETURNS TRIGGER AS $$
                    BEGIN
                        NEW.updated_at = CURRENT_TIMESTAMP;
                        RETURN NEW;
                    END;
                    $$ language 'plpgsql'
                """)
                await conn.execute(
                    "DROP TRIGGER IF EXISTS update_conversations_updated_at ON conversations"
                )
                await conn.execute("""
                    CREATE TRIGGER update_conversations_updated_at
                        BEFORE UPDATE ON conversations
                        FOR EACH ROW
                        EXECUTE FUNCTION update_updated_at_column()
                """)

    # --- Message <-> dict for SSE / API surfaces (kept for back-compat) ---

    def _message_to_dict(self, message: BaseMessage) -> Dict:
        result = {
            "type": message.__class__.__name__,
            "content": message.content,
        }
        if hasattr(message, "tool_calls") and message.tool_calls:
            result["tool_calls"] = message.tool_calls
        if isinstance(message, ToolMessage):
            result["tool_call_id"] = getattr(message, "tool_call_id", None)
            result["name"] = getattr(message, "name", None)
        return result

    def _dict_to_message(self, data: Dict) -> BaseMessage:
        msg_type = data["type"]
        content = data["content"]
        if msg_type == "AIMessage":
            msg = AIMessage(content=content)
            if "tool_calls" in data:
                msg.tool_calls = data["tool_calls"]
            return msg
        elif msg_type == "HumanMessage":
            return HumanMessage(content=content)
        elif msg_type == "SystemMessage":
            return SystemMessage(content=content)
        elif msg_type == "ToolMessage":
            return ToolMessage(
                content=content,
                tool_call_id=data.get("tool_call_id", ""),
                name=data.get("name", "")
            )
        else:
            return HumanMessage(content=content)

    # --- L1 / L2 cache helpers (kept the same shape; per-user-scoped) ---

    def _get_cached_messages(self, user_id: str, chat_id: str) -> Optional[List[BaseMessage]]:
        return self._message_cache.get(_cache_key(user_id, chat_id))

    def _cache_messages(self, user_id: str, chat_id: str, messages: List[BaseMessage]) -> None:
        self._message_cache.put(_cache_key(user_id, chat_id), messages.copy())

    def _invalidate_cache(self, user_id: str, chat_id: str) -> None:
        key = _cache_key(user_id, chat_id)
        self._message_cache.remove(key)
        self._metadata_cache.remove(key)
        self._chat_list_cache.pop(user_id, None)

    # --- Public message API ---

    async def exists(self, user_id: str, chat_id: str) -> bool:
        cached_messages = self._get_cached_messages(user_id, chat_id)
        if cached_messages is not None and len(cached_messages) > 0:
            return True
        async with self.pool.acquire() as conn:
            result = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM conversations WHERE user_id = $1 AND chat_id = $2)",
                user_id, chat_id
            )
            self._db_operations += 1
            return result

    async def get_messages(
        self, user_id: str, chat_id: str, limit: Optional[int] = None
    ) -> List[BaseMessage]:
        """Retrieve messages, scoped to the owning user.

        Returns [] if the chat doesn't exist or belongs to a different user.
        Reads from the row-per-message ``messages`` table; ``limit`` (if
        provided) returns the most recent N messages.
        """
        cached_messages = self._get_cached_messages(user_id, chat_id)
        if cached_messages is not None:
            return cached_messages[-limit:] if limit else cached_messages

        from cache import redis_cache
        l2 = await redis_cache.get_json("messages", _cache_key(user_id, chat_id))
        if l2 is not None:
            messages = [self._dict_to_message(d) for d in l2]
            self._cache_messages(user_id, chat_id, messages)
            return messages[-limit:] if limit else messages

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT role, content, tool_calls, tool_call_id, name, position
                FROM messages
                WHERE user_id = $1 AND chat_id = $2
                ORDER BY position ASC
                """,
                user_id, chat_id
            )
            self._db_operations += 1
            messages = [_row_to_message(dict(r)) for r in rows]

            self._cache_messages(user_id, chat_id, messages)
            serialized = [self._message_to_dict(m) for m in messages]
            await redis_cache.set_json(
                "messages", _cache_key(user_id, chat_id), serialized
            )
            return messages[-limit:] if limit else messages

    async def create_empty_chat(self, user_id: str, chat_id: str) -> None:
        """Insert an empty conversations row owned by ``user_id``.

        Used when a fresh chat is created via the API before any messages
        exist. The conversations row is required so chat_metadata + messages
        FKs can reference it.
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO conversations (chat_id, user_id, messages, message_count)
                VALUES ($1, $2, '[]'::jsonb, 0)
                ON CONFLICT (chat_id) DO NOTHING
                """,
                chat_id, user_id,
            )
            self._db_operations += 1
        self._cache_messages(user_id, chat_id, [])
        self._chat_list_cache.pop(user_id, None)
        from cache import redis_cache
        await redis_cache.set_json("messages", _cache_key(user_id, chat_id), [])
        await redis_cache.delete("chats_list", user_id)

    async def append_messages_to_chat(
        self, user_id: str, chat_id: str, new_messages: List[BaseMessage]
    ) -> List[BaseMessage]:
        """Append ``new_messages`` to ``chat_id`` for ``user_id``.

        Creates the conversations row if it doesn't already exist. Inserts
        rows at ``position = MAX(position) + 1, +2, ...`` inside a single
        transaction. Returns the **combined** message list (existing +
        new) so callers can pass it through to the SSE response without
        re-reading from the DB.
        """
        if not new_messages:
            return await self.get_messages(user_id, chat_id)

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Ensure conversations row exists (and confirm ownership if it
                # already does — INSERT … DO NOTHING is a no-op on conflict,
                # so a wrong-owner write silently fails).
                await conn.execute(
                    """
                    INSERT INTO conversations (chat_id, user_id, messages, message_count)
                    VALUES ($1, $2, '[]'::jsonb, 0)
                    ON CONFLICT (chat_id) DO NOTHING
                    """,
                    chat_id, user_id,
                )
                owner = await conn.fetchval(
                    "SELECT user_id FROM conversations WHERE chat_id = $1",
                    chat_id,
                )
                if owner != user_id:
                    logger.warning(
                        f"append_messages rejected: user={user_id} does not "
                        f"own chat={chat_id} (owner={owner})"
                    )
                    return await self.get_messages(user_id, chat_id)

                next_pos = await conn.fetchval(
                    "SELECT COALESCE(MAX(position), -1) + 1 FROM messages "
                    "WHERE chat_id = $1",
                    chat_id,
                )
                rows = []
                for offset, m in enumerate(new_messages):
                    rows.append((
                        chat_id,
                        user_id,
                        next_pos + offset,
                        _message_role(m),
                        m.content if isinstance(m.content, str) else json.dumps(m.content),
                        json.dumps(m.tool_calls) if (
                            isinstance(m, AIMessage) and getattr(m, "tool_calls", None)
                        ) else None,
                        getattr(m, "tool_call_id", None) if isinstance(m, ToolMessage) else None,
                        getattr(m, "name", None) if isinstance(m, ToolMessage) else None,
                    ))
                await conn.executemany(
                    """
                    INSERT INTO messages
                        (chat_id, user_id, position, role, content,
                         tool_calls, tool_call_id, name)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    """,
                    rows,
                )
                await conn.execute(
                    """
                    UPDATE conversations
                    SET message_count = message_count + $2,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE chat_id = $1
                    """,
                    chat_id, len(new_messages),
                )
                self._db_operations += 1

        # Update L1: cached_list = cached_list + new_messages.
        # Falls back to a full reload if the cache was cold.
        cached = self._get_cached_messages(user_id, chat_id)
        if cached is not None:
            combined = cached + list(new_messages)
        else:
            combined = await self._reload_messages_from_db(user_id, chat_id)
        self._cache_messages(user_id, chat_id, combined)
        self._chat_list_cache.pop(user_id, None)

        from cache import redis_cache
        serialized = [self._message_to_dict(m) for m in combined]
        await redis_cache.set_json(
            "messages", _cache_key(user_id, chat_id), serialized
        )
        await redis_cache.delete("chats_list", user_id)
        return combined

    async def _reload_messages_from_db(
        self, user_id: str, chat_id: str
    ) -> List[BaseMessage]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT role, content, tool_calls, tool_call_id, name, position
                FROM messages
                WHERE user_id = $1 AND chat_id = $2
                ORDER BY position ASC
                """,
                user_id, chat_id,
            )
            self._db_operations += 1
            return [_row_to_message(dict(r)) for r in rows]

    async def delete_conversation(self, user_id: str, chat_id: str) -> bool:
        """Delete a conversation owned by this user. CASCADE drops messages."""
        try:
            async with self.pool.acquire() as conn:
                result = await conn.execute(
                    "DELETE FROM conversations WHERE user_id = $1 AND chat_id = $2",
                    user_id, chat_id
                )
                self._db_operations += 1

                self._invalidate_cache(user_id, chat_id)

                from cache import redis_cache
                await redis_cache.delete("messages", _cache_key(user_id, chat_id))
                await redis_cache.delete("partial", _cache_key(user_id, chat_id))
                await redis_cache.delete("chats_list", user_id)

                return "DELETE 1" in result
        except Exception as e:
            logger.error(f"Error deleting conversation {chat_id} for user {user_id}: {e}")
            return False

    async def list_conversations(self, user_id: str) -> List[str]:
        """List the caller's conversation IDs, newest first."""
        cached_entry = self._chat_list_cache.get(user_id)
        if cached_entry and not cached_entry.is_expired():
            return cached_entry.data

        from cache import redis_cache
        l2 = await redis_cache.get_json("chats_list", user_id)
        if l2 is not None:
            self._chat_list_cache[user_id] = CacheEntry(
                data=l2, timestamp=time.time(), ttl=60
            )
            return l2

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT chat_id FROM conversations WHERE user_id = $1 "
                "ORDER BY updated_at DESC",
                user_id,
            )
            self._db_operations += 1

            chat_ids = [row['chat_id'] for row in rows]

            self._chat_list_cache[user_id] = CacheEntry(
                data=chat_ids, timestamp=time.time(), ttl=60
            )
            await redis_cache.set_json(
                "chats_list", user_id, chat_ids, ttl_seconds=60
            )

            return chat_ids

    async def get_chat_metadata(
        self, user_id: str, chat_id: str
    ) -> Optional[Dict]:
        cache_key = _cache_key(user_id, chat_id)
        cached = self._metadata_cache.get(cache_key)
        if cached is not None:
            return cached

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT name, created_at FROM chat_metadata "
                "WHERE user_id = $1 AND chat_id = $2",
                user_id, chat_id
            )
            self._db_operations += 1

            if row:
                metadata = {
                    "name": row['name'],
                    "created_at": row['created_at'].isoformat()
                }
            else:
                metadata = {"name": f"Chat {chat_id[:8]}"}

            self._metadata_cache.put(cache_key, metadata)
            return metadata

    async def set_chat_metadata(
        self, user_id: str, chat_id: str, name: str
    ) -> None:
        """Set chat metadata. Only writes if the conversation is owned by this user."""
        async with self.pool.acquire() as conn:
            owned = await conn.fetchval(
                "SELECT EXISTS("
                "SELECT 1 FROM conversations WHERE user_id = $1 AND chat_id = $2"
                ")",
                user_id, chat_id,
            )
            if not owned:
                logger.warning(
                    f"set_chat_metadata rejected: user={user_id} does not own chat={chat_id}"
                )
                return

            await conn.execute("""
                INSERT INTO chat_metadata (chat_id, user_id, name)
                VALUES ($1, $2, $3)
                ON CONFLICT (chat_id)
                DO UPDATE SET
                    name = EXCLUDED.name,
                    updated_at = CURRENT_TIMESTAMP
                WHERE chat_metadata.user_id = EXCLUDED.user_id
            """, chat_id, user_id, name)
            self._db_operations += 1

        self._metadata_cache.put(_cache_key(user_id, chat_id), {"name": name})

    async def _cache_eviction_worker(self) -> None:
        while True:
            try:
                await asyncio.sleep(60)
                msg_evicted = self._message_cache.evict_expired()
                meta_evicted = self._metadata_cache.evict_expired()
                expired_users = [
                    uid for uid, entry in self._chat_list_cache.items()
                    if entry.is_expired()
                ]
                for uid in expired_users:
                    del self._chat_list_cache[uid]
                if msg_evicted or meta_evicted or expired_users:
                    logger.debug(
                        f"Cache eviction: {msg_evicted} messages, "
                        f"{meta_evicted} metadata, "
                        f"{len(expired_users)} chat-list entries expired"
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in cache eviction worker: {e}")

    def get_cache_stats(self) -> Dict[str, Any]:
        total_hits = self._message_cache.hits + self._metadata_cache.hits
        total_misses = self._message_cache.misses + self._metadata_cache.misses
        total = total_hits + total_misses
        hit_rate = (total_hits / total * 100) if total > 0 else 0

        return {
            "cache_hits": total_hits,
            "cache_misses": total_misses,
            "hit_rate_percent": round(hit_rate, 2),
            "db_operations": self._db_operations,
            "cached_conversations": len(self._message_cache),
            "cached_metadata": len(self._metadata_cache),
        }

    # ------------------------------------------------------------------
    # Document source management (user_id + visibility)
    # ------------------------------------------------------------------

    async def add_document_source(
        self,
        source_name: str,
        user_id: str,
        visibility: str = "private",
        file_path: Optional[str] = None,
        task_id: Optional[str] = None,
        chunk_count: int = 0,
    ) -> None:
        if visibility not in ("public", "private"):
            raise ValueError(f"Invalid visibility: {visibility!r}")
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO document_sources
                    (source_name, user_id, visibility, file_path, task_id, chunk_count)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (source_name)
                DO UPDATE SET
                    user_id = EXCLUDED.user_id,
                    visibility = EXCLUDED.visibility,
                    file_path = COALESCE(EXCLUDED.file_path, document_sources.file_path),
                    task_id = COALESCE(EXCLUDED.task_id, document_sources.task_id),
                    chunk_count = EXCLUDED.chunk_count,
                    updated_at = CURRENT_TIMESTAMP
            """, source_name, user_id, visibility, file_path, task_id, chunk_count)
            self._db_operations += 1

    async def get_visible_sources(self, user_id: str) -> List[Dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT source_name, user_id, visibility, chunk_count, created_at
                FROM document_sources
                WHERE visibility = 'public' OR user_id = $1
                ORDER BY created_at DESC
            """, user_id)
            self._db_operations += 1
            result = []
            for row in rows:
                if row['user_id'] == user_id:
                    ownership = "yours"
                else:
                    ownership = "public"
                result.append({
                    "source_name": row['source_name'],
                    "ownership": ownership,
                    "chunk_count": row['chunk_count'],
                    "created_at": (
                        row['created_at'].isoformat()
                        if row['created_at']
                        else None
                    ),
                })
            return result

    async def get_visible_source_names(self, user_id: str) -> List[str]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT source_name FROM document_sources
                WHERE visibility = 'public' OR user_id = $1
                ORDER BY created_at DESC
            """, user_id)
            self._db_operations += 1
            return [row['source_name'] for row in rows]

    async def delete_document_source(
        self, source_name: str, user_id: str
    ) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM document_sources "
                "WHERE source_name = $1 AND user_id = $2",
                source_name, user_id,
            )
            self._db_operations += 1
            return "DELETE 1" in result

    async def source_is_visible_to(
        self, source_name: str, user_id: str
    ) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.fetchval("""
                SELECT EXISTS(
                    SELECT 1 FROM document_sources
                    WHERE source_name = $1
                      AND (visibility = 'public' OR user_id = $2)
                )
            """, source_name, user_id)
            self._db_operations += 1
            return result

    # ------------------------------------------------------------------
    # Per-user preferences (selected_sources, current_chat_id)
    # ------------------------------------------------------------------

    async def get_user_preferences(self, user_id: str) -> Dict[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT selected_sources, current_chat_id "
                "FROM user_preferences WHERE user_id = $1",
                user_id,
            )
            self._db_operations += 1
            if row is None:
                return {"selected_sources": [], "current_chat_id": None}
            sel = row['selected_sources']
            if isinstance(sel, str):
                sel = json.loads(sel)
            return {
                "selected_sources": sel or [],
                "current_chat_id": row['current_chat_id'],
            }

    async def update_user_selected_sources(
        self, user_id: str, selected_sources: List[str]
    ) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO user_preferences (user_id, selected_sources)
                VALUES ($1, $2)
                ON CONFLICT (user_id) DO UPDATE SET
                    selected_sources = EXCLUDED.selected_sources,
                    updated_at = CURRENT_TIMESTAMP
            """, user_id, json.dumps(selected_sources))
            self._db_operations += 1

    async def update_user_current_chat_id(
        self, user_id: str, chat_id: Optional[str]
    ) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO user_preferences (user_id, current_chat_id)
                VALUES ($1, $2)
                ON CONFLICT (user_id) DO UPDATE SET
                    current_chat_id = EXCLUDED.current_chat_id,
                    updated_at = CURRENT_TIMESTAMP
            """, user_id, chat_id)
            self._db_operations += 1
