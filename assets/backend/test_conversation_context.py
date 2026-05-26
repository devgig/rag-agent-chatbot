"""Tests for conversation_context module."""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

import pytest
from conversation_context import (
    ConversationBuffer,
    cosine_similarity,
    count_tokens,
    select_history_window,
    INTENT_DRIFT_THRESHOLD,
)


class TestCosine:
    def test_identical_vectors(self):
        v = [1.0, 0.0, 0.0]
        assert cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        a = [1.0, 0.0, 0.0]
        b = [0.0, 1.0, 0.0]
        assert cosine_similarity(a, b) == pytest.approx(0.0)

    def test_opposite_vectors(self):
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        assert cosine_similarity(a, b) == pytest.approx(-1.0)

    def test_zero_vector(self):
        a = [0.0, 0.0, 0.0]
        b = [1.0, 2.0, 3.0]
        assert cosine_similarity(a, b) == 0.0

    def test_realistic_384dim(self):
        import math
        a = [math.sin(i) for i in range(384)]
        b = [math.sin(i + 0.1) for i in range(384)]
        sim = cosine_similarity(a, b)
        assert 0.9 < sim < 1.0


class TestCountTokens:
    def test_empty_string(self):
        assert count_tokens("") >= 0

    def test_simple_sentence(self):
        tokens = count_tokens("Hello, world!")
        assert tokens > 0 and tokens < 20

    def test_longer_text(self):
        text = "This is a longer sentence that should have more tokens than a short one."
        assert count_tokens(text) > count_tokens("short")

    def test_fallback_when_tiktoken_unavailable(self):
        """The char/4 fallback should give a reasonable estimate."""
        import conversation_context as cc
        orig = cc._encoder
        try:
            cc._encoder = None
            # Monkey-patch to force fallback
            import types
            old_get = cc._get_encoder
            cc._get_encoder = lambda: None
            result = cc.count_tokens("Hello, world!")
            assert result > 0
            assert result == len("Hello, world!") // 4 + 1
        finally:
            cc._encoder = orig
            cc._get_encoder = old_get


class TestSelectHistoryWindow:
    def _make_history(self, n_pairs: int) -> list[dict]:
        """Create n user/assistant pairs."""
        result = []
        for i in range(n_pairs):
            result.append({"role": "user", "content": f"Question {i}"})
            result.append({"role": "assistant", "content": f"Answer {i}"})
        return result

    def test_empty_history(self):
        assert select_history_window([], 1000) == []

    def test_zero_budget(self):
        history = self._make_history(3)
        assert select_history_window(history, 0) == []

    def test_returns_complete_pairs(self):
        history = self._make_history(5)
        result = select_history_window(history, 10000)
        assert len(result) % 2 == 0
        for i in range(0, len(result), 2):
            assert result[i]["role"] == "user"
            assert result[i + 1]["role"] == "assistant"

    def test_respects_token_budget(self):
        history = self._make_history(50)
        result = select_history_window(history, 50)
        total = sum(count_tokens(m["content"]) for m in result)
        assert total <= 50

    def test_most_recent_first(self):
        history = self._make_history(5)
        result = select_history_window(history, 10000)
        assert result[-2]["content"] == "Question 4"
        assert result[-1]["content"] == "Answer 4"

    def test_single_pair_too_large(self):
        history = [
            {"role": "user", "content": "x" * 10000},
            {"role": "assistant", "content": "y" * 10000},
        ]
        result = select_history_window(history, 10)
        assert result == []


class TestConversationBuffer:
    def test_get_returns_none_for_unknown(self):
        buf = ConversationBuffer(max_chats=10, max_turns=10)
        assert buf.get("unknown-id") is None

    def test_append_and_get(self):
        buf = ConversationBuffer(max_chats=10, max_turns=10)
        buf.append("chat1", "hello", "hi there")
        history = buf.get("chat1")
        assert history == [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]

    def test_multiple_appends(self):
        buf = ConversationBuffer(max_chats=10, max_turns=20)
        buf.append("chat1", "q1", "a1")
        buf.append("chat1", "q2", "a2")
        history = buf.get("chat1")
        assert len(history) == 4
        assert history[0]["content"] == "q1"
        assert history[3]["content"] == "a2"

    def test_max_turns_trims_oldest(self):
        buf = ConversationBuffer(max_chats=10, max_turns=4)
        buf.append("chat1", "q1", "a1")
        buf.append("chat1", "q2", "a2")
        buf.append("chat1", "q3", "a3")
        history = buf.get("chat1")
        assert len(history) == 4
        assert history[0]["content"] == "q2"
        assert history[3]["content"] == "a3"

    def test_lru_eviction(self):
        buf = ConversationBuffer(max_chats=2, max_turns=10)
        buf.append("chat1", "q1", "a1")
        buf.append("chat2", "q2", "a2")
        buf.append("chat3", "q3", "a3")
        assert buf.get("chat1") is None
        assert buf.get("chat2") is not None
        assert buf.get("chat3") is not None

    def test_lru_access_refreshes(self):
        buf = ConversationBuffer(max_chats=2, max_turns=10)
        buf.append("chat1", "q1", "a1")
        buf.append("chat2", "q2", "a2")
        buf.get("chat1")  # refresh chat1
        buf.append("chat3", "q3", "a3")  # should evict chat2
        assert buf.get("chat1") is not None
        assert buf.get("chat2") is None

    def test_set_from_db(self):
        buf = ConversationBuffer(max_chats=10, max_turns=10)
        db_history = [
            {"role": "user", "content": "old q"},
            {"role": "assistant", "content": "old a"},
        ]
        buf.set_from_db("chat1", db_history)
        history = buf.get("chat1")
        assert history == db_history

    def test_query_embedding_roundtrip(self):
        buf = ConversationBuffer(max_chats=10, max_turns=10)
        emb = [0.1, 0.2, 0.3]
        buf.set_query_embedding("chat1", emb)
        assert buf.get_query_embedding("chat1") == emb
        assert buf.get_query_embedding("unknown") is None

    def test_clear(self):
        buf = ConversationBuffer(max_chats=10, max_turns=10)
        buf.append("chat1", "q", "a")
        buf.set_query_embedding("chat1", [1.0])
        buf.clear("chat1")
        assert buf.get("chat1") is None
        assert buf.get_query_embedding("chat1") is None

    def test_get_returns_copy(self):
        buf = ConversationBuffer(max_chats=10, max_turns=10)
        buf.append("chat1", "q", "a")
        history = buf.get("chat1")
        history.append({"role": "user", "content": "injected"})
        assert len(buf.get("chat1")) == 2


class TestIntentDrift:
    def test_threshold_is_reasonable(self):
        assert 0.1 < INTENT_DRIFT_THRESHOLD < 0.8
