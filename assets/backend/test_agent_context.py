"""Integration tests for ChatAgent multi-turn context and intent drift."""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

# Mock heavy dependencies that aren't needed for unit-testing agent logic
from unittest.mock import MagicMock
sys.modules.setdefault("asyncpg", MagicMock())
sys.modules.setdefault("redis", MagicMock())
sys.modules.setdefault("redis.asyncio", MagicMock())
sys.modules.setdefault("orjson", MagicMock())
sys.modules.setdefault("prometheus_fastapi_instrumentator", MagicMock())

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass

import pytest

from conversation_context import ConversationBuffer, cosine_similarity


@dataclass
class FakeDoc:
    page_content: str
    metadata: dict


class FakeChoice:
    def __init__(self, content=None, finish_reason=None):
        self.delta = MagicMock()
        self.delta.content = content
        self.delta.tool_calls = []
        self.finish_reason = finish_reason


class FakeChunk:
    def __init__(self, content=None, finish_reason=None, usage=None):
        self.choices = [FakeChoice(content, finish_reason)] if content or finish_reason else []
        self.usage = usage


class FakeUsage:
    prompt_tokens = 100
    completion_tokens = 50
    total_tokens = 150


async def fake_stream(chunks):
    """Simulate an async iterable of chunks."""
    for chunk in chunks:
        yield chunk


class FakeStreamContext:
    """Wraps an async generator to behave like the OpenAI streaming response."""
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for c in self._chunks:
            yield c


@pytest.fixture
def mock_deps():
    """Set up mocked dependencies for ChatAgent."""
    vector_store = MagicMock()
    vector_store.embeddings = MagicMock()
    vector_store.get_documents = AsyncMock(return_value=[
        FakeDoc(page_content="Relevant document content.", metadata={"source": "test.pdf"})
    ])

    config_manager = MagicMock()
    config_manager.get_available_models.return_value = ["test-model"]
    config_manager.get_selected_model.return_value = "test-model"

    postgres_storage = MagicMock()
    postgres_storage.get_user_preferences = AsyncMock(return_value={"selected_sources": []})
    postgres_storage.get_messages = AsyncMock(return_value=[])
    postgres_storage.append_messages_to_chat = AsyncMock(return_value=[])

    return vector_store, config_manager, postgres_storage


@pytest.mark.asyncio
async def test_generate_includes_history_on_continuation(mock_deps):
    """When intent drift says 'continuation', history should appear in the LLM messages."""
    vector_store, config_manager, postgres_storage = mock_deps

    # Embeddings: return similar vectors for "continuation"
    base_emb = [0.5] * 384
    vector_store.embeddings.aembed_query = AsyncMock(return_value=base_emb)

    from agent import ChatAgent

    with patch.object(ChatAgent, 'set_current_model'):
        agent = ChatAgent(vector_store, config_manager, postgres_storage)
        agent.current_model = "test-model"
        agent.system_prompt_template = MagicMock()
        agent.system_prompt_template.render = MagicMock(return_value="System prompt")

        # Pre-populate buffer with history and a similar embedding
        agent.context_buffer.append("chat1", "What is Redpanda?", "Redpanda is a streaming platform.")
        agent.context_buffer.set_query_embedding("chat1", base_emb)

        # Mock LLM streaming response
        chunks = [
            FakeChunk(content="Test response"),
            FakeChunk(finish_reason="stop", usage=FakeUsage()),
        ]

        model_client = AsyncMock()
        model_client.chat.completions.create = AsyncMock(return_value=FakeStreamContext(chunks))
        agent.model_client = model_client

        captured_events = []
        agent.stream_callback = AsyncMock(side_effect=lambda e: captured_events.append(e))
        agent._usage_accumulator = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        from langchain_core.messages import HumanMessage
        state = {
            "chat_id": "chat1",
            "user_id": "user1",
            "messages": [HumanMessage(content="Tell me more about Redpanda")],
        }

        await agent.generate(state)

        # Verify the LLM call included history messages
        call_args = model_client.chat.completions.create.call_args
        messages = call_args.kwargs["messages"]

        roles = [m["role"] for m in messages]
        assert "system" in roles
        assert "user" in roles
        # History should be present (user + assistant from buffer)
        assert roles.count("user") == 2  # history user + current user
        assert "assistant" in roles


@pytest.mark.asyncio
async def test_generate_excludes_history_on_topic_shift(mock_deps):
    """When intent drift detects a topic change, history should be excluded."""
    vector_store, config_manager, postgres_storage = mock_deps

    # Return a very different embedding to trigger topic shift
    new_emb = [-0.5] * 384
    vector_store.embeddings.aembed_query = AsyncMock(return_value=new_emb)

    from agent import ChatAgent

    with patch.object(ChatAgent, 'set_current_model'):
        agent = ChatAgent(vector_store, config_manager, postgres_storage)
        agent.current_model = "test-model"
        agent.system_prompt_template = MagicMock()
        agent.system_prompt_template.render = MagicMock(return_value="System prompt")

        # Pre-populate buffer with history and an OPPOSITE embedding
        old_emb = [0.5] * 384
        agent.context_buffer.append("chat1", "What is Redpanda?", "Redpanda is a streaming platform.")
        agent.context_buffer.set_query_embedding("chat1", old_emb)

        # Verify the embeddings are actually dissimilar
        sim = cosine_similarity(new_emb, old_emb)
        assert sim < 0  # opposite vectors

        chunks = [
            FakeChunk(content="Pasta recipe"),
            FakeChunk(finish_reason="stop", usage=FakeUsage()),
        ]

        model_client = AsyncMock()
        model_client.chat.completions.create = AsyncMock(return_value=FakeStreamContext(chunks))
        agent.model_client = model_client

        captured_events = []
        agent.stream_callback = AsyncMock(side_effect=lambda e: captured_events.append(e))
        agent._usage_accumulator = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        from langchain_core.messages import HumanMessage
        state = {
            "chat_id": "chat1",
            "user_id": "user1",
            "messages": [HumanMessage(content="How do I make pasta?")],
        }

        await agent.generate(state)

        # Verify the LLM call did NOT include history
        call_args = model_client.chat.completions.create.call_args
        messages = call_args.kwargs["messages"]

        roles = [m["role"] for m in messages]
        assert roles == ["system", "user"]  # no history


@pytest.mark.asyncio
async def test_generate_loads_from_postgres_on_cold_start(mock_deps):
    """On buffer miss, history should be loaded from Postgres."""
    vector_store, config_manager, postgres_storage = mock_deps

    from langchain_core.messages import HumanMessage, AIMessage

    # Postgres returns existing history
    postgres_storage.get_messages = AsyncMock(return_value=[
        HumanMessage(content="Prior question"),
        AIMessage(content="Prior answer"),
    ])

    # Same-topic embedding so history is included
    base_emb = [0.5] * 384
    vector_store.embeddings.aembed_query = AsyncMock(return_value=base_emb)

    from agent import ChatAgent

    with patch.object(ChatAgent, 'set_current_model'):
        agent = ChatAgent(vector_store, config_manager, postgres_storage)
        agent.current_model = "test-model"
        agent.system_prompt_template = MagicMock()
        agent.system_prompt_template.render = MagicMock(return_value="System prompt")

        # Set a prior embedding so drift detection can work
        agent.context_buffer.set_query_embedding("chat1", base_emb)

        chunks = [
            FakeChunk(content="Response"),
            FakeChunk(finish_reason="stop", usage=FakeUsage()),
        ]

        model_client = AsyncMock()
        model_client.chat.completions.create = AsyncMock(return_value=FakeStreamContext(chunks))
        agent.model_client = model_client

        agent.stream_callback = AsyncMock()
        agent._usage_accumulator = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        state = {
            "chat_id": "chat1",
            "user_id": "user1",
            "messages": [HumanMessage(content="Follow up question")],
        }

        await agent.generate(state)

        # Verify Postgres was called for history
        postgres_storage.get_messages.assert_called_once_with("user1", "chat1")

        # Verify history was included in LLM call
        call_args = model_client.chat.completions.create.call_args
        messages = call_args.kwargs["messages"]
        roles = [m["role"] for m in messages]
        assert "assistant" in roles  # from Postgres history


@pytest.mark.asyncio
async def test_buffer_updated_after_generate(mock_deps):
    """After generate completes, the buffer should contain the new turn."""
    vector_store, config_manager, postgres_storage = mock_deps

    base_emb = [0.5] * 384
    vector_store.embeddings.aembed_query = AsyncMock(return_value=base_emb)

    from agent import ChatAgent

    with patch.object(ChatAgent, 'set_current_model'):
        agent = ChatAgent(vector_store, config_manager, postgres_storage)
        agent.current_model = "test-model"
        agent.system_prompt_template = MagicMock()
        agent.system_prompt_template.render = MagicMock(return_value="System prompt")

        chunks = [
            FakeChunk(content="Generated answer"),
            FakeChunk(finish_reason="stop", usage=FakeUsage()),
        ]

        model_client = AsyncMock()
        model_client.chat.completions.create = AsyncMock(return_value=FakeStreamContext(chunks))
        agent.model_client = model_client

        agent.stream_callback = AsyncMock()
        agent._usage_accumulator = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        from langchain_core.messages import HumanMessage
        state = {
            "chat_id": "chat1",
            "user_id": "user1",
            "messages": [HumanMessage(content="New question")],
        }

        await agent.generate(state)

        # Buffer should now have the turn
        history = agent.context_buffer.get("chat1")
        assert history is not None
        assert len(history) == 2
        assert history[0] == {"role": "user", "content": "New question"}
        assert history[1] == {"role": "assistant", "content": "Generated answer"}

        # Embedding should be stored
        stored_emb = agent.context_buffer.get_query_embedding("chat1")
        assert stored_emb == base_emb


@pytest.mark.asyncio
async def test_no_shared_state_between_chats(mock_deps):
    """Concurrent requests to different chats shouldn't cross-contaminate."""
    vector_store, config_manager, postgres_storage = mock_deps

    vector_store.embeddings.aembed_query = AsyncMock(return_value=[0.5] * 384)

    from agent import ChatAgent

    with patch.object(ChatAgent, 'set_current_model'):
        agent = ChatAgent(vector_store, config_manager, postgres_storage)
        agent.current_model = "test-model"
        agent.system_prompt_template = MagicMock()
        agent.system_prompt_template.render = MagicMock(return_value="System prompt")

        agent.context_buffer.append("chatA", "Question A", "Answer A")
        agent.context_buffer.append("chatB", "Question B", "Answer B")

        histA = agent.context_buffer.get("chatA")
        histB = agent.context_buffer.get("chatB")

        assert histA[0]["content"] == "Question A"
        assert histB[0]["content"] == "Question B"
        assert histA != histB
