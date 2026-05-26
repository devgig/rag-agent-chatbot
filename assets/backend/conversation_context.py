"""In-memory conversation buffer with intent-drift detection.

Stores per-chat turn history (user query + assistant response, never RAG
chunks) in an LRU cache.  On cold start or eviction the buffer is
repopulated from Postgres transparently.

The structural design choice — excluding prior RAG chunks from history —
is the primary defense against generation contamination: the model can
reference what it previously said, but cannot re-use stale document
evidence because it simply isn't in the context window.
"""

import math
import os
from collections import OrderedDict
from typing import Optional

from logger import logger

MAX_CHATS = int(os.getenv("CONTEXT_BUFFER_MAX_CHATS", "500"))
MAX_TURNS_PER_CHAT = int(os.getenv("CONTEXT_BUFFER_MAX_TURNS", "20"))
MAX_HISTORY_TOKENS = int(os.getenv("MAX_HISTORY_TOKENS", "4096"))
OUTPUT_RESERVE_TOKENS = int(os.getenv("OUTPUT_RESERVE_TOKENS", "2048"))
INTENT_DRIFT_THRESHOLD = float(os.getenv("INTENT_DRIFT_THRESHOLD", "0.3"))

_encoder = None


def _get_encoder():
    global _encoder
    if _encoder is not None:
        return _encoder
    try:
        import tiktoken
        _encoder = tiktoken.get_encoding("cl100k_base")
    except Exception:
        _encoder = None
    return _encoder


def count_tokens(text: str) -> int:
    enc = _get_encoder()
    if enc is not None:
        return len(enc.encode(text))
    return len(text) // 4 + 1


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(y * y for y in b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def select_history_window(
    history: list[dict],
    max_tokens: int,
) -> list[dict]:
    """Return the most recent complete turn-pairs that fit within *max_tokens*.

    Walks backward from newest.  A turn-pair is (user, assistant); we
    never split a pair so the model always sees the question that led to
    each answer.
    """
    if not history or max_tokens <= 0:
        return []

    pairs: list[tuple[dict, dict]] = []
    i = len(history) - 1
    while i >= 1:
        if history[i]["role"] == "assistant" and history[i - 1]["role"] == "user":
            pairs.append((history[i - 1], history[i]))
            i -= 2
        else:
            i -= 1

    budget = max_tokens
    selected: list[tuple[dict, dict]] = []
    for user_msg, asst_msg in pairs:
        cost = count_tokens(user_msg["content"]) + count_tokens(asst_msg["content"])
        if cost > budget:
            break
        budget -= cost
        selected.append((user_msg, asst_msg))

    selected.reverse()
    result: list[dict] = []
    for user_msg, asst_msg in selected:
        result.append(user_msg)
        result.append(asst_msg)
    return result


class ConversationBuffer:
    """Per-chat LRU cache of recent conversation turns.

    Thread-safety note: the singleton ChatAgent is shared across async
    requests, but the asyncio event loop is single-threaded so dict
    operations within a single coroutine are safe.  The Redis stream-slot
    cap (MAX_STREAMS_PER_USER) further prevents concurrent streams on the
    same chat.
    """

    def __init__(
        self,
        max_chats: int = MAX_CHATS,
        max_turns: int = MAX_TURNS_PER_CHAT,
    ):
        self._buffers: OrderedDict[str, list[dict]] = OrderedDict()
        self._query_embeddings: dict[str, list[float]] = {}
        self._max_chats = max_chats
        self._max_turns = max_turns

    def get(self, chat_id: str) -> Optional[list[dict]]:
        """Return buffered messages or None (signals caller to load from Postgres)."""
        if chat_id not in self._buffers:
            return None
        self._buffers.move_to_end(chat_id)
        return list(self._buffers[chat_id])

    def append(self, chat_id: str, user_content: str, assistant_content: str) -> None:
        """Append a complete turn (user + assistant)."""
        if chat_id not in self._buffers:
            self._buffers[chat_id] = []
        buf = self._buffers[chat_id]
        buf.append({"role": "user", "content": user_content})
        buf.append({"role": "assistant", "content": assistant_content})
        if len(buf) > self._max_turns:
            excess = len(buf) - self._max_turns
            del buf[:excess]
        self._buffers.move_to_end(chat_id)
        self._evict()

    def set_from_db(self, chat_id: str, messages: list[dict]) -> None:
        """Populate buffer from a Postgres history load."""
        trimmed = messages[-self._max_turns:] if len(messages) > self._max_turns else list(messages)
        self._buffers[chat_id] = trimmed
        self._buffers.move_to_end(chat_id)
        self._evict()

    def set_query_embedding(self, chat_id: str, embedding: list[float]) -> None:
        self._query_embeddings[chat_id] = embedding

    def get_query_embedding(self, chat_id: str) -> Optional[list[float]]:
        return self._query_embeddings.get(chat_id)

    def clear(self, chat_id: str) -> None:
        self._buffers.pop(chat_id, None)
        self._query_embeddings.pop(chat_id, None)

    def _evict(self) -> None:
        while len(self._buffers) > self._max_chats:
            evicted_id, _ = self._buffers.popitem(last=False)
            self._query_embeddings.pop(evicted_id, None)
            logger.debug({"message": "conversation_buffer_evict", "chat_id": evicted_id})
