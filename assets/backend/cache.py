#
# SPDX-FileCopyrightText: Copyright (c) 1993-2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
"""Redis L2 cache and partial-response storage.

Pairs with the per-process L1 cache already in ``postgres_storage.py``.
The L2 layer is hit only on L1 miss and is shared across all backend pods,
so a user pinned to a different pod (or a fresh pod after a rollout) still
gets a warm-ish cache instead of falling all the way back to PostgreSQL.

Also stores partial LLM responses on cancel/disconnect with a TTL so a user
who reconnects sees what they had before the interruption.
"""

import os
from typing import Any, Optional

import orjson
import redis.asyncio as aioredis

from logger import logger


def _json_dumps(value: Any) -> bytes:
    """orjson serialize with a default for datetimes & other non-JSON types.

    Returns bytes — redis-py accepts bytes natively which avoids the
    bytes→str round-trip the stdlib json module forced. 3-5× faster on
    the conversation payloads we cache.
    """
    return orjson.dumps(value, default=str)


def _json_loads(raw: Any) -> Any:
    """orjson parse, accepting either str (decode_responses=True) or bytes."""
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    return orjson.loads(raw)

REDIS_HOST = os.getenv("REDIS_HOST", "redis.redis-system.svc.cluster.local")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD") or None
REDIS_DB = int(os.getenv("REDIS_DB", "0"))
REDIS_KEY_PREFIX = os.getenv("REDIS_KEY_PREFIX", "sparkchat")

# Default TTL for both regular L2 entries and partial responses. 8h is long
# enough that a user can come back after lunch and still see their chat,
# short enough that drafts don't accumulate forever.
CACHE_L2_TTL_SECONDS = int(os.getenv("CACHE_L2_TTL_SECONDS", str(8 * 3600)))


class RedisCache:
    """Async Redis adapter — JSON values, namespaced keys, soft-fail on errors.

    All methods catch Redis exceptions and log+return a miss. Redis is a
    performance optimization here, not a correctness requirement; if it's
    down, requests fall through to PostgreSQL and the app keeps working.
    """

    def __init__(self):
        self._client: Optional[aioredis.Redis] = None

    async def connect(self) -> None:
        """Create the Redis connection pool. Idempotent."""
        if self._client is not None:
            return
        try:
            self._client = aioredis.Redis(
                host=REDIS_HOST,
                port=REDIS_PORT,
                password=REDIS_PASSWORD,
                db=REDIS_DB,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
                health_check_interval=30,
            )
            await self._client.ping()
            logger.info(f"Redis L2 cache connected: {REDIS_HOST}:{REDIS_PORT} db={REDIS_DB}")
        except Exception as exc:
            logger.warning(f"Redis L2 cache unavailable, continuing without: {exc}")
            self._client = None

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception as exc:
                logger.warning(f"Error closing Redis client: {exc}")
            self._client = None

    def _key(self, namespace: str, key: str) -> str:
        return f"{REDIS_KEY_PREFIX}:{namespace}:{key}"

    async def get_json(self, namespace: str, key: str) -> Optional[Any]:
        if self._client is None:
            return None
        try:
            raw = await self._client.get(self._key(namespace, key))
            return _json_loads(raw)
        except Exception as exc:
            logger.warning(f"Redis GET failed (ns={namespace} key={key}): {exc}")
            return None

    async def set_json(
        self,
        namespace: str,
        key: str,
        value: Any,
        ttl_seconds: int = CACHE_L2_TTL_SECONDS,
    ) -> None:
        if self._client is None:
            return
        try:
            await self._client.set(
                self._key(namespace, key),
                _json_dumps(value),
                ex=ttl_seconds,
            )
        except Exception as exc:
            logger.warning(f"Redis SET failed (ns={namespace} key={key}): {exc}")

    async def delete(self, namespace: str, key: str) -> None:
        if self._client is None:
            return
        try:
            await self._client.delete(self._key(namespace, key))
        except Exception as exc:
            logger.warning(f"Redis DEL failed (ns={namespace} key={key}): {exc}")

    async def acquire_stream_slot(
        self, user_id: str, max_concurrent: int, ttl_seconds: int
    ) -> bool:
        """Atomically try to claim a per-user SSE stream slot.

        Returns True on success (caller may proceed). Returns False on cap
        hit (caller should reject the request with 429). Fails open (returns
        True) if Redis is unavailable — losing the cap is preferable to
        blocking all chat traffic during a Redis outage.

        Implementation note: there is a benign race between INCR and the
        conditional DECR. Under pile-on, the count may briefly overshoot
        ``max_concurrent`` by up to ``concurrent_request_count`` before
        each over-cap request decrements itself. Steady-state is bounded.
        The TTL ensures stale counters don't accumulate forever if a pod
        crashes mid-stream.
        """
        if self._client is None:
            return True
        try:
            key = self._key("streams", user_id)
            count = await self._client.incr(key)
            await self._client.expire(key, ttl_seconds)
            if count > max_concurrent:
                await self._client.decr(key)
                return False
            return True
        except Exception as exc:
            logger.warning(
                f"Redis acquire_stream_slot failed (user={user_id}): {exc} "
                "— failing open"
            )
            return True

    async def release_stream_slot(self, user_id: str) -> None:
        """Release a previously-claimed stream slot.

        Idempotent and best-effort: Redis errors are logged and swallowed.
        On rare double-release or missed-release, the TTL on the counter
        ensures we don't drift permanently — stale counts auto-decay.
        """
        if self._client is None:
            return
        try:
            key = self._key("streams", user_id)
            # Use DECR but floor at 0 to avoid negative counts if something
            # double-releases (e.g. after a Redis flush).
            new_count = await self._client.decr(key)
            if new_count < 0:
                await self._client.set(key, 0, xx=True)
        except Exception as exc:
            logger.warning(
                f"Redis release_stream_slot failed (user={user_id}): {exc}"
            )


# Module-level singleton — one Redis pool per backend process.
redis_cache = RedisCache()


# --- Partial-response storage ---
# A partial is the prefix of an LLM response that the user saw before they
# cancelled or got disconnected. Stored under the same TTL as the L2 cache
# so it's still visible if they reconnect within the window.

_PARTIAL_NS = "partial"


def _partial_key(user_id: str, chat_id: str) -> str:
    """User-scoped partial key. `|` is safe because neither value can contain it."""
    return f"{user_id}|{chat_id}"


async def save_partial_response(user_id: str, chat_id: str, content: str) -> None:
    """Persist a partial assistant response keyed by user + chat."""
    if not content:
        return
    await redis_cache.set_json(
        _PARTIAL_NS,
        _partial_key(user_id, chat_id),
        {"content": content, "truncated": True},
    )


async def get_partial_response(user_id: str, chat_id: str) -> Optional[dict]:
    """Return ``{'content': str, 'truncated': True}`` or ``None``."""
    return await redis_cache.get_json(_PARTIAL_NS, _partial_key(user_id, chat_id))


async def clear_partial_response(user_id: str, chat_id: str) -> None:
    """Remove any saved partial — call on stream completion or new query."""
    await redis_cache.delete(_PARTIAL_NS, _partial_key(user_id, chat_id))
