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

User-scoped: every conversation, chat metadata row, and user preference is
keyed by the JWT ``sub`` (an email string). Cross-user reads/writes are not
possible through the public API — every method takes ``user_id`` and every
query filters on it.

Document sources are scoped by ``(user_id, visibility)``: legacy chunks
uploaded before user-scoping have ``user_id = NULL`` and ``visibility =
'public'`` and remain visible to all users. New uploads carry the uploader
in ``user_id`` and either ``visibility = 'public'`` (visible to everyone)
or ``visibility = 'private'`` (visible only to the uploader).
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
    """Build a user-scoped cache key. Email + chat UUID, separated by `|`
    (which neither value can contain) to avoid ambiguity."""
    return f"{user_id}|{chat_id}"


class PostgreSQLConversationStorage:
    """PostgreSQL-based conversation storage with LRU caching and I/O optimization."""

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

        # All caches now keyed by `user_id|chat_id` to prevent cross-user reads.
        self._message_cache = LRUCache(max_size=MAX_CACHE_ENTRIES, default_ttl=cache_ttl)
        self._metadata_cache = LRUCache(max_size=MAX_CACHE_ENTRIES, default_ttl=cache_ttl)
        # Per-user chat list cache: user_id -> CacheEntry(list[str])
        self._chat_list_cache: Dict[str, CacheEntry] = {}

        self._pending_saves: Dict[str, List[BaseMessage]] = {}
        self._save_lock = asyncio.Lock()
        self._batch_save_task: Optional[asyncio.Task] = None
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

                self._batch_save_task = asyncio.create_task(self._batch_save_worker())
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
        for task in (self._batch_save_task, self._cache_eviction_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Flush remaining pending saves before closing
        if self._pending_saves and self.pool:
            try:
                async with self._save_lock:
                    saves = self._pending_saves.copy()
                    self._pending_saves.clear()
                async with self.pool.acquire() as conn:
                    async with conn.transaction():
                        for key, messages in saves.items():
                            user_id, chat_id = key.split("|", 1)
                            serialized = [self._message_to_dict(msg) for msg in messages]
                            await conn.execute("""
                                INSERT INTO conversations (chat_id, user_id, messages, message_count)
                                VALUES ($1, $2, $3, $4)
                                ON CONFLICT (chat_id)
                                DO UPDATE SET messages = EXCLUDED.messages,
                                    message_count = EXCLUDED.message_count,
                                    updated_at = CURRENT_TIMESTAMP
                                WHERE conversations.user_id = EXCLUDED.user_id
                            """, chat_id, user_id, json.dumps(serialized), len(messages))
            except Exception as e:
                logger.error(f"Error flushing pending saves on shutdown: {e}")

        if self.pool:
            await self.pool.close()
            logger.debug("PostgreSQL connection pool closed")

    async def _create_tables(self) -> None:
        """Create / migrate schema.

        Uses a Postgres advisory lock so only one pod runs DDL at a time
        during rolling deploys. The migration is idempotent: if user_id
        columns are already present, the migration is a no-op.

        Chat data (conversations + chat_metadata) is DROPPED on initial
        migration — dev decision documented in the PR description. Document
        sources are preserved and the existing rows are treated as
        ``visibility = 'public'``, ``user_id = NULL``.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Serialize migrations across pods
                await conn.execute(
                    "SELECT pg_advisory_xact_lock($1)",
                    _MIGRATION_ADVISORY_LOCK_KEY,
                )

                # --- Migration 1: chat tables become user-scoped ---
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
                        messages JSONB NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        message_count INTEGER DEFAULT 0
                    )
                """)

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

                # --- Migration 2: document_sources gets user_id + visibility ---
                # NULL user_id + visibility='public' is the legacy default so existing
                # chunks remain visible to all users.
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

                # --- Migration 3: per-user preferences ---
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS user_preferences (
                        user_id TEXT PRIMARY KEY,
                        selected_sources JSONB NOT NULL DEFAULT '[]'::jsonb,
                        current_chat_id VARCHAR(255),
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                # --- Images: unchanged ---
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

                # Trigger to keep updated_at fresh
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

    def _message_to_dict(self, message: BaseMessage) -> Dict:
        """Convert a message object to a dictionary for storage."""
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
        """Convert a dictionary back to a message object."""
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

    def _get_cached_messages(self, user_id: str, chat_id: str) -> Optional[List[BaseMessage]]:
        """Get messages from LRU cache if available and not expired."""
        return self._message_cache.get(_cache_key(user_id, chat_id))

    def _cache_messages(self, user_id: str, chat_id: str, messages: List[BaseMessage]) -> None:
        """Cache messages in LRU cache."""
        self._message_cache.put(_cache_key(user_id, chat_id), messages.copy())

    def _invalidate_cache(self, user_id: str, chat_id: str) -> None:
        """Invalidate cache entries for a chat."""
        key = _cache_key(user_id, chat_id)
        self._message_cache.remove(key)
        self._metadata_cache.remove(key)
        self._chat_list_cache.pop(user_id, None)

    async def exists(self, user_id: str, chat_id: str) -> bool:
        """Check if a conversation exists and is owned by this user."""
        cached_messages = self._get_cached_messages(user_id, chat_id)
        if cached_messages is not None:
            return len(cached_messages) > 0

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
        """Retrieve messages for a chat session, scoped to the owning user.

        Returns [] if the chat doesn't exist or belongs to a different user —
        same response either way so we don't leak existence.
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
            row = await conn.fetchrow(
                "SELECT messages FROM conversations WHERE user_id = $1 AND chat_id = $2",
                user_id, chat_id
            )
            self._db_operations += 1

            if not row:
                return []

            messages_data = row['messages']
            if isinstance(messages_data, str):
                messages_data = json.loads(messages_data)
            messages = [self._dict_to_message(msg_data) for msg_data in messages_data]

            self._cache_messages(user_id, chat_id, messages)
            await redis_cache.set_json(
                "messages", _cache_key(user_id, chat_id), messages_data
            )

            return messages[-limit:] if limit else messages

    async def save_messages(
        self, user_id: str, chat_id: str, messages: List[BaseMessage]
    ) -> None:
        """Save messages with batching for performance."""
        async with self._save_lock:
            self._pending_saves[_cache_key(user_id, chat_id)] = messages.copy()

        self._cache_messages(user_id, chat_id, messages)

    async def save_messages_immediate(
        self, user_id: str, chat_id: str, messages: List[BaseMessage]
    ) -> None:
        """Save messages immediately without batching.

        Uses INSERT ... ON CONFLICT, but the WHERE clause on the UPDATE
        protects against a different user trying to write to an existing
        chat_id (which would only happen on UUID collision or attempted
        squatting). On mismatch the UPDATE is silently skipped — the user
        observes the operation as a no-op and we don't leak ownership.
        """
        serialized_messages = [self._message_to_dict(msg) for msg in messages]

        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO conversations (chat_id, user_id, messages, message_count)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (chat_id)
                DO UPDATE SET
                    messages = EXCLUDED.messages,
                    message_count = EXCLUDED.message_count,
                    updated_at = CURRENT_TIMESTAMP
                WHERE conversations.user_id = EXCLUDED.user_id
            """, chat_id, user_id, json.dumps(serialized_messages), len(messages))
            self._db_operations += 1

        self._cache_messages(user_id, chat_id, messages)
        self._chat_list_cache.pop(user_id, None)

        from cache import redis_cache
        await redis_cache.set_json(
            "messages", _cache_key(user_id, chat_id), serialized_messages
        )
        await redis_cache.delete("chats_list", user_id)

    async def _batch_save_worker(self) -> None:
        """Background worker to batch save operations."""
        while True:
            try:
                await asyncio.sleep(1.0)

                async with self._save_lock:
                    if not self._pending_saves:
                        continue

                    saves_to_process = self._pending_saves.copy()
                    self._pending_saves.clear()

                async with self.pool.acquire() as conn:
                    async with conn.transaction():
                        for key, messages in saves_to_process.items():
                            user_id, chat_id = key.split("|", 1)
                            serialized_messages = [self._message_to_dict(msg) for msg in messages]

                            await conn.execute("""
                                INSERT INTO conversations (chat_id, user_id, messages, message_count)
                                VALUES ($1, $2, $3, $4)
                                ON CONFLICT (chat_id)
                                DO UPDATE SET
                                    messages = EXCLUDED.messages,
                                    message_count = EXCLUDED.message_count,
                                    updated_at = CURRENT_TIMESTAMP
                                WHERE conversations.user_id = EXCLUDED.user_id
                            """, chat_id, user_id, json.dumps(serialized_messages), len(messages))

                self._db_operations += len(saves_to_process)
                if saves_to_process:
                    logger.debug(f"Batch saved {len(saves_to_process)} conversations")
                    # Invalidate per-user list caches
                    touched_users = set()
                    for key in saves_to_process:
                        touched_users.add(key.split("|", 1)[0])
                    for uid in touched_users:
                        self._chat_list_cache.pop(uid, None)

                    from cache import redis_cache
                    for key, messages in saves_to_process.items():
                        serialized = [self._message_to_dict(msg) for msg in messages]
                        await redis_cache.set_json("messages", key, serialized)
                    for uid in touched_users:
                        await redis_cache.delete("chats_list", uid)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in batch save worker: {e}")

    async def append_messages(
        self, user_id: str, chat_id: str, new_messages: List[BaseMessage]
    ) -> None:
        """Append messages to a conversation without re-fetching when cache is warm."""
        cached = self._get_cached_messages(user_id, chat_id)
        if cached is not None:
            updated = cached + new_messages
        else:
            existing = await self.get_messages(user_id, chat_id)
            updated = existing + new_messages
        await self.save_messages(user_id, chat_id, updated)

    async def add_message(self, user_id: str, chat_id: str, message: BaseMessage) -> None:
        """Add a single message to conversation (optimized)."""
        await self.append_messages(user_id, chat_id, [message])

    async def delete_conversation(self, user_id: str, chat_id: str) -> bool:
        """Delete a conversation owned by this user. Returns False if not owned."""
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
        """Get chat metadata if owned by this user, else fallback to default name."""
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
                # If the chat isn't owned by this user, this is what we return.
                # Don't leak existence-of-row signals to a different user.
                metadata = {"name": f"Chat {chat_id[:8]}"}

            self._metadata_cache.put(cache_key, metadata)
            return metadata

    async def set_chat_metadata(
        self, user_id: str, chat_id: str, name: str
    ) -> None:
        """Set chat metadata. Only writes if the conversation is owned by this user."""
        async with self.pool.acquire() as conn:
            # Verify ownership exists before inserting metadata. INSERT alone
            # would tie metadata to a chat without checking the conversations
            # row's owner.
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
        """Periodically evict expired cache entries to free memory."""
        while True:
            try:
                await asyncio.sleep(60)
                msg_evicted = self._message_cache.evict_expired()
                meta_evicted = self._metadata_cache.evict_expired()
                # Evict expired per-user list entries
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
        """Get cache performance statistics."""
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
        """Add or update a document source.

        ``visibility`` must be either ``"public"`` or ``"private"``.
        Public sources are visible to all users; private only to the uploader.
        """
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
        logger.debug(
            f"Added/updated document source: {source_name} "
            f"(user={user_id}, visibility={visibility})"
        )

    async def get_visible_sources(self, user_id: str) -> List[Dict]:
        """Return sources visible to this user: public + their own private.

        Each item: ``{source_name, ownership, chunk_count, created_at}``
        where ``ownership`` is one of ``"public"``, ``"yours"``.
        """
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
        """Return just the names of sources visible to this user."""
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
        """Delete a document source if owned by this user.

        Legacy (NULL user_id, public) sources cannot be deleted via this
        endpoint — they need admin tooling. Returns False on not-owned or
        not-found.
        """
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM document_sources "
                "WHERE source_name = $1 AND user_id = $2",
                source_name, user_id,
            )
            self._db_operations += 1
            deleted = "DELETE 1" in result
            if deleted:
                logger.debug(
                    f"Deleted document source: {source_name} (user={user_id})"
                )
            else:
                logger.warning(
                    f"Refused source deletion: user={user_id} not owner of "
                    f"source={source_name}"
                )
            return deleted

    async def source_is_visible_to(
        self, source_name: str, user_id: str
    ) -> bool:
        """Check if a source is visible to a given user (public or theirs)."""
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
        """Return per-user preferences. Creates a default row if missing."""
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
        """Persist this user's selected_sources list."""
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
        """Persist this user's last-active chat_id."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO user_preferences (user_id, current_chat_id)
                VALUES ($1, $2)
                ON CONFLICT (user_id) DO UPDATE SET
                    current_chat_id = EXCLUDED.current_chat_id,
                    updated_at = CURRENT_TIMESTAMP
            """, user_id, chat_id)
            self._db_operations += 1
